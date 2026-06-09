"""Offline tests for the multi-op multi-device remote batch runner
(``_execute_op_subprocess.py``) and the ``execute_op_remote`` bridge unwrap.

No hardware: a ``Fake`` device (``__getattr__`` recorder) is injected via
``_build_device`` so we can assert the connect-once / no-reset / safe-off
guarantees at the driver-call level. Packs are registered directly because the
PYTHONPATH test env has no installed bundle.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import rules_packager_base.rules.v2_0_2.parser._default_registry as reg
import labscpi.rules.v2_0_1.parser as labpack

reg._DEFAULT_PACK_PARSERS.update({
    "psu": labpack.PACK_PARSER, "eload": labpack.PACK_PARSER,
    "scope": labpack.PACK_PARSER,
})
reg.register_lifecycle(labpack.LIFECYCLE)

_RUNNER = (Path(__file__).resolve().parent.parent
           / "workflow_editor" / "llm" / "_execute_op_subprocess.py")
_spec = importlib.util.spec_from_file_location("_exec_op_runner", _RUNNER)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

_PROC = {"equipment": [{"id": "PSU1", "type": "psu"},
                       {"id": "ELOAD1", "type": "eload"}]}
_BENCH = {"PSU1": {"visa": "MOCK", "_dev": "PSU1"},
          "ELOAD1": {"visa": "MOCK", "_dev": "ELOAD1"}}


class _Fake:
    """Records every driver method call; measures return a float."""

    def __init__(self, etype, device, calls):
        self._e, self._d, self._calls = etype, device, calls

    def __getattr__(self, name):
        def rec(*a, **k):
            self._calls.append((self._e, self._d, name, dict(k)))
            return 1.23
        return rec


class BatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.calls: list = []
        er._build_device = lambda bench, etype, lifecycle, log: _Fake(
            etype, bench["_dev"], self.calls)

    def _devcalls(self, device, method):
        return [c for c in self.calls if c[1] == device and c[2] == method]

    def test_clean_batch_no_reset_no_teardown_right_devices(self):
        ops = [
            {"node_path": "s0", "op": {"op": "psu.measure_voltage",
                                       "device": "PSU1", "channel": 1, "ref": 7}},
            {"node_path": "s1", "op": {"op": "eload.measure_current",
                                       "device": "ELOAD1", "channel": 1, "ref": 8}},
        ]
        r = er._run_batch({"procedure_json": _PROC, "ops": ops, "bench_map": _BENCH})
        self.assertFalse(r["aborted"])
        self.assertTrue(all(x["ok"] and x["has_value"] for x in r["results"]))
        self.assertEqual([x["node_path"] for x in r["results"]], ["s0", "s1"])
        # each device connect+initialize+close EXACTLY once; reset NEVER
        for d in ("PSU1", "ELOAD1"):
            self.assertEqual(len(self._devcalls(d, "connect")), 1)
            self.assertEqual(len(self._devcalls(d, "initialize")), 1)
            self.assertEqual(len(self._devcalls(d, "close")), 1)
        self.assertFalse(any(c[2] == "reset" for c in self.calls))
        # routing: eload measure -> ELOAD1.get_current, psu -> PSU1.measure_voltage
        gc = [c for c in self.calls if c[2] == "get_current"]
        self.assertEqual([c[1] for c in gc], ["ELOAD1"])
        mv = [c for c in self.calls if c[2] == "measure_voltage"]
        self.assertEqual([c[1] for c in mv], ["PSU1"])
        # clean batch never powers down
        self.assertFalse(any(c[3].get("on") is False for c in self.calls))

    def test_partial_failure_safe_off_eload_before_psu_before_close(self):
        ops = [
            {"node_path": "s0", "op": {"op": "psu.set_voltage", "device": "PSU1",
                                       "channel": 1, "volts": 5.0, "volts_unit": "V"}},
            {"node_path": "s1", "op": {"op": "eload.set_current", "device": "ELOAD1",
                                       "channel": 1, "amps": 2.0, "amps_unit": "A"}},
            # PSU2 absent from bench_map -> _NotRemote -> batch aborts here
            {"node_path": "s2", "op": {"op": "psu.set_voltage", "device": "PSU2",
                                       "channel": 1, "volts": 9.0, "volts_unit": "V"}},
        ]
        r = er._run_batch({"procedure_json": _PROC, "ops": ops, "bench_map": _BENCH})
        self.assertTrue(r["aborted"])
        self.assertEqual([x["ok"] for x in r["results"]], [True, True, False])
        self.assertIn("PSU2", r["results"][2]["error"])
        seq = [(c[1], c[2]) for c in self.calls]
        offs = [(c[1], c[2]) for c in self.calls if c[3].get("on") is False]
        # ELOAD (cleanup priority 10) before PSU (priority 20)
        self.assertEqual(offs, [("ELOAD1", "set_output"), ("PSU1", "output")])
        # each safe-off precedes that device's close
        self.assertLess(seq.index(("ELOAD1", "set_output")), seq.index(("ELOAD1", "close")))
        self.assertLess(seq.index(("PSU1", "output")), seq.index(("PSU1", "close")))
        self.assertFalse(any(c[2] == "reset" for c in self.calls))

    def test_safe_off_mode(self):
        r = er._run_safe_off({
            "procedure_json": _PROC, "bench_map": _BENCH,
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]},
                         {"device": "ELOAD1", "etype": "eload", "channels": [1]}],
        })
        self.assertTrue(r["ok"])
        offs = [(c[1], c[2]) for c in self.calls if c[3].get("on") is False]
        self.assertEqual(offs, [("ELOAD1", "set_output"), ("PSU1", "output")])
        self.assertFalse(any(c[2] == "reset" for c in self.calls))


class ExecuteOpRemoteUnwrapTests(unittest.TestCase):
    """The single-op bridge unwraps the batch result to the flat ⚡ shape."""

    def _pp(self):
        from tests._qt_stub import ensure_workflow_editor_importable
        ensure_workflow_editor_importable()
        from workflow_editor.llm import pack_parsers as pp
        return pp

    def _patch(self, pp, batch_result):
        self._orig = (pp._resolve_project_python, pp._subprocess_call)
        pp._resolve_project_python = lambda root: Path("py")
        pp._subprocess_call = lambda py, spec, timeout, runner_name=None: batch_result
        self.addCleanup(lambda: setattr(pp, "_resolve_project_python", self._orig[0]))
        self.addCleanup(lambda: setattr(pp, "_subprocess_call", self._orig[1]))

    def test_unwrap_measure(self):
        pp = self._pp()
        self._patch(pp, {
            "ok": True, "aborted": False,
            "results": [{"node_path": "", "ok": True, "has_value": True,
                         "value": 3.3, "unit": "V", "ref": 7}],
            "log": ["x"]})
        out = pp.execute_op_remote(
            {"op": "psu.measure_voltage", "device": "PSU1"},
            _PROC, {"visa": "MOCK"}, Path("/nonexistent"))
        self.assertEqual(out, {"ok": True, "has_value": True, "value": 3.3,
                               "unit": "V", "ref": 7, "log": ["x"]})

    def test_unwrap_failure(self):
        pp = self._pp()
        self._patch(pp, {
            "ok": True, "aborted": True,
            "results": [{"node_path": "", "ok": False, "error": "boom"}],
            "log": []})
        out = pp.execute_op_remote(
            {"op": "psu.set_voltage", "device": "PSU1"},
            _PROC, {"visa": "MOCK"}, Path("/nonexistent"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "boom")


if __name__ == "__main__":
    unittest.main()
