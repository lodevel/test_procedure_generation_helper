"""Offline tests for the persistent-daemon remote runner
(``_execute_op_subprocess.py``) and the ``execute_op_remote`` bridge unwrap.

No hardware: a ``Fake`` device (``__getattr__`` recorder) is injected via
``_build_device`` so we can assert the held-session / no-reset / safe-off
guarantees at the driver-call level.  The runner is exercised through its
command handlers (``_cmd_exec_ops`` / ``_cmd_safe_off`` / ``_cmd_shutdown`` /
``_teardown``) on a single ``_Session``, the way the daemon loop drives them.
Packs are registered directly because the test env has no installed bundle.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
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


def _proc_file(case: unittest.TestCase, proc: dict = _PROC) -> Path:
    """Write ``proc`` to a tmp procedure.json (RemoteSession is path-based)."""
    td = tempfile.TemporaryDirectory()
    case.addCleanup(td.cleanup)
    path = Path(td.name) / "procedure.json"
    path.write_text(json.dumps(proc), encoding="utf-8")
    return path


def _set_v(device, node="s", volts=5.0):
    return {"node_path": node, "op": {"op": "psu.set_voltage", "device": device,
                                      "channel": 1, "volts": volts, "volts_unit": "V"}}


def _meas_v(device, node="s", ref=7):
    return {"node_path": node, "op": {"op": "psu.measure_voltage",
                                      "device": device, "channel": 1, "ref": ref}}


def _set_i(device, node="s", amps=2.0):
    return {"node_path": node, "op": {"op": "eload.set_current", "device": device,
                                      "channel": 1, "amps": amps, "amps_unit": "A"}}


def _meas_i(device, node="s", ref=8):
    return {"node_path": node, "op": {"op": "eload.measure_current",
                                      "device": device, "channel": 1, "ref": ref}}


class _Fake:
    """Records every driver method call; measures return a float."""

    def __init__(self, etype, device, calls):
        self._e, self._d, self._calls = etype, device, calls

    def __getattr__(self, name):
        def rec(*a, **k):
            self._calls.append((self._e, self._d, name, dict(k)))
            return 1.23
        return rec


class DaemonRunnerTests(unittest.TestCase):
    def setUp(self):
        self.calls: list = []
        er._build_device = lambda bench, etype, lifecycle, log: _Fake(
            etype, bench["_dev"], self.calls)

    def _session(self, proc: dict = _PROC):
        s = er._Session()
        s.lifecycle = reg.get_lifecycle()
        s.procedure_json = proc          # the daemon's _load_procedure latch
        return s

    def _devcalls(self, device, method):
        return [c for c in self.calls if c[1] == device and c[2] == method]

    def _offs(self):
        return [(c[1], c[2]) for c in self.calls if c[3].get("on") is False]

    # -- held session: no close until teardown, no reset ever -----------------
    def test_per_session_devices_held_until_teardown(self):
        s = self._session()
        r = er._cmd_exec_ops(s, {"ops": [_meas_v("PSU1", "s0"), _meas_i("ELOAD1", "s1")],
                                 "bench_map": _BENCH})
        self.assertFalse(r["aborted"])
        self.assertEqual([x["node_path"] for x in r["results"]], ["s0", "s1"])
        self.assertTrue(all(x["ok"] and x["has_value"] for x in r["results"]))
        # routing: eload measure -> ELOAD1.get_current, psu -> PSU1.measure_voltage
        self.assertEqual([c[1] for c in self.calls if c[2] == "get_current"], ["ELOAD1"])
        self.assertEqual([c[1] for c in self.calls if c[2] == "measure_voltage"], ["PSU1"])
        for d in ("PSU1", "ELOAD1"):
            self.assertEqual(len(self._devcalls(d, "connect")), 1)
            self.assertEqual(len(self._devcalls(d, "initialize")), 1)
            self.assertEqual(len(self._devcalls(d, "close")), 0)   # HELD, not closed
        self.assertFalse(any(c[2] == "reset" for c in self.calls))
        self.assertFalse(self._offs())              # read-only batch: no power-down
        self.assertEqual(set(s.held), {"PSU1", "ELOAD1"})
        # teardown closes (and unlocks) each device exactly once
        er._teardown(s)
        for d in ("PSU1", "ELOAD1"):
            self.assertEqual(len(self._devcalls(d, "close")), 1)
        self.assertFalse(s.held)

    # -- the core fix: ONE session reused across separate commands ------------
    def test_session_persists_across_commands(self):
        s = self._session()
        er._cmd_exec_ops(s, {"ops": [_set_v("PSU1")], "bench_map": _BENCH})
        er._cmd_exec_ops(s, {"ops": [_meas_v("PSU1")], "bench_map": _BENCH})
        self.assertEqual(len(self._devcalls("PSU1", "connect")), 1)   # NOT 2
        self.assertEqual(len(self._devcalls("PSU1", "initialize")), 1)
        self.assertEqual(len(self._devcalls("PSU1", "close")), 0)
        er._teardown(s)
        self.assertEqual(len(self._devcalls("PSU1", "close")), 1)

    # -- mixed-policy batch: a per_step device must not drop a held one --------
    def test_mixed_policy_transient_does_not_drop_held(self):
        bench = dict(_BENCH)
        bench["PSU2"] = {"visa": "MOCK", "_dev": "PSU2", "session_policy": "per_step"}
        proc = {"equipment": [{"id": "PSU1", "type": "psu"},
                              {"id": "PSU2", "type": "psu"}]}
        s = self._session(proc)
        ops = [_set_v("PSU1", "a"), _meas_v("PSU2", "b", ref=1), _meas_v("PSU1", "c", ref=2)]
        r = er._cmd_exec_ops(s, {"ops": ops, "bench_map": bench})
        self.assertFalse(r["aborted"])
        # PSU1 held: opened once, NOT closed during the batch
        self.assertEqual(len(self._devcalls("PSU1", "connect")), 1)
        self.assertEqual(len(self._devcalls("PSU1", "close")), 0)
        # PSU2 transient (per_step): opened AND closed within the batch
        self.assertEqual(len(self._devcalls("PSU2", "connect")), 1)
        self.assertEqual(len(self._devcalls("PSU2", "close")), 1)
        # PSU1's later measure still ran after PSU2's transient close
        self.assertEqual(len(self._devcalls("PSU1", "measure_voltage")), 1)
        self.assertIn("PSU1", s.held)
        self.assertNotIn("PSU2", s.held)

    # -- partial failure: safe-off energized held, ELOAD before PSU, no close --
    def test_partial_failure_safe_off_keeps_held_until_teardown(self):
        s = self._session()
        ops = [_set_v("PSU1", "s0"), _set_i("ELOAD1", "s1"),
               _set_v("PSU2", "s2")]   # PSU2 absent from bench -> _NotRemote, aborts
        r = er._cmd_exec_ops(s, {"ops": ops, "bench_map": _BENCH})
        self.assertTrue(r["aborted"])
        self.assertEqual([x["ok"] for x in r["results"]], [True, True, False])
        self.assertIn("PSU2", r["results"][2]["error"])
        # ELOAD (cleanup priority 10) before PSU (priority 20)
        self.assertEqual(self._offs(), [("ELOAD1", "set_output"), ("PSU1", "output")])
        # held devices safe-off'd but NOT closed (session continues, re-armable)
        self.assertEqual(len(self._devcalls("PSU1", "close")), 0)
        self.assertEqual(len(self._devcalls("ELOAD1", "close")), 0)
        self.assertFalse(any(c[2] == "reset" for c in self.calls))
        er._teardown(s)   # only now do they close

    # -- explicit safe_off command: open, off, keep held ----------------------
    def test_safe_off_command(self):
        s = self._session()
        r = er._cmd_safe_off(s, {
            "bench_map": _BENCH,
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]},
                         {"device": "ELOAD1", "etype": "eload", "channels": [1]}]})
        self.assertTrue(r["ok"])
        self.assertEqual(self._offs(), [("ELOAD1", "set_output"), ("PSU1", "output")])
        self.assertFalse(any(c[2] == "reset" for c in self.calls))
        self.assertEqual(len(self._devcalls("PSU1", "close")), 0)   # kept held
        er._teardown(s)
        self.assertEqual(len(self._devcalls("PSU1", "close")), 1)

    # -- shutdown safe-offs ALL held by declared channels (Codex #2) ----------
    def test_shutdown_safe_offs_all_declared_before_close(self):
        proc = {"equipment": [{"id": "PSU1", "type": "psu", "channels": [1, 2]},
                              {"id": "ELOAD1", "type": "eload", "channels": [1]}]}
        s = self._session(proc)
        er._cmd_exec_ops(s, {"ops": [_meas_v("PSU1", "r", ref=1), _meas_i("ELOAD1", "r2", ref=2)],
                             "bench_map": _BENCH})
        self.assertFalse(self._offs())          # nothing powered down during the batch
        er._cmd_shutdown(s, {})
        offs = self._offs()
        self.assertEqual(offs[0], ("ELOAD1", "set_output"))   # ELOAD before PSU
        # a read-only session STILL safe-offs every declared psu channel at end
        psu_offs = [c for c in self.calls if c[1] == "PSU1" and c[3].get("on") is False]
        self.assertEqual(len(psu_offs), 2)                    # both declared channels
        seq = [(c[1], c[2]) for c in self.calls]
        self.assertLess(seq.index(("ELOAD1", "set_output")), seq.index(("ELOAD1", "close")))
        self.assertFalse(s.held)

    # -- safe-off OP verb is metadata-driven (off_op), default "output" --------
    def test_safe_off_uses_pack_off_op_verb(self):
        recorded: list = []
        orig = er._exec_op
        er._exec_op = lambda proc, op, ns: (recorded.append(op["op"]),
                                            (False, None, "", None))[1]
        self.addCleanup(lambda: setattr(er, "_exec_op", orig))
        res = er._ResStub()
        # a future state-holding pack whose off op-verb is NOT "output"
        lc = {"siggen": {"cleanup": {"off_op": "rf_off", "priority": 5}}}
        failures = er._safe_off({}, {}, {"SG1": {1}}, {"SG1": "siggen"}, lc, res)
        self.assertEqual(failures, [])
        self.assertIn("siggen.rf_off", recorded)      # honored the declared verb
        self.assertNotIn("siggen.output", recorded)
        # default convention when off_op absent -> "output"
        recorded.clear()
        lc2 = {"psu": {"cleanup": {"off_method": "output", "priority": 20}}}
        er._safe_off({}, {}, {"PSU1": {1}}, {"PSU1": "psu"}, lc2, res)
        self.assertIn("psu.output", recorded)

    # -- a safe-off that can't power a device down is LOUD, not swallowed ------
    def test_safe_off_failure_returns_unsafe_and_logs(self):
        def _boom(*a, **k):
            raise RuntimeError("no comms")
        orig = er._exec_op
        er._exec_op = _boom
        self.addCleanup(lambda: setattr(er, "_exec_op", orig))
        res = er._ResStub()
        lc = {"psu": {"cleanup": {"off_op": "output", "priority": 20}}}
        failures = er._safe_off({}, {}, {"PSU1": {1}}, {"PSU1": "psu"}, lc, res)
        self.assertEqual(failures, ["PSU1 CH1"])
        self.assertTrue(any("UNSAFE" in line for line in res.log))

    # -- no bundle/lifecycle -> hard visible error, never a silent no-power-down
    def test_exec_ops_without_lifecycle_errors(self):
        s = er._Session()                     # lifecycle empty (bundle never loaded)
        r = er._cmd_exec_ops(s, {"ops": [_meas_v("PSU1")], "bench_map": _BENCH})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "NoLifecycle")
        self.assertEqual(self.calls, [])      # nothing opened
        r2 = er._cmd_safe_off(s, {"bench_map": _BENCH,
                                  "safe_off": [{"device": "PSU1", "etype": "psu",
                                                "channels": [1]}]})
        self.assertEqual(r2["kind"], "NoLifecycle")

    # -- no procedure document ever latched -> hard visible error, no exec -----
    def test_unreadable_procedure_path_blocks_only_exec_ops(self):
        """A bad procedure_path must gate exec_ops ONLY — safe_off, ping and
        shutdown proceed (recovery safe-off is the last line of defense and
        must never be blocked by a missing document)."""
        s = self._session()
        s.procedure_json = None
        bad = {"procedure_path": "/nonexistent/procedure.json"}
        resp, stop = er._dispatch(s, {"cmd": "exec_ops", "ops": [],
                                      "bench_map": {}, **bad})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "NoProcedure")
        resp, stop = er._dispatch(s, {"cmd": "ping", **bad})
        self.assertTrue(resp.get("pong"))
        resp, stop = er._dispatch(s, {"cmd": "safe_off", "safe_off": [],
                                      "bench_map": {}, **bad})
        self.assertTrue(resp["ok"])
        resp, stop = er._dispatch(s, {"cmd": "shutdown", **bad})
        self.assertTrue(stop)

    def test_exec_ops_without_procedure_document_errors(self):
        s = er._Session()
        s.lifecycle = reg.get_lifecycle()     # bundle loaded, document never latched
        r = er._cmd_exec_ops(s, {"ops": [_meas_v("PSU1")], "bench_map": _BENCH})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "NoProcedure")
        self.assertEqual(self.calls, [])      # nothing opened

    # -- scope: no cleanup -> never state-changing, closed but never safe-off'd -
    def test_scope_no_cleanup_held_not_safe_off(self):
        lc = reg.get_lifecycle()
        self.assertFalse(er._is_state_changing(
            {"op": "scope.measure_stats", "device": "SCOPE1"}, lc))
        s = self._session()
        s.held["SCOPE1"] = _Fake("scope", "SCOPE1", self.calls)
        s.etype_of["SCOPE1"] = "scope"
        s.procedure_json = {"equipment": [{"id": "SCOPE1", "type": "scope"}]}
        er._teardown(s)
        self.assertEqual(len(self._devcalls("SCOPE1", "close")), 1)   # closed
        self.assertFalse(self._offs())                                # never powered down

    # -- a per_step op that fails mid-op still closes its transient device ------
    def test_per_step_failure_still_closes_transient(self):
        def _boom(*a, **k):
            raise RuntimeError("boom")
        orig = er._exec_op
        er._exec_op = _boom
        self.addCleanup(lambda: setattr(er, "_exec_op", orig))
        bench = {"CTRL1": {"port": "COM1", "_dev": "CTRL1", "session_policy": "per_step"}}
        proc = {"equipment": [{"id": "CTRL1", "type": "controller"}]}
        s = self._session(proc)
        r = er._cmd_exec_ops(s, {"bench_map": bench,
                                 "ops": [{"node_path": "a", "op": {
                                     "op": "controller.write_digital", "device": "CTRL1"}}]})
        self.assertTrue(r["aborted"])
        self.assertEqual(len(self._devcalls("CTRL1", "connect")), 1)
        self.assertEqual(len(self._devcalls("CTRL1", "close")), 1)    # finally closed it
        self.assertNotIn("CTRL1", s.held)


class ProtocolLoopTests(unittest.TestCase):
    """Drive the real daemon main()/_dispatch over a pipe (no packs needed)."""

    def test_framed_loop_ping_unknown_badframe_shutdown(self):
        inp = '{"cmd":"ping"}\n{"cmd":"bogus"}\nnot json\n{"cmd":"shutdown"}\n'
        proc = subprocess.run([sys.executable, str(_RUNNER)], input=inp,
                              capture_output=True, text=True, timeout=30)
        objs = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(objs[0], {"ok": True, "pong": True})         # ping
        self.assertEqual(objs[1]["kind"], "Protocol")                 # unknown cmd
        self.assertIn("bad frame", objs[2]["error"])                  # corrupt frame
        self.assertTrue(objs[3]["ok"])                                # shutdown
        # a stray write to fd-1 must NOT have corrupted any frame (all parsed)
        self.assertEqual(len(objs), 4)


class RemoteSessionUnwrapTests(unittest.TestCase):
    """RemoteSession.exec_op unwraps a batch-of-one to the flat ⚡ shape, and a
    missing venv degrades to a dead-session error (mocks the transport)."""

    def _pp(self):
        from tests._qt_stub import ensure_workflow_editor_importable
        ensure_workflow_editor_importable()
        from workflow_editor.llm import pack_parsers as pp
        return pp

    def _session(self, batch_result):
        pp = self._pp()
        s = pp.RemoteSession(_proc_file(self), Path("/nonexistent"))
        s._request = lambda frame, timeout=None: batch_result   # mock the daemon
        return s

    def test_unwrap_measure(self):
        s = self._session({
            "ok": True, "aborted": False,
            "results": [{"node_path": "", "ok": True, "has_value": True,
                         "value": 3.3, "unit": "V", "ref": 7}],
            "log": ["x"]})
        out = s.exec_op({"op": "psu.measure_voltage", "device": "PSU1"}, {"visa": "MOCK"})
        self.assertEqual(out, {"ok": True, "has_value": True, "value": 3.3,
                               "unit": "V", "ref": 7, "log": ["x"]})

    def test_unwrap_failure(self):
        s = self._session({
            "ok": True, "aborted": True,
            "results": [{"node_path": "", "ok": False, "error": "boom"}],
            "log": []})
        out = s.exec_op({"op": "psu.set_voltage", "device": "PSU1"}, {"visa": "MOCK"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "boom")

    def test_no_venv_marks_session_dead(self):
        pp = self._pp()
        s = pp.RemoteSession(_proc_file(self), Path("/nonexistent"))   # real transport, no venv
        out = s.exec_ops([{"node_path": "a", "op": {
            "op": "psu.measure_voltage", "device": "PSU1"}}], {"PSU1": {"visa": "X"}})
        self.assertFalse(out["ok"])
        self.assertIn(out["kind"], ("ParserUnavailable", "SessionDead"))
        self.assertTrue(s.dead)

    def test_dead_resp_terminates_proc(self):
        # dead == True must GUARANTEE the proc is gone, so recovery never opens a
        # 2nd process on a device the old daemon still holds.
        pp = self._pp()
        s = pp.RemoteSession(_proc_file(self), Path("/x"))

        class _FP:
            def __init__(self): self.killed = False
            def terminate(self): self.killed = True
            def wait(self, timeout=None): return 0
            def kill(self): self.killed = True
            def poll(self): return 0
        fp = _FP()
        s._proc = fp
        out = s._dead_resp("SessionDead", "boom")
        self.assertTrue(s.dead)
        self.assertTrue(fp.killed)               # proc terminated as part of dead
        self.assertEqual(out["kind"], "SessionDead")

    # -- frames carry the document PATH, never the document itself -------------
    def test_frames_carry_procedure_path_never_procedure_json(self):
        pp = self._pp()
        path = _proc_file(self)
        s = pp.RemoteSession(path, Path("/nonexistent"))

        class _In:
            lines: list = []
            def write(self, data): self.lines.append(data)
            def flush(self): pass

        class _Out:
            def readline(self): return '{"ok": true, "results": [], "log": []}\n'

        class _FP:
            stdin = _In(); stdout = _Out()
            def poll(self): return None          # "alive" -> _start never runs
            def wait(self, timeout=None): return 0
        s._proc = _FP()
        s.exec_ops([{"node_path": "a", "op": _meas_v("PSU1")["op"]}],
                   {"PSU1": {"visa": "X"}})
        s.safe_off([{"device": "PSU1", "etype": "psu", "channels": [1]}], {})
        s.shutdown()
        frames = [json.loads(line) for line in _In.lines]
        self.assertEqual([f["cmd"] for f in frames],
                         ["exec_ops", "safe_off", "shutdown"])
        for f in frames:
            self.assertNotIn("procedure_json", f)
            self.assertEqual(f["procedure_path"], str(Path(path).resolve()))


class RemoteSessionLiveTransportTests(unittest.TestCase):
    """RemoteSession driving the REAL daemon subprocess over a pipe — exercises
    Popen, framed write/read, ONE-process reuse across requests, and clean
    shutdown, without needing a project venv or hardware (ping needs no bundle)."""

    def _live_session(self):
        from tests._qt_stub import ensure_workflow_editor_importable
        ensure_workflow_editor_importable()
        from workflow_editor.llm import pack_parsers as pp
        s = pp.RemoteSession(_proc_file(self), Path("/nonexistent"))
        runner = Path(pp.__file__).resolve().parent / "_execute_op_subprocess.py"

        def _start():               # use THIS interpreter (no project venv in test env)
            s._bundle_dir = None
            s._proc = subprocess.Popen(
                [sys.executable, str(runner)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
            s._dead = False
        s._start = _start
        self.addCleanup(s.kill)
        return s

    def test_ping_reuses_one_process_then_shutdown(self):
        s = self._live_session()
        self.assertEqual(s.ping().get("pong"), True)
        pid1 = s._proc.pid
        self.assertEqual(s.ping().get("pong"), True)   # same long-lived process
        self.assertEqual(s._proc.pid, pid1)
        out = s.shutdown()
        self.assertTrue(out["ok"])
        self.assertTrue(s.dead)


if __name__ == "__main__":
    unittest.main()
