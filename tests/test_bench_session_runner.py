"""Offline tests for the bench-session holder machinery (S3):
``_bench_session_runner.py`` + the daemon dispatch/reader wiring in
``_execute_op_subprocess.py`` + the ``BenchSessionClient`` channel.

Follows the ``test_execute_op_batch`` patterns: no hardware — a ``_Fake``
device (``__getattr__`` recorder) is injected via ``_build_device``; handlers
are driven through ``er._dispatch`` on a single ``_Session`` the way the
daemon loop drives them.  The generated-test fixture is produced by the REAL
wheel codegen (parse + generate) so the ExecutionContext seam is pinned end to
end.  The daemon-side reader is fed by a scripted host-side stream (the reader
thread is the ONE allowed thread; scripts sleep/wait on it, not on extra
threads)."""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pytest

pytest.importorskip(
    "rules_packager_base",
    reason="needs the installed grammar wheel (bench/host venv); absent on CI",
)
pytest.importorskip(
    "rules_packager_base.bench_endpoints",
    reason="needs the bench-session wheel (rules_packager >= 2.0.5)",
)
import rules_packager_base.rules.v2_0_2.parser._default_registry as reg
import labscpi_pack.rules.v2_0_1.parser as labpack


def setUpModule() -> None:
    """Register the labscpi pack for in-process parse/generate/exec.

    Deliberately at RUN time, not import time: other test modules evaluate
    their skip probes against the registry state at COLLECTION (import), and
    an import-time lifecycle/dispatch registration here would flip those
    decisions suite-wide."""
    reg._DEFAULT_PACK_PARSERS.update({
        "psu": labpack.PACK_PARSER, "eload": labpack.PACK_PARSER,
        "scope": labpack.PACK_PARSER,
    })
    reg.register_lifecycle(labpack.LIFECYCLE)
    # codegen resolves lifecycle through the equipment dispatch (etype ->
    # pack namespace); labscpi is single-variant so namespace == etype.
    reg.set_equipment_dispatch({e: {"": e} for e in ("psu", "eload", "scope")})

_RUNNER = (Path(__file__).resolve().parent.parent
           / "workflow_editor" / "llm" / "_execute_op_subprocess.py")
_BSR_PATH = _RUNNER.parent / "_bench_session_runner.py"

# Load the sibling FIRST under its canonical name so the daemon's lazy
# ``import _bench_session_runner`` resolves to the same module object.
if "_bench_session_runner" in sys.modules:
    bsr = sys.modules["_bench_session_runner"]
else:
    _bspec = importlib.util.spec_from_file_location(
        "_bench_session_runner", _BSR_PATH)
    bsr = importlib.util.module_from_spec(_bspec)
    sys.modules["_bench_session_runner"] = bsr
    _bspec.loader.exec_module(bsr)

_spec = importlib.util.spec_from_file_location("_exec_op_runner_bench", _RUNNER)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)


# ---------------------------------------------------------------------------
# Generated-test fixtures (REAL wheel codegen).
# ---------------------------------------------------------------------------

_TEXT_QUICK = """# BENCH-Q
Bench-session fixture.

## Meta
format_version: 2.0.2
board: K
rules_pack: rules_packager_base@2.0.5
labscpi_pack: labscpi@2.0.1

## Steps
1. Set PSU1 CH1 voltage = 5.0 V.
2. Set PSU1 CH1 output = ON.
3. Operator: read DC voltage on TP VOUT with DMM1 as {1}.

## Equipment
PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]
DMM1 : dmm

## Expected
{1} < 10 V
"""

# Same, plus a long cancellable wait BEFORE the operator read.
_TEXT_WAIT = _TEXT_QUICK.replace(
    "3. Operator: read DC voltage on TP VOUT with DMM1 as {1}.",
    "3. Wait 30 s.\n4. Operator: read DC voltage on TP VOUT with DMM1 as {1}.",
).replace("# BENCH-Q", "# BENCH-W")

# Same, plus a scope screenshot (a RELATIVE artifact filename) before the read.
_TEXT_SHOT = _TEXT_QUICK.replace(
    "3. Operator: read DC voltage on TP VOUT with DMM1 as {1}.",
    '3. Save SCOPE1 screenshot as "shot.png".\n'
    "4. Operator: read DC voltage on TP VOUT with DMM1 as {1}.",
).replace(
    "DMM1 : dmm",
    "SCOPE1 : scope channels=[1]\nDMM1 : dmm",
).replace("# BENCH-Q", "# BENCH-S")

_TEXTS = {"quick": _TEXT_QUICK, "wait": _TEXT_WAIT, "shot": _TEXT_SHOT}


@functools.lru_cache(maxsize=None)
def _code(kind: str) -> str:
    from rules_packager_base.rules.v2_0_2.parser import parse
    from rules_packager_base.rules.v2_0_2.parser.codegen import generate
    r = parse(_TEXTS[kind])
    assert r.success and not r.findings, [f.code for f in r.findings]
    code = generate(r.json)
    assert "BENCH_SESSION_API = 1" in code
    return code


def _proc_file(case: unittest.TestCase) -> Path:
    """tmp procedure.json for the path-based client."""
    td = tempfile.TemporaryDirectory()
    case.addCleanup(td.cleanup)
    path = Path(td.name) / "procedure.json"
    path.write_text(json.dumps({"equipment": [{"id": "PSU1", "type": "psu"}]}),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class _Fake:
    """Records every driver method call; measures return a float."""

    def __init__(self, etype, dev, calls):
        self._e, self._d, self._calls = etype, dev, calls

    def __getattr__(self, name):
        def rec(*a, **k):
            self._calls.append((self._e, self._d, name, dict(k)))
            return 1.23
        return rec


class _RaisingFake(_Fake):
    """Like _Fake, but one method raises (driver failure)."""

    def __init__(self, etype, dev, calls, boom_method):
        super().__init__(etype, dev, calls)
        self._boom = boom_method

    def __getattr__(self, name):
        if name == self._boom:
            def boom(*a, **k):
                raise RuntimeError(f"driver failure in {name}")
            return boom
        return super().__getattr__(name)


class _ShotFake(_Fake):
    """Like _Fake, but ``save_screenshot`` actually writes its (RELATIVE)
    filename — the way a real scope driver drops the PNG into the process
    CWD."""

    def save_screenshot(self, filename=None, **k):
        self._calls.append((self._e, self._d, "save_screenshot",
                            {"filename": filename}))
        Path(filename).write_bytes(b"PNG")


class _ProtoCapture:
    """Captures daemon->host frames; exposes prompt frames on a queue so the
    scripted host stream can answer them (deterministic, no polling)."""

    def __init__(self):
        self.frames: list = []
        self.prompts: queue.Queue = queue.Queue()

    def write(self, s):
        for ln in str(s).splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            self.frames.append(obj)
            if obj.get("kind") == "prompt":
                self.prompts.put(obj)

    def flush(self):
        pass

    def kinds(self):
        return [f.get("kind") for f in self.frames]

    def texts(self, kind):
        return [f.get("text", "") for f in self.frames if f.get("kind") == kind]


class _ScriptedStream:
    """Host-side stdin stand-in consumed by the daemon Reader thread.

    Script items (consumed in order; exhaustion == EOF — which the daemon
    treats as "host gone" and cancels an in-flight run):
      ("prompt", fn)        -> wait for the NEXT prompt frame, return fn(frame)
      ("delay", t, line_or_fn) -> sleep t seconds, return the line
      ("hold", event)       -> keep the pipe OPEN until the event, then EOF
      "<raw line>"          -> returned immediately
    The waiting/sleeping happens ON the reader thread — no extra threads."""

    def __init__(self, script, proto: _ProtoCapture):
        self._script = list(script)
        self._proto = proto

    def readline(self):
        if not self._script:
            return ""
        item = self._script.pop(0)
        if isinstance(item, str):
            return item
        if item[0] == "prompt":
            frame = self._proto.prompts.get(timeout=10)
            fn = item[1]
            return fn(frame) if callable(fn) else fn
        if item[0] == "delay":
            time.sleep(item[1])
            payload = item[2]
            return payload() if callable(payload) else payload
        if item[0] == "hold":
            item[1].wait(timeout=30)
            return ""
        raise AssertionError(f"bad script item {item!r}")


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


# ---------------------------------------------------------------------------
# Shared harness.
# ---------------------------------------------------------------------------

_ADDR_A = "TCPIP0::10.0.0.5::INSTR"          # canonical: visa:tcpip::10.0.0.5
_ADDR_A2 = "TCPIP::10.0.0.5::inst0::INSTR"   # same endpoint, other spelling
_ADDR_B = "TCPIP0::10.0.0.6::INSTR"          # canonical: visa:tcpip::10.0.0.6
_EP_A = "visa:tcpip::10.0.0.5"
_EP_B = "visa:tcpip::10.0.0.6"


def _claim(test_id, name, visa=_ADDR_A, etype="psu", dev=None, **extra):
    bench = {"visa": visa, "_dev": dev or name, "channel": 1,
             "timeout_ms": 5000}
    bench.update(extra)
    return {"test_id": test_id, "local_name": name, "etype": etype,
            "bench": bench}


class _BenchBase(unittest.TestCase):
    def setUp(self):
        self.calls: list = []
        er._build_device = lambda bench, etype, lifecycle, log: _Fake(
            etype, bench.get("_dev", bench.get("visa", "?")), self.calls)
        self.proto = _ProtoCapture()
        er._PROTO_OUT = self.proto
        self.addCleanup(lambda: setattr(er, "_PROTO_OUT", None))
        self.addCleanup(lambda: setattr(bsr, "_READER", None))
        # run_test re-asserts the daemon's SIGTERM/SIGINT handlers — restore
        # the pytest process's handlers afterwards.
        for sig in (signal.SIGINT, signal.SIGTERM):
            prev = signal.getsignal(sig)
            self.addCleanup(signal.signal, sig, prev)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # "hold" script items keep the fake pipe open until test end (an EOF
        # during a run is deliberately treated as an operator stop).
        self.hold_evt = threading.Event()
        self.addCleanup(self.hold_evt.set)

    def _hold(self):
        return ("hold", self.hold_evt)

    def _session(self):
        s = er._Session()
        s.lifecycle = reg.get_lifecycle()
        return s

    def _dispatch(self, s, req):
        resp, stop = er._dispatch(s, req)
        return resp, stop

    def _acquire(self, s, claims, **kw):
        resp, stop = self._dispatch(s, {"cmd": "acquire_run",
                                        "claims": claims, **kw})
        self.assertFalse(stop)
        return resp

    def _reader(self, script):
        bsr._READER = bsr.Reader(_ScriptedStream(script, self.proto))
        return bsr._READER

    def _module(self, code) -> Path:
        path = Path(self.tmp.name) / "test.py"
        path.write_text(code, encoding="utf-8")
        return path

    def _results_dir(self) -> Path:
        d = Path(self.tmp.name) / "results"
        return d

    def _run(self, s, run_id, module: Path, script):
        self._reader(script)
        return self._dispatch(s, {
            "cmd": "run_test", "run_id": run_id,
            "module_path": str(module),
            "results_dir": str(self._results_dir())})

    def _devcalls(self, dev, method):
        return [c for c in self.calls if c[1] == dev and c[2] == method]

    def _offs(self):
        return [c for c in self.calls if c[3].get("on") is False]


def _answer(text="3.3"):
    """Script item answering the next prompt with ``text``."""
    return ("prompt",
            lambda f: _line({"cmd": "prompt_response", "id": f["id"],
                             "text": text}))


# ---------------------------------------------------------------------------
# Endpoint rekey: held is keyed by canonical endpoint for run claims.
# ---------------------------------------------------------------------------


class EndpointRekeyTests(_BenchBase):
    def test_two_names_one_endpoint_one_held_instance(self):
        s = self._session()
        resp = self._acquire(s, [
            _claim("T-A", "PSU1", _ADDR_A, dev="PHYS"),
            _claim("T-C", "MAIN_SUPPLY", _ADDR_A2, dev="PHYS"),
        ])
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(set(s.held), {_EP_A})            # ONE endpoint key
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 1)
        self.assertEqual(len(self._devcalls("PHYS", "initialize")), 1)
        self.assertFalse(any(c[2] == "reset" for c in self.calls))
        run = s.bench.runs[resp["run_id"]]
        self.assertEqual(run.view,
                         {"PSU1": _EP_A, "MAIN_SUPPLY": _EP_A})
        usages = resp["plan"]["endpoints"][0]["usages"]
        self.assertEqual(sorted(usages), ["T-A/PSU1", "T-C/MAIN_SUPPLY"])

    def test_same_name_two_endpoints_two_instances(self):
        s = self._session()
        r1 = self._acquire(s, [_claim("T-A", "PSU1", _ADDR_A, dev="P-A")])
        self.assertTrue(r1["ok"], r1)
        resp, _ = self._dispatch(s, {"cmd": "release_run",
                                     "run_id": r1["run_id"]})
        self.assertTrue(resp["ok"])
        r2 = self._acquire(s, [_claim("T-B", "PSU1", _ADDR_B, dev="P-B")])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(set(s.held), {_EP_A, _EP_B})     # TWO endpoints
        self.assertEqual(len(self._devcalls("P-A", "connect")), 1)
        self.assertEqual(len(self._devcalls("P-B", "connect")), 1)
        self.assertEqual(s.bench.runs[r2["run_id"]].view, {"PSU1": _EP_B})
        # release never closed anything — persistent session
        self.assertFalse(any(c[2] == "close" for c in self.calls))

    def test_type_mismatch_is_conflict_nothing_opened(self):
        s = self._session()
        resp = self._acquire(s, [
            _claim("T-A", "PSU1", _ADDR_A, etype="psu"),
            _claim("T-B", "LOAD", _ADDR_A, etype="eload"),
        ])
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "EndpointConflict")
        self.assertTrue(resp["conflicts"])
        self.assertEqual(self.calls, [])                  # nothing opened
        self.assertFalse(s.held)

    def test_acquire_is_exclusive_until_release(self):
        s = self._session()
        r1 = self._acquire(s, [_claim("T-A", "PSU1")])
        self.assertTrue(r1["ok"])
        r2 = self._acquire(s, [_claim("T-B", "PSU2", _ADDR_B)])
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "RunActive")
        self._dispatch(s, {"cmd": "release_run", "run_id": r1["run_id"]})
        r3 = self._acquire(s, [_claim("T-B", "PSU2", _ADDR_B)])
        self.assertTrue(r3["ok"])

    def test_old_wheel_reports_unsupported_bundle(self):
        # Simulate an old wheel: kill BOTH resolution paths of
        # ``from rules_packager_base import bench_endpoints`` — the parent
        # package attribute AND the sys.modules entry (None -> ImportError).
        import rules_packager_base as rpb
        saved_attr = rpb.bench_endpoints
        saved_mod = sys.modules.get("rules_packager_base.bench_endpoints")
        del rpb.bench_endpoints
        sys.modules["rules_packager_base.bench_endpoints"] = None

        def _restore():
            rpb.bench_endpoints = saved_attr
            sys.modules["rules_packager_base.bench_endpoints"] = saved_mod
        self.addCleanup(_restore)
        s = self._session()
        resp = self._acquire(s, [_claim("T-A", "PSU1")])
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "unsupported_bundle")
        self.assertIn("2.0.5", resp["error"])

    def test_identities_from_idn_attribute(self):
        class _IdnFake(_Fake):
            idn = "ACME PSU 9080,SN123"
        er._build_device = lambda bench, etype, lifecycle, log: _IdnFake(
            etype, bench.get("_dev", "?"), self.calls)
        s = self._session()
        resp = self._acquire(s, [_claim("T-A", "PSU1")])
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["identities"], {_EP_A: "ACME PSU 9080,SN123"})

    def test_dry_run_plans_without_opening_or_holding(self):
        # F-dryrun: "dry_run": true reconciles and returns the FULL plan
        # without opening any endpoint, holding anything, creating a run,
        # or reading identities.
        s = self._session()
        claims = [
            _claim("T-A", "PSU1", _ADDR_A, dev="PHYS"),
            _claim("T-C", "MAIN_SUPPLY", _ADDR_A2, dev="PHYS"),
            {"test_id": "T-A", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}},
        ]
        dry = self._acquire(s, claims, dry_run=True)
        self.assertTrue(dry["ok"], dry)
        self.assertTrue(dry["dry_run"])
        self.assertNotIn("run_id", dry)
        self.assertNotIn("identities", dry)         # no identity reads
        self.assertFalse(s.held)                    # nothing opened or held
        self.assertEqual(self.calls, [])
        self.assertFalse(s.bench.runs)              # no run created
        # the dry plan IS the real plan: a following real acquire matches
        real = self._acquire(s, claims)
        self.assertTrue(real["ok"], real)
        self.assertEqual(dry["plan"], real["plan"])
        # and a dry run is allowed even while that run holds the bench
        dry2 = self._acquire(s, claims, dry_run=True)
        self.assertTrue(dry2["ok"], dry2)
        self.assertEqual(dry2["plan"], real["plan"])

    def test_dry_run_conflict_reported_nothing_opened(self):
        s = self._session()
        resp = self._acquire(s, [
            _claim("T-A", "PSU1", _ADDR_A, etype="psu"),
            _claim("T-B", "LOAD", _ADDR_A, etype="eload"),
        ], dry_run=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "EndpointConflict")
        self.assertTrue(resp["conflicts"])
        self.assertEqual(self.calls, [])
        self.assertFalse(s.held)

    def test_manual_claims_never_form_endpoints(self):
        s = self._session()
        resp = self._acquire(s, [
            {"test_id": "T-A", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}}])
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["plan"]["endpoints"], [])
        self.assertEqual(resp["plan"]["manual"], ["T-A/DMM1"])
        self.assertFalse(s.held)


# ---------------------------------------------------------------------------
# Guided-manual routing through the run view (and back to name keys).
# ---------------------------------------------------------------------------


class GuidedRoutingTests(_BenchBase):
    def _set_v(self, device, volts=5.0):
        return {"node_path": "s", "op": {
            "op": "psu.set_voltage", "device": device, "channel": 1,
            "volts": volts, "volts_unit": "V"}}

    def test_named_op_routes_to_held_endpoint_while_run_active(self):
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self.assertTrue(r1["ok"])
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                        "_dev": "GUIDED"}}})
        self.assertFalse(r["aborted"], r)
        # routed onto the RUN-HELD instance: no second open, op landed on PHYS
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 1)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 0)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 1)
        self.assertEqual(set(s.held), {_EP_A})            # no name key added

    def test_named_op_reuses_held_endpoint_without_a_run(self):
        # F-H1: with NO active run, a name-keyed guided op whose bench dict
        # addresses an already-held endpoint (in ANOTHER spelling) lands on
        # the held instance — ONE connection, never a second same-process
        # session contending the endpoint's remote lock.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._dispatch(s, {"cmd": "release_run", "run_id": r1["run_id"]})
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A2,
                                                        "_dev": "GUIDED"}}})
        self.assertFalse(r["aborted"], r)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 0)
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 1)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 1)
        self.assertEqual(set(s.held), {_EP_A})            # no name key added

    def test_named_op_opens_own_device_for_unheld_endpoint(self):
        # Fallback preserved: a genuinely UN-HELD endpoint still opens
        # name-keyed, today's guided-manual semantics.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._dispatch(s, {"cmd": "release_run", "run_id": r1["run_id"]})
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_B,
                                                        "_dev": "GUIDED"}}})
        self.assertFalse(r["aborted"], r)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 1)
        self.assertEqual(len(self._devcalls("GUIDED", "set_voltage")), 1)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 0)
        self.assertEqual(set(s.held), {_EP_A, "PSU1"})

    def test_named_op_stays_name_keyed_on_old_wheel(self):
        # Old-wheel venv (no bench_endpoints): the canonical fold is
        # unavailable, so the op keeps today's name-keyed behavior (the same
        # unsupported-bundle conditional pattern as acquire_run).
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._dispatch(s, {"cmd": "release_run", "run_id": r1["run_id"]})
        import rules_packager_base as rpb
        saved_attr = rpb.bench_endpoints
        saved_mod = sys.modules.get("rules_packager_base.bench_endpoints")
        del rpb.bench_endpoints
        sys.modules["rules_packager_base.bench_endpoints"] = None

        def _restore():
            rpb.bench_endpoints = saved_attr
            sys.modules["rules_packager_base.bench_endpoints"] = saved_mod
        self.addCleanup(_restore)
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                        "_dev": "GUIDED"}}})
        self.assertFalse(r["aborted"], r)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 1)
        self.assertEqual(set(s.held), {_EP_A, "PSU1"})


# ---------------------------------------------------------------------------
# run_test: happy path, prompts, cancel, ladder, reimport, rejection.
# ---------------------------------------------------------------------------


class RunTestTests(_BenchBase):
    def _acquired(self, s, **kw):
        resp = self._acquire(s, [
            _claim("BENCH-Q", "PSU1", dev="PHYS"),
            {"test_id": "BENCH-Q", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}},
        ], **kw)
        self.assertTrue(resp["ok"], resp)
        return resp["run_id"]

    def test_happy_path_prompt_roundtrip_output_frames_results(self):
        s = self._session()
        rid = self._acquired(s)
        resp, stop = self._run(s, rid, self._module(_code("quick")),
                               [_answer("3.3"), self._hold()])
        self.assertFalse(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["overall"], "PASS")
        self.assertFalse(resp["result"]["aborted"])
        self.assertFalse(resp["result"]["error"])
        self.assertNotIn("session_ended", resp)
        # the held instance ran the steps; never reset/closed by the test
        self.assertEqual([c[3].get("volts") for c in
                          self._devcalls("PHYS", "set_voltage")], [5.0])
        self.assertFalse(any(c[2] == "reset" for c in self.calls))
        self.assertFalse(any(c[2] == "close" for c in self.calls))
        # keep_state False (default): the test's own cleanup off fired
        self.assertTrue(self._offs())
        # output frames carried the print stream; prompt frame correlated
        out = self.proto.texts("output")
        self.assertTrue(any(t.startswith("STEP 1 - ") for t in out), out[:5])
        self.assertTrue(any("RESULTS:" in t for t in out))
        prompts = self.proto.texts("prompt")
        self.assertEqual(len(prompts), 1)
        self.assertIn("Read DC voltage", prompts[0])
        # verdicts land host-side via results.json in the EXPLICIT results dir
        data = json.loads((self._results_dir() / "results.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["measurements"], {"1": 3.3})
        self.assertEqual(set(data["verdicts"].values()), {"PASS"})
        # prompt hook restored after the run
        from rules_packager_base import test_helpers
        self.assertIs(test_helpers._prompt_hook, test_helpers._default_prompt_hook)

    def test_keep_state_true_leaves_outputs_untouched(self):
        s = self._session()
        rid = self._acquired(s, keep_state=True)
        resp, _ = self._run(s, rid, self._module(_code("quick")),
                            [_answer("3.3"), self._hold()])
        self.assertEqual(resp["result"]["overall"], "PASS")
        self.assertFalse(self._offs())        # no off anywhere: bench stays hot

    def test_unique_module_name_reimport_runs_new_code(self):
        s = self._session()
        rid = self._acquired(s)
        module = self._module(_code("quick"))
        resp, _ = self._run(s, rid, module, [_answer("3.3"), self._hold()])
        self.assertEqual(resp["result"]["overall"], "PASS")
        # regenerate the SAME path mid-session with different code
        module.write_text(_code("quick").replace("volts=5.0", "volts=7.5"),
                          encoding="utf-8")
        resp2, _ = self._run(s, rid, module, [_answer("3.3"), self._hold()])
        self.assertEqual(resp2["result"]["overall"], "PASS")
        self.assertEqual([c[3].get("volts") for c in
                          self._devcalls("PHYS", "set_voltage")], [5.0, 7.5])

    def test_cancel_mid_sleep_aborts_safe_off_session_survives(self):
        s = self._session()
        rid = self._acquired(s, keep_state=True)   # isolate the LADDER's off
        cancel = _line({"cmd": "cancel_run", "run_id": rid})
        t0 = time.monotonic()
        resp, stop = self._run(s, rid, self._module(_code("wait")),
                               [("delay", 0.6, cancel)])
        self.assertLess(time.monotonic() - t0, 15)     # not the full 30 s wait
        self.assertFalse(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["result"]["aborted"])
        self.assertFalse(resp["result"]["error"])
        self.assertEqual(resp["result"]["overall"], "ABORTED")
        # in-run cancel was acknowledged with an "ack" frame
        self.assertIn("ack", self.proto.kinds())
        # ladder: ordered safe-off of held power equipment (keep_state True,
        # so this off can ONLY be the ladder's) — by the claim's channel
        offs = self._offs()
        self.assertTrue(offs, self.proto.frames)
        self.assertEqual(offs[0][3].get("channel"), 1)
        # session + connections REMAIN; devices stay held; next acquire works
        self.assertEqual(set(s.held), {_EP_A})
        self.assertFalse(any(c[2] == "close" for c in self.calls))
        self._dispatch(s, {"cmd": "release_run", "run_id": rid})
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")])
        self.assertTrue(r2["ok"], r2)

    def test_error_ladder_safe_off_close_session_ends(self):
        er._build_device = lambda bench, etype, lifecycle, log: _RaisingFake(
            etype, bench.get("_dev", "?"), self.calls, "set_voltage")
        s = self._session()
        rid = self._acquired(s)
        cwd_before = os.getcwd()
        resp, stop = self._run(s, rid, self._module(_code("quick")),
                               [self._hold()])
        self.assertTrue(stop)                       # respond then STOP (like shutdown)
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["session_ended"])
        self.assertTrue(resp["result"]["error"])
        self.assertEqual(resp["result"]["overall"], "ERROR")
        # safe-off ran, then held devices were CLOSED (session over)
        self.assertTrue(self._offs())
        self.assertEqual(len(self._devcalls("PHYS", "close")), 1)
        self.assertFalse(s.held)
        # F-H5 guard: CWD restored even on the errored-ladder path
        self.assertEqual(os.getcwd(), cwd_before)

    def test_import_failure_ends_session_via_ladder(self):
        # F-H3a: a test file that cannot even import mid-session is an
        # UNEXPECTED execution failure — errored ladder: safe-off all held,
        # teardown, session_ended, stop.  Never an ExecError leaving the
        # session hot with no safe-off.
        s = self._session()
        rid = self._acquired(s)
        module = Path(self.tmp.name) / "broken_import.py"
        module.write_text("this is not python !!!\n", encoding="utf-8")
        self._reader([])
        resp, stop = self._dispatch(s, {"cmd": "run_test", "run_id": rid,
                                        "module_path": str(module),
                                        "results_dir": self.tmp.name})
        self.assertTrue(stop)                       # respond then STOP
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["session_ended"])
        self.assertTrue(resp["result"]["error"])
        self.assertEqual(resp["result"]["overall"], "ERROR")
        self.assertEqual(resp["result"]["exception_type"], "SyntaxError")
        self.assertIn("traceback", resp["result"])
        self.assertTrue(self._offs())               # safe-off all held ran
        self.assertEqual(len(self._devcalls("PHYS", "close")), 1)
        self.assertFalse(s.held)

    def test_held_endpoint_gone_ends_session_via_ladder(self):
        # F-H3a (context-build failure): a vanished held endpoint is the
        # same unexpected-failure ladder — session ends, stop.
        s = self._session()
        rid = self._acquired(s)
        s.held.clear()                    # the held instance vanished
        self._reader([])
        resp, stop = self._dispatch(s, {"cmd": "run_test", "run_id": rid,
                                        "module_path":
                                            str(self._module(_code("quick"))),
                                        "results_dir": self.tmp.name})
        self.assertTrue(stop)
        self.assertTrue(resp["session_ended"])
        self.assertTrue(resp["result"]["error"])
        self.assertIn("restart the bench session",
                      resp["result"]["exception_message"])

    def test_unknown_run_refusal_is_benign_session_untouched(self):
        # F-H3a counterpart: the genuinely-benign refusal class stays a
        # plain error — no ladder, no teardown, session hot.
        s = self._session()
        self._acquired(s)
        resp, stop = self._dispatch(s, {"cmd": "run_test", "run_id": "nope",
                                        "module_path": "x",
                                        "results_dir": self.tmp.name})
        self.assertFalse(stop)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "UnknownRun")
        self.assertEqual(set(s.held), {_EP_A})
        self.assertFalse(self._offs())
        self.assertFalse(any(c[2] == "close" for c in self.calls))

    def test_scope_screenshot_relative_write_lands_in_results_dir(self):
        # F-H5: generated scope ops write RELATIVE artifact filenames; the
        # daemon runs main(ctx) with CWD at the results dir, so the PNG
        # lands beside results.json (like the legacy subprocess path) — and
        # the prior CWD is restored afterwards.
        er._build_device = lambda bench, etype, lifecycle, log: _ShotFake(
            etype, bench.get("_dev", "?"), self.calls)
        s = self._session()
        resp = self._acquire(s, [
            _claim("BENCH-S", "PSU1", dev="PHYS"),
            _claim("BENCH-S", "SCOPE1", _ADDR_B, etype="scope", dev="SCOPE"),
            {"test_id": "BENCH-S", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}},
        ])
        self.assertTrue(resp["ok"], resp)
        cwd_before = os.getcwd()
        r, stop = self._run(s, resp["run_id"], self._module(_code("shot")),
                            [_answer("3.3"), self._hold()])
        self.assertFalse(stop)
        self.assertEqual(r["result"]["overall"], "PASS", r)
        self.assertEqual(os.getcwd(), cwd_before)   # CWD restored after run
        shot = self._results_dir() / "shot.png"
        self.assertTrue(shot.exists(),
                        sorted(p.name for p in self._results_dir().iterdir()))
        self.assertFalse((Path(cwd_before) / "shot.png").exists())
        # the evidence path in results.json refers to the file BESIDE it
        data = json.loads((self._results_dir() / "results.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual([e["file"] for e in data["evidence"]], ["shot.png"])

    def test_exec_ops_relative_write_stays_in_daemon_cwd(self):
        # Guided-manual screenshot harvesting contract (UNCHANGED by F-H5):
        # outside run_test the daemon CWD is the parent's choice (the
        # RemoteSession screenshot tmp dir) — an exec_ops relative write
        # lands THERE, never in a results dir.
        er._build_device = lambda bench, etype, lifecycle, log: _ShotFake(
            etype, bench.get("_dev", "?"), self.calls)
        cwd0 = os.getcwd()
        os.chdir(self.tmp.name)           # stand-in for the daemon tmp CWD
        self.addCleanup(os.chdir, cwd0)
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "SCOPE1", "type": "scope"}]}
        r = er._cmd_exec_ops(s, {"ops": [{"node_path": "s", "op": {
            "op": "scope.screenshot", "device": "SCOPE1",
            "filename": "guided_shot.png"}}],
            "bench_map": {"SCOPE1": {"visa": _ADDR_B, "_dev": "GUIDED"}}})
        self.assertFalse(r["aborted"], r)
        self.assertTrue((Path(self.tmp.name) / "guided_shot.png").exists())

    def test_escaped_exception_backstop_same_as_error(self):
        s = self._session()
        rid = self._acquired(s)
        module = Path(self.tmp.name) / "broken.py"
        module.write_text("BENCH_SESSION_API = 1\n", encoding="utf-8")  # no main
        self._reader([])
        resp, stop = self._dispatch(s, {"cmd": "run_test", "run_id": rid,
                                        "module_path": str(module),
                                        "results_dir": self.tmp.name})
        self.assertTrue(stop)
        self.assertTrue(resp["session_ended"])
        self.assertTrue(resp["result"]["error"])
        self.assertIn("AttributeError", resp["result"]["exception_type"])
        self.assertFalse(s.held)                    # closed by the ladder

    def test_module_without_marker_is_unsupported_no_ladder(self):
        s = self._session()
        rid = self._acquired(s)
        module = Path(self.tmp.name) / "legacy.py"
        module.write_text("def main(inventory=None):\n    return None\n",
                          encoding="utf-8")
        resp, stop = self._dispatch(s, {"cmd": "run_test", "run_id": rid,
                                        "module_path": str(module),
                                        "results_dir": self.tmp.name})
        self.assertFalse(stop)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "unsupported_test")
        self.assertEqual(set(s.held), {_EP_A})      # session untouched

    def test_prompt_cancel_response_ordering_no_deadlock(self):
        s = self._session()
        rid = self._acquired(s, keep_state=True)
        cancel = _line({"cmd": "cancel_run", "run_id": rid})
        stale = _line({"cmd": "prompt_response", "id": 1, "text": "late"})
        resp, stop = self._run(s, rid, self._module(_code("quick")), [
            ("prompt", lambda f: cancel),     # cancel lands DURING the prompt
            ("delay", 0.05, stale),           # the answer arrives too late
        ])
        self.assertFalse(stop)
        self.assertTrue(resp["result"]["aborted"])
        self.assertIn("ack", self.proto.kinds())
        self.assertTrue(self._offs())               # ladder safe-off ran
        # the stale response reaches the MAIN loop and is rejected (typed)
        line = bsr._READER.next_line()
        self.assertIsNotNone(line)
        late, stop = self._dispatch(s, json.loads(line))
        self.assertFalse(stop)
        self.assertFalse(late["ok"])
        self.assertIn("no prompt outstanding", late["error"])
        # EOF signalled through the queue — the loop would reach _teardown
        self.assertIsNone(bsr._READER.next_line())
        # session alive: next acquire works
        self._dispatch(s, {"cmd": "release_run", "run_id": rid})
        self.assertTrue(self._acquire(
            s, [_claim("T-B", "PSU1", dev="PHYS")])["ok"])

    def test_other_frames_rejected_during_run(self):
        s = self._session()
        rid = self._acquired(s)
        box: dict = {}

        def grab(frame):
            box["id"] = frame["id"]
            return _line({"cmd": "exec_ops", "ops": [], "bench_map": {}})

        def respond():
            return _line({"cmd": "prompt_response", "id": box["id"],
                          "text": "3.3"})

        resp, _ = self._run(s, rid, self._module(_code("quick")),
                            [("prompt", grab), ("delay", 0.05, respond),
                             self._hold()])
        self.assertEqual(resp["result"]["overall"], "PASS")   # run completed
        rejects = [f for f in self.proto.frames if f.get("kind") == "reject"]
        self.assertTrue(any(f.get("cmd") == "exec_ops" for f in rejects))

    def test_cancel_and_prompt_response_idle_are_typed(self):
        s = self._session()
        resp, _ = self._dispatch(s, {"cmd": "cancel_run", "run_id": "nope"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "UnknownRun")
        resp, _ = self._dispatch(s, {"cmd": "prompt_response", "id": 9,
                                     "text": "x"})
        self.assertFalse(resp["ok"])
        self.assertIn("no prompt outstanding", resp["error"])
        resp, _ = self._dispatch(s, {"cmd": "run_test", "run_id": "nope",
                                     "module_path": "x", "results_dir": "y"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "UnknownRun")


# ---------------------------------------------------------------------------
# Real daemon over a pipe: EOF backstop + the BenchSessionClient channel.
# ---------------------------------------------------------------------------


class DaemonWireTests(unittest.TestCase):
    def test_eof_during_idle_reaches_teardown_backstop(self):
        # ping, then close stdin WITHOUT shutdown: the reader signals EOF
        # through the queue and the loop exits cleanly through _teardown.
        proc = subprocess.run([sys.executable, str(_RUNNER)],
                              input='{"cmd":"ping"}\n',
                              capture_output=True, text=True, timeout=30)
        objs = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(objs, [{"ok": True, "pong": True}])


class BenchClientLiveTests(unittest.TestCase):
    """BenchSessionClient driving the REAL daemon subprocess: acquire (manual
    claims — no bundle needed), run_test streaming output + prompts with the
    prompt_response written from within the in-flight exchange, release, and
    end_session (== shutdown)."""

    def _client(self):
        from tests._qt_stub import ensure_workflow_editor_importable
        ensure_workflow_editor_importable()
        from workflow_editor.pack_ops import bench_client as bc
        c = bc.BenchSessionClient(_proc_file(self), Path("/nonexistent"))

        def _start():            # THIS interpreter (no project venv in tests)
            c._bundle_dir = None
            c._proc = subprocess.Popen(
                [sys.executable, str(_RUNNER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
            c._dead = False
        c._start = _start
        self.addCleanup(c.kill)
        return c

    def test_end_session_is_shutdown(self):
        from tests._qt_stub import ensure_workflow_editor_importable
        ensure_workflow_editor_importable()
        from workflow_editor.pack_ops import bench_client as bc
        from workflow_editor.pack_ops.pack_parsers import RemoteSession
        self.assertIs(bc.BenchSessionClient.end_session, RemoteSession.shutdown)

    def test_manual_flow_acquire_run_release_end(self):
        c = self._client()
        claims = [
            {"test_id": "BENCH-Q", "local_name": "PSU1", "etype": "psu",
             "bench": {"remote": False}},
            {"test_id": "BENCH-Q", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}},
        ]
        acq = c.acquire_run(claims)
        self.assertTrue(acq.get("ok"), acq)
        self.assertEqual(acq["plan"]["endpoints"], [])
        self.assertEqual(sorted(acq["plan"]["manual"]),
                         ["BENCH-Q/DMM1", "BENCH-Q/PSU1"])
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        module = Path(td.name) / "test.py"
        module.write_text(
            _code("quick").replace("PSU1_REMOTE = True", "PSU1_REMOTE = False"),
            encoding="utf-8")
        results_dir = Path(td.name) / "results"
        outputs: list = []
        prompts: list = []

        def on_prompt(text):
            prompts.append(text)
            return "3.3" if "Read DC voltage" in text else ""

        res = c.run_test(acq["run_id"], module, results_dir,
                         on_output=outputs.append, on_prompt=on_prompt)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["result"]["overall"], "PASS")
        self.assertTrue(any(t.startswith("STEP 1 - ") for t in outputs))
        self.assertTrue(any("Read DC voltage" in t for t in prompts))
        # keep_state False + manual device: the SAFE-STATE prompt pass ran
        self.assertTrue(any("SAFE STATE" in t for t in prompts), prompts)
        data = json.loads((results_dir / "results.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(set(data["verdicts"].values()), {"PASS"})
        rel = c.release_run(acq["run_id"])
        self.assertTrue(rel.get("ok"), rel)
        out = c.end_session()
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(c.dead)

    def test_plan_only_holds_nothing_and_matches_acquire(self):
        # F-dryrun client seam: plan_only == acquire_run + dry_run over the
        # live channel; nothing held, and it still answers while a run
        # holds the bench.
        c = self._client()
        claims = [
            {"test_id": "T", "local_name": "PSU1", "etype": "psu",
             "bench": {"remote": False}}]
        dry = c.plan_only(claims)
        self.assertTrue(dry.get("ok"), dry)
        self.assertTrue(dry.get("dry_run"))
        self.assertNotIn("run_id", dry)
        acq = c.acquire_run(claims)
        self.assertTrue(acq.get("ok"), acq)
        self.assertEqual(dry["plan"], acq["plan"])
        dry2 = c.plan_only(claims)          # run active: dry-run still allowed
        self.assertTrue(dry2.get("ok"), dry2)
        self.assertEqual(dry2["plan"], acq["plan"])

    def test_daemon_dead_between_exchanges_is_session_dead_no_respawn(self):
        # A holder killed BETWEEN exchanges must surface SessionDead from
        # run_test so the host runs its crash-recovery safe-off (design
        # row 5) — the inherited lazy _start must NOT silently hand the run
        # to a fresh daemon holding nothing (S6 e2e finding).
        c = self._client()
        starts: list = []
        orig_start = c._start

        def counting_start():
            starts.append(1)
            orig_start()
        c._start = counting_start
        acq = c.acquire_run([
            {"test_id": "T", "local_name": "PSU1", "etype": "psu",
             "bench": {"remote": False}}])
        self.assertTrue(acq.get("ok"), acq)
        c._proc.kill()
        c._proc.wait(timeout=30)
        res = c.run_test(acq["run_id"], "whatever.py", "results")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("kind"), "SessionDead")
        self.assertEqual(len(starts), 1)      # no silent respawn
        self.assertTrue(c.dead)

    def test_acquire_after_daemon_death_is_session_dead_no_respawn(self):
        # Same invariant for the CONTROL frames: acquire_run rides the
        # inherited _request, whose lazy _start would silently hand the
        # frame to a FRESH daemon holding nothing — the
        # BenchSessionClient._request override must report the crash so the
        # host runs its recovery safe-off (design row 5) instead.
        c = self._client()
        starts: list = []
        orig_start = c._start

        def counting_start():
            starts.append(1)
            orig_start()
        c._start = counting_start
        claims = [{"test_id": "T", "local_name": "PSU1", "etype": "psu",
                   "bench": {"remote": False}}]
        acq = c.acquire_run(claims)
        self.assertTrue(acq.get("ok"), acq)
        c._proc.kill()
        c._proc.wait(timeout=30)
        res = c.acquire_run(claims)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("kind"), "SessionDead")
        self.assertIn("died between exchanges", res.get("error", ""))
        self.assertEqual(len(starts), 1)      # no silent respawn
        self.assertTrue(c.dead)


if __name__ == "__main__":
    unittest.main()
