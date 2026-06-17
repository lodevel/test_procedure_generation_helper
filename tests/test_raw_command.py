"""Offline tests for the raw-terminal path of the persistent-daemon runner
(``_execute_op_subprocess.py``): the ``_raw_send`` adapter seam, the ``_cmd_raw``
command handler (per_step/per_session coherence + broken-handle drop), and the
``_namespace_for`` declared-type -> pack-namespace resolver.

No hardware and no installed bundle required: ``_raw_send`` is exercised with
tiny fake drivers, and ``_cmd_raw`` is driven on a single ``_Session`` with a
hand-built lifecycle and an injected ``_build_device`` (so the four-rung adapter
ladder ORDER and the connection lifecycle are pinned at the driver-call level).
``_safe_name`` is stubbed so the broken-handle drop needs no bundle import.
The dispatch half of ``_namespace_for`` is skipped when ``rules_packager_base``
is not importable (it falls back to the declared type, which is also asserted).
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

try:  # the real registry is only present with an installed bundle
    import rules_packager_base.rules.v2_0_2.parser._default_registry as reg
    _HAVE_REG = True
except Exception:  # noqa: BLE001 — WSL/dev box without the wheel
    reg = None
    _HAVE_REG = False

_RUNNER = (Path(__file__).resolve().parent.parent
           / "workflow_editor" / "llm" / "_execute_op_subprocess.py")
_spec = importlib.util.spec_from_file_location("_exec_op_runner_raw", _RUNNER)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)


class RawSendTierTests(unittest.TestCase):
    """The ``_raw_send`` ladder dispatches on the FIRST interface present, in a
    load-bearing order: public passthrough -> fncore raw_command/_write_readline
    -> SCPI-session wrapper -> bare resource -> RuntimeError."""

    def test_query_raw_for_question(self):
        class D:
            def query_raw(self, t): return "Q:" + t
            def write_raw(self, t): self.w = t
        self.assertEqual(er._raw_send(D(), "*IDN?"), "Q:*IDN?")

    def test_write_raw_for_non_question(self):
        rec = {}
        class D:
            def query_raw(self, t): return "Q"
            def write_raw(self, t): rec["w"] = t
        self.assertEqual(er._raw_send(D(), "RUN"), "")
        self.assertEqual(rec["w"], "RUN")

    def test_raw_command_preferred_over_write_readline(self):
        class D:
            def raw_command(self, t): return "MULTI\nLINE"
            def _write_readline(self, t): return "SINGLE"
        self.assertEqual(er._raw_send(D(), "help"), "MULTI\nLINE")

    def test_write_readline_fallback(self):
        class D:
            def _write_readline(self, t): return "R:" + t
        self.assertEqual(er._raw_send(D(), "x"), "R:x")

    def test_scpi_session_wrapper(self):
        class S:
            def query(self, t): return "SQ:" + t
            def write(self, t): self.w = t
        class D:
            def __init__(self): self.s = S()
        self.assertEqual(er._raw_send(D(), "V?"), "SQ:V?")
        d = D()
        self.assertEqual(er._raw_send(d, "OUTP ON"), "")
        self.assertEqual(d.s.w, "OUTP ON")

    def test_bare_resource_last_resort(self):
        class R:
            def query(self, t): return "RQ:" + t
            def write(self, t): self.w = t
        class D:
            def __init__(self): self._resource = R()
        self.assertEqual(er._raw_send(D(), "V?"), "RQ:V?")

    def test_no_interface_raises(self):
        class D:
            pass
        with self.assertRaises(RuntimeError):
            er._raw_send(D(), "x?")

    def test_public_passthrough_beats_private(self):
        class D:  # both present -> the public tier 1 wins
            def query_raw(self, t): return "PUBLIC"
            def raw_command(self, t): return "FNCORE"
            def _write_readline(self, t): return "PRIVATE"
        self.assertEqual(er._raw_send(D(), "x?"), "PUBLIC")


class CmdRawTests(unittest.TestCase):
    """``_cmd_raw`` connection-lifecycle coherence, hand-built lifecycle + an
    injected ``_build_device`` so no bundle is needed."""

    def setUp(self):
        self._orig_build = er._build_device
        self._orig_safe = er._safe_name
        er._safe_name = lambda d: str(d)   # avoid the bundle import in the drop path
        self.addCleanup(self._restore)

    def _restore(self):
        er._build_device = self._orig_build
        er._safe_name = self._orig_safe

    def _session(self, lifecycle):
        s = er._Session()
        s.lifecycle = lifecycle
        return s

    def test_per_session_reuses_held(self):
        s = self._session({"psu": {"driver_class": "PSU"}})
        class Held:
            def query_raw(self, t): return "12.00"
        s.held["PSU1"] = Held()
        r = er._cmd_raw(s, {"device": "PSU1", "etype": "psu", "subtype": "",
                            "text": "MEAS:VOLT?",
                            "bench_map": {"PSU1": {"session_policy": "per_session"}}})
        self.assertTrue(r["ok"])
        self.assertEqual(r["response"], "12.00")
        self.assertTrue(any("PSU1 -> 12.00" in ln for ln in r["log"]))

    def test_per_session_broken_handle_dropped(self):
        s = self._session({"psu": {"driver_class": "PSU"}})
        class Boom:
            def query_raw(self, t): raise IOError("link dropped")
        s.held["BOOM"] = Boom()
        s.etype_of["BOOM"] = "psu"
        r = er._cmd_raw(s, {"device": "BOOM", "etype": "psu", "subtype": "",
                            "text": "X?",
                            "bench_map": {"BOOM": {"session_policy": "per_session"}}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "ExecError")
        self.assertNotIn("BOOM", s.held)   # broken handle dropped -> retry-on-next

    def test_per_step_transient_open_no_reset_close(self):
        s = self._session({"ctrl": {"driver_class": "Fake"}})
        ev = {"open": 0, "close": 0, "reset": 0}
        class Dev:
            def connect(self): ev["open"] += 1
            def initialize(self): pass
            def reset(self): ev["reset"] += 1
            def close(self): ev["close"] += 1
            def raw_command(self, t): return "R[" + t + "]"
        er._build_device = lambda bench, ns, lc, log: Dev()
        r = er._cmd_raw(s, {"device": "FN1", "etype": "ctrl", "subtype": "",
                            "text": "help",
                            "bench_map": {"FN1": {"port": "COM1",
                                                  "session_policy": "per_step"}}})
        self.assertTrue(r["ok"])
        self.assertEqual(r["response"], "R[help]")
        self.assertEqual(ev["open"], 1)
        self.assertEqual(ev["close"], 1)   # transient: closed after the command
        self.assertEqual(ev["reset"], 0)   # open-NO-reset
        self.assertNotIn("FN1", s.held)    # per_step never enters held

    def test_empty_text_guarded(self):
        s = self._session({"psu": {"driver_class": "PSU"}})
        r = er._cmd_raw(s, {"device": "PSU1", "etype": "psu", "subtype": "",
                            "text": "   ", "bench_map": {}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "Protocol")

    def test_no_bench_params_errors(self):
        s = self._session({"psu": {"driver_class": "PSU"}})
        r = er._cmd_raw(s, {"device": "PSUX", "etype": "psu", "subtype": "",
                            "text": "*IDN?", "bench_map": {}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "ExecError")
        self.assertIn("PSUX", r["error"])


class NamespaceForTests(unittest.TestCase):
    def test_fallback_returns_declared_type(self):
        # psu/eload/scope: namespace == declared type, so the fallback is correct
        self.assertEqual(er._namespace_for("psu", ""), "psu")

    @unittest.skipUnless(_HAVE_REG, "needs rules_packager_base (installed bundle)")
    def test_controller_resolves_to_fncore(self):
        reg.set_equipment_dispatch({"controller": {"": "fncore"}})
        self.assertEqual(er._namespace_for("controller", ""), "fncore")
        # an unclaimed type still falls back to itself
        self.assertEqual(er._namespace_for("nosuch", ""), "nosuch")


if __name__ == "__main__":
    unittest.main()
