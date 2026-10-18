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
           / "procedo" / "studio" / "llm" / "_execute_op_subprocess.py")
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
_ADDR_B2 = "TCPIP::10.0.0.6::inst0::INSTR"   # same endpoint as B, other spelling
_ADDR_C = "TCPIP0::10.0.0.7::INSTR"          # canonical: visa:tcpip::10.0.0.7
_EP_A = "visa:tcpip::10.0.0.5"
_EP_B = "visa:tcpip::10.0.0.6"
_EP_C = "visa:tcpip::10.0.0.7"


def _claim(test_id, name, visa=_ADDR_A, etype="psu", dev=None, **extra):
    bench = {"visa": visa, "_dev": dev or name, "channels": [1],
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
# Boundary health check (F-H6): probe + one rebuild for held endpoints.
# ---------------------------------------------------------------------------


def _kill_link(dev) -> None:
    """Dead physical link: the live *IDN? probe raises (the _RaisingFake
    idiom, applied to the ONE held instance's probe method)."""
    def boom(*a, **k):
        raise RuntimeError("VISA link dead")
    dev.query_raw = boom


class HealthCheckTests(_BenchBase):
    def _set_v(self, device, volts=5.0):
        return {"node_path": "s", "op": {
            "op": "psu.set_voltage", "device": device, "channel": 1,
            "volts": volts, "volts_unit": "V"}}

    def _held_acquire_release(self, s, claims, **kw):
        r = self._acquire(s, claims, **kw)
        self.assertTrue(r["ok"], r)
        self._dispatch(s, {"cmd": "release_run", "run_id": r["run_id"]})
        return r

    def test_healthy_probe_is_noop(self):
        s = self._session()
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")])
        self.assertTrue(r2["ok"], r2)
        self.assertNotIn("reconnected", r2)
        # probed once (live *IDN?), never rebuilt
        self.assertEqual(len(self._devcalls("PHYS", "query_raw")), 1)
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 1)
        self.assertFalse(any(c[2] == "close" for c in self.calls))

    def test_dead_held_endpoint_rebuilt_and_run_proceeds(self):
        s = self._session()
        self._held_acquire_release(s, [
            _claim("BENCH-Q", "PSU1", dev="PHYS"),
            {"test_id": "BENCH-Q", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}},
        ])
        _kill_link(s.held[_EP_A])
        r2 = self._acquire(s, [
            _claim("BENCH-Q", "PSU1", dev="PHYS"),
            {"test_id": "BENCH-Q", "local_name": "DMM1", "etype": "dmm",
             "bench": {"remote": False}},
        ])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["reconnected"], [_EP_A])
        # dead instance dropped (best-effort close), ONE fresh construction
        self.assertEqual(len(self._devcalls("PHYS", "close")), 1)
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 2)
        self.assertIn("re-established", " ".join(s.res.log))
        # the run proceeds on the rebuilt instance
        resp, stop = self._run(s, r2["run_id"], self._module(_code("quick")),
                               [_answer("3.3"), self._hold()])
        self.assertFalse(stop)
        self.assertEqual(resp["result"]["overall"], "PASS", resp)

    def test_rebuilt_identity_mismatch_refused_cache_untouched(self):
        idn_box = {"idn": "ACME PSU,SN1"}

        class _IdnFake(_Fake):
            def __init__(self, *a):
                super().__init__(*a)
                self.idn = idn_box["idn"]
        er._build_device = lambda bench, etype, lifecycle, log: _IdnFake(
            etype, bench.get("_dev", "?"), self.calls)
        s = self._session()
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self.assertEqual(s.bench.fingerprints, {_EP_A: "ACME PSU,SN1"})
        _kill_link(s.held[_EP_A])
        idn_box["idn"] = "ACME PSU,SN9"      # a DIFFERENT instrument answers
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")])
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "IdentityChanged")
        self.assertEqual(r2["endpoints"], [_EP_A])
        self.assertEqual(r2["changes"][_EP_A],
                         {"cached": "ACME PSU,SN1", "current": "ACME PSU,SN9"})
        # fingerprint cache untouched; rebuilt instance held; no run created
        self.assertEqual(s.bench.fingerprints[_EP_A], "ACME PSU,SN1")
        self.assertIn(_EP_A, s.held)
        self.assertIsNone(s.bench.active)

    def test_rebuild_failure_runs_gone_ladder(self):
        s = self._session()
        claims = [_claim("T-A", "PSU1", _ADDR_A, dev="P-A"),
                  _claim("T-A", "PSU2", _ADDR_B, dev="P-B")]
        self._held_acquire_release(s, claims)
        _kill_link(s.held[_EP_A])
        er._build_device = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("no route to host"))
        resp, stop = self._dispatch(s, {"cmd": "acquire_run", "claims": claims})
        self.assertTrue(stop)                       # respond then STOP
        self.assertFalse(resp["ok"])
        self.assertTrue(resp["session_ended"])
        self.assertIn("gone", resp["error"])
        self.assertIn("restart the bench session", resp["error"])
        # errored ladder: remaining held equipment safed-off and closed
        self.assertTrue(any(c[1] == "P-B" for c in self._offs()))
        self.assertEqual(len(self._devcalls("P-B", "close")), 1)
        self.assertFalse(s.held)

    def test_state_lost_power_endpoint_keep_state_on(self):
        s = self._session()
        claims = [_claim("T-A", "PSU1", dev="PHYS")]
        self._held_acquire_release(s, claims, keep_state=True)
        _kill_link(s.held[_EP_A])
        r2 = self._acquire(s, claims, keep_state=True)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "StateLost")
        self.assertEqual(r2["endpoints"], [_EP_A])
        self.assertIn("keep-state", r2["error"])
        # rebuilt instance stays held, session alive, no run created
        self.assertIn(_EP_A, s.held)
        self.assertIsNone(s.bench.active)
        # the incident fires ONCE: the operator's re-run proceeds
        r3 = self._acquire(s, claims, keep_state=True)
        self.assertTrue(r3["ok"], r3)
        self.assertNotIn("reconnected", r3)

    def test_state_lost_not_for_non_power_endpoint(self):
        s = self._session()
        claims = [_claim("T-A", "SCOPE1", _ADDR_B, etype="scope", dev="SC")]
        self._held_acquire_release(s, claims, keep_state=True)
        _kill_link(s.held[_EP_B])
        r2 = self._acquire(s, claims, keep_state=True)
        self.assertTrue(r2["ok"], r2)               # scope: no off action
        self.assertEqual(r2["reconnected"], [_EP_B])

    def test_keep_state_off_power_rebuild_proceeds(self):
        s = self._session()
        claims = [_claim("T-A", "PSU1", dev="PHYS")]
        self._held_acquire_release(s, claims)       # keep_state OFF (default)
        _kill_link(s.held[_EP_A])
        r2 = self._acquire(s, claims)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["reconnected"], [_EP_A])

    def test_per_step_equipment_never_probed(self):
        s = self._session()
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        dead = s.held[_EP_A]
        _kill_link(dead)
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")],
                           policies={"T-B": {"PSU1": "per_step"}})
        self.assertTrue(r2["ok"], r2)               # a probe would have laddered
        self.assertNotIn("reconnected", r2)
        self.assertEqual(r2["plan"]["per_step"], [_EP_A])
        self.assertIs(s.held[_EP_A], dead)          # untouched, never rebuilt
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 1)

    def test_guided_reuse_probes_and_rebuilds(self):
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        _kill_link(s.held[_EP_A])
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A2,
                                                        "_dev": "GUIDED"}}})
        self.assertFalse(r["aborted"], r)
        # rebuilt from the STORED admission bench, not the guided op's dict
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 0)
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 2)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 1)
        self.assertTrue(any("re-established" in ln for ln in r["log"]), r)

    def test_guided_reuse_state_lost_refusal_then_proceeds(self):
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")],
                                   keep_state=True)
        _kill_link(s.held[_EP_A])
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                        "_dev": "GUIDED"}}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "StateLost")
        self.assertEqual(r["endpoints"], [_EP_A])
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 0)
        # rebuilt instance stays held: the next batch probes healthy and runs
        r2 = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                  "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                         "_dev": "GUIDED"}}})
        self.assertFalse(r2["aborted"], r2)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 1)

    def test_safe_off_dead_held_rebuilds_and_offs_never_refuses(self):
        # A SAFETY action never refuses: keep_state ON + a dead held POWER
        # endpoint at guided safe-off time -> probe + ONE rebuild (repairing
        # the link in order to turn outputs off is exactly right), then the
        # off runs on the rebuilt instance.  No StateLost, no unsafe entry.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")],
                                   keep_state=True)
        _kill_link(s.held[_EP_A])
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED"}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("unsafe", r)
        self.assertNotIn("StateLost", json.dumps(r))
        # ONE rebuild from the STORED admission bench; the off landed on it
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 2)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 0)
        self.assertEqual([c[1] for c in self._offs()], ["PHYS"])
        self.assertTrue(any("re-established" in ln for ln in r["log"]), r)

    def test_safe_off_rebuild_failure_lands_in_unsafe(self):
        # Rebuild FAILS at safe-off time -> the target lands in ``unsafe``
        # exactly as the pre-F-H6 dead-link off failure did (the host latches
        # UNSAFE) — never swallowed as a log-only line.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        _kill_link(s.held[_EP_A])
        er._build_device = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("no route to host"))
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED"}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["unsafe"], ["PSU1 CH1"])
        self.assertTrue(any("UNSAFE" in ln for ln in r["log"]), r)

    def test_exec_refusal_carries_prior_batch_results(self):
        # A StateLost refusal mid-batch returns the results/log of the ops
        # that ALREADY executed (a re-run must not re-execute state-changing
        # ops), plus the typed refusal for the endpoint that ran nothing.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"},
                                          {"id": "PSU2", "type": "psu"}]}
        self._held_acquire_release(
            s, [_claim("T-A", "PSU2", _ADDR_B, dev="PHYS-B")], keep_state=True)
        _kill_link(s.held[_EP_B])
        meas = {"node_path": "a", "op": {"op": "psu.measure_voltage",
                                        "device": "PSU1", "channel": 1,
                                        "ref": 7}}
        r = er._cmd_exec_ops(s, {
            "ops": [meas, self._set_v("PSU2")],
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED-A"},
                          "PSU2": {"visa": _ADDR_B, "_dev": "GUIDED-B"}}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "StateLost")
        self.assertEqual(r["endpoints"], [_EP_B])
        # op A's executed result rides along with the refusal
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["node_path"], "a")
        self.assertTrue(r["results"][0]["ok"])
        self.assertTrue(r["results"][0]["has_value"])
        # the refused endpoint itself ran nothing
        self.assertEqual(len(self._devcalls("PHYS-B", "set_voltage")), 0)

    def test_raw_refusal_typed_kind_no_class_name_leak(self):
        # A health refusal on the raw path surfaces as its typed kind with a
        # clean message — never "ExecError: HealthRefusal: ...".
        idn_box = {"idn": "ACME PSU,SN1"}

        class _IdnFake(_Fake):
            def __init__(self, *a):
                super().__init__(*a)
                self.idn = idn_box["idn"]
        er._build_device = lambda bench, etype, lifecycle, log: _IdnFake(
            etype, bench.get("_dev", "?"), self.calls)
        s = self._session()
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        _kill_link(s.held[_EP_A])
        idn_box["idn"] = "ACME PSU,SN9"      # a DIFFERENT instrument answers
        r = er._cmd_raw(s, {"device": "PSU1", "etype": "psu", "subtype": "",
                            "text": "MEAS:VOLT?",
                            "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                   "_dev": "GUIDED"}}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "IdentityChanged")
        self.assertNotIn("HealthRefusal", json.dumps(r))
        self.assertIn("identity changed", r["error"])
        self.assertIn(_EP_A, s.held)         # rebuilt instance stays held

    def test_raw_dead_held_rebuilds_and_sends_no_state_lost(self):
        # Raw context: probe + rebuild are welcome; StateLost never fires
        # (keep_state semantics do not apply to a terminal line).
        s = self._session()
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")],
                                   keep_state=True)
        _kill_link(s.held[_EP_A])
        r = er._cmd_raw(s, {"device": "PSU1", "etype": "psu", "subtype": "",
                            "text": "MEAS:VOLT?",
                            "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                   "_dev": "GUIDED"}}})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("StateLost", json.dumps(r))
        self.assertEqual(r["response"], "1.23")
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 2)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 0)


# ---------------------------------------------------------------------------
# Unconfirmed-off memory (R2 closure): a failed off is never forgotten.
# ---------------------------------------------------------------------------


class UnconfirmedOffTests(_BenchBase):
    """Reviewer chain R2: a safe-off whose rebuild FAILED dropped the
    endpoint out of ``held`` (check_held closes the dead instance before
    reconnecting), and the held-only shutdown sweep then answered
    false-clean — so the host's ``end()`` cleared the persistent UNSAFE
    latch on an off that was never confirmed.  The daemon now records every
    failed off at its failure site (``unconfirmed_off``) and every later
    safe-off pass retries from the stored admission bench; shutdown lists a
    still-unconfirmed target even when nothing holds it, and reports clean
    only once the off genuinely ran."""

    def _held_acquire_release(self, s, claims, **kw):
        r = self._acquire(s, claims, **kw)
        self.assertTrue(r["ok"], r)
        self._dispatch(s, {"cmd": "release_run", "run_id": r["run_id"]})
        return r

    def _fail_safe_off(self, s):
        """The reviewer's steps 1-2: dead link + failing rebuild at safe-off
        time -> unsafe response, endpoint gone from held, memory keeps it."""
        _kill_link(s.held[_EP_A])
        er._build_device = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("no route to host"))
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED"}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["unsafe"], ["PSU1 CH1"])
        self.assertFalse(s.held)                    # handle dropped...
        self.assertEqual(set(s.unconfirmed_off), {_EP_A})  # ...memory holds it
        return r

    def test_rebuild_failure_recorded_shutdown_still_unsafe(self):
        # Steps 1-3 of the reviewer chain: after the failed off the endpoint
        # is held by NOTHING, yet shutdown must not answer false-clean — the
        # unconfirmed target is retried (reconnect still failing) and STILL
        # lands in shutdown's ``unsafe`` list.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._fail_safe_off(s)
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["unsafe"], [f"{_EP_A} CH1"])
        self.assertTrue(any("retry reconnect" in ln for ln in resp["log"]), resp)

    def test_retry_reconnects_from_stored_bench_then_shutdown_clean(self):
        # Link restored before the NEXT safe-off pass: the retry reconstructs
        # the endpoint from the STORED admission bench (dev PHYS — not this
        # frame's bench_map), confirms the off, removes the record, and the
        # following shutdown is then LEGITIMATELY clean.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._fail_safe_off(s)
        calls = self.calls
        er._build_device = lambda bench, etype, lifecycle, log: _Fake(
            etype, bench.get("_dev", bench.get("visa", "?")), calls)
        r2 = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED2"}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r2["ok"], r2)
        self.assertNotIn("unsafe", r2)
        self.assertNotIn("unsafe_targets", r2)  # clean == neither form
        self.assertEqual(s.unconfirmed_off, {})
        # reconnect-retry path: the target was NOT held — rebuilt from the
        # stored admission bench, never from the frame's own bench_map
        self.assertEqual(len(self._devcalls("GUIDED2", "connect")), 0)
        self.assertEqual(len(self._devcalls("PHYS", "connect")), 2)
        self.assertTrue(any(c[1] == "PHYS" for c in self._offs()))
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertNotIn("unsafe", resp)

    def test_failed_off_while_held_recorded_then_confirmed_by_teardown(self):
        # The plain (no-rebuild) failure site: the off SCPI itself errors on
        # a healthy held endpoint -> recorded; the device stays held, and the
        # shutdown sweep's successful off CONFIRMS and removes the record.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        held = s.held[_EP_A]

        def boom(*a, **k):
            raise RuntimeError("SCPI error")
        held.output = boom
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED"}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["unsafe"], ["PSU1 CH1"])
        self.assertIn(_EP_A, s.held)                # still held (no drop)
        self.assertEqual(set(s.unconfirmed_off), {_EP_A})
        del held.output                             # instrument answers again
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertNotIn("unsafe", resp)
        self.assertEqual(s.unconfirmed_off, {})

    def test_residual_channel_outside_declared_still_reported(self):
        # Backstop: a STILL-HELD device with a recorded channel outside its
        # declared set is neither swept (held sweep drives declared channels
        # only) nor retried (the retry set excludes declared devices) —
        # shutdown must still report it, never answer false-clean.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        er._record_unconfirmed(s, _EP_A, "psu", {2})
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertIn(f"{_EP_A} CH2", resp.get("unsafe") or [], resp)
        # R3: the structured record pins the reviewer composition — claim
        # CH1, residual CH2 — endpoint-keyed with reconnect data, so the
        # host recovery can drive the EXACT channel the claims never named.
        (rec,) = resp["unsafe_targets"]
        self.assertEqual((rec["device"], rec["etype"], rec["channels"]),
                         (_EP_A, "psu", [2]))
        self.assertTrue(rec["bench"], rec)  # stored admission bench

    def test_unsafe_targets_structured_snapshot_rides_unsafe(self):
        # Every response carrying ``unsafe`` also carries its structured
        # twin ``unsafe_targets`` (module docstring contract): the
        # unconfirmed-off memory as endpoint/etype/channels/bench records —
        # what the host recovery consumes instead of parsing the strings.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        self._held_acquire_release(s, [_claim("T-A", "PSU1", dev="PHYS")])
        r = self._fail_safe_off(s)
        (rec,) = r["unsafe_targets"]
        self.assertEqual(rec["device"], _EP_A)
        self.assertEqual(rec["etype"], "psu")
        self.assertEqual(rec["channels"], [1])
        self.assertEqual(rec["bench"]["_dev"], "PHYS")  # ADMISSION bench
        # ...and shutdown's backstop report carries the same records
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        (rec2,) = resp["unsafe_targets"]
        self.assertEqual((rec2["device"], rec2["channels"]), (_EP_A, [1]))

    def test_unsafe_targets_bench_none_when_never_stored(self):
        # No admission data was ever stored for the target: the record says
        # so explicitly (bench=None) — the host keeps its latch rather than
        # guess a reconnect route.
        s = self._session()
        er._record_unconfirmed(s, _EP_B, "psu", {2})
        resp = er._attach_unsafe(s, {"ok": True}, [f"{_EP_B} CH2"])
        self.assertEqual(resp["unsafe"], [f"{_EP_B} CH2"])
        (rec,) = resp["unsafe_targets"]
        self.assertEqual((rec["device"], rec["channels"], rec["bench"]),
                         (_EP_B, [2], None))


# ---------------------------------------------------------------------------
# Multi-channel power contract (round-4 High): the claim's DECLARED channel
# set — the new INVENTORY "channels" list — reaches the daemon's sweeps.
# ---------------------------------------------------------------------------


class MultiChannelContractTests(_BenchBase):
    """New-contract inventories carry ``"channels": [1, 2]`` next to the
    singular ``"channel"``; ``_claim_channels`` reads both through the
    wheel's ONE normalizer (``bench_endpoints.channels_of``) so the union
    lands in ``BenchState.channels`` and every declared channel is swept."""

    def test_claim_channels_new_and_old_shapes(self):
        cc = bsr._claim_channels
        # New contract: dict entries (codegen refs) and bare ints.
        self.assertEqual(cc({"channels": [{"channel": 1}, {"channel": 2}]}),
                         {1, 2})
        self.assertEqual(cc({"channels": [2, 3]}), {2, 3})
        # Old generated tests: singular only.
        self.assertEqual(cc({"channel": 2}), {2})
        # Both present: union (an operator-edited singular outside the
        # declared list must stay in the safety set).
        self.assertEqual(cc({"channels": [1, 2], "channel": 3}), {1, 2, 3})
        # Nothing usable: the conventional CH1 default.
        self.assertEqual(cc({}), {1})

    def test_acquire_unions_declared_channels_into_state(self):
        s = self._session()
        r = self._acquire(s, [_claim(
            "T-A", "PSU1", dev="PHYS",
            channels=[{"channel": 1}, {"channel": 2}])])
        self.assertTrue(r["ok"], r)
        self.assertEqual(s.bench.channels[_EP_A], {1, 2})
        self._dispatch(s, {"cmd": "release_run", "run_id": r["run_id"]})

    def test_shutdown_sweeps_every_declared_claim_channel(self):
        # The reviewer probe, daemon side: CH1+CH2 declared, nothing ran —
        # the shutdown sweep must still drive BOTH channels off.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r = self._acquire(s, [_claim(
            "T-A", "PSU1", dev="PHYS",
            channels=[{"channel": 1}, {"channel": 2}])])
        self.assertTrue(r["ok"], r)
        self._dispatch(s, {"cmd": "release_run", "run_id": r["run_id"]})
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertTrue(resp["ok"], resp)
        self.assertNotIn("unsafe", resp)
        off_channels = {c[3].get("channel")
                        for c in self._offs() if c[1] == "PHYS"}
        self.assertEqual(off_channels, {1, 2})


# ---------------------------------------------------------------------------
# Sticky IdentityChanged (F-H6): an unresolved mismatch never silently passes.
# ---------------------------------------------------------------------------


class StickyIdentityTests(_BenchBase):
    """After a rebuild lands on a DIFFERENT instrument, the mismatch is
    recorded per endpoint and EVERY refusing boundary (guided op, raw line,
    acquire_run, run_test) keeps returning the typed IdentityChanged —
    a healthy liveness probe of the impostor can never clear it.  Only
    end_session (a fresh daemon BenchState) forgets the flag; safe_off
    bypasses it and offs whatever answers."""

    def setUp(self):
        super().setUp()
        self.idn_box = idn_box = {"idn": "ACME PSU,SN1"}
        calls = self.calls

        class _IdnFake(_Fake):
            def __init__(self, *a):
                super().__init__(*a)
                self.idn = idn_box["idn"]
        er._build_device = lambda bench, etype, lifecycle, log: _IdnFake(
            etype, bench.get("_dev", "?"), calls)

    def _set_v(self, device, volts=5.0):
        return {"node_path": "s", "op": {
            "op": "psu.set_voltage", "device": device, "channel": 1,
            "volts": volts, "volts_unit": "V"}}

    def _guided(self, s):
        return er._cmd_exec_ops(s, {
            "ops": [self._set_v("PSU1")],
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED"}}})

    def _flag(self, s):
        """Acquire+release PSU1, kill the link, swap the instrument, then a
        guided op takes the FIRST IdentityChanged (the rebuild boundary)."""
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self.assertTrue(r["ok"], r)
        self._dispatch(s, {"cmd": "release_run", "run_id": r["run_id"]})
        _kill_link(s.held[_EP_A])
        self.idn_box["idn"] = "ACME PSU,SN9"   # a DIFFERENT instrument answers
        first = self._guided(s)
        self.assertEqual(first["kind"], "IdentityChanged")

    def test_guided_second_call_still_refused_nothing_executes(self):
        # The reviewer's probe: first call refused, SECOND call must NOT
        # succeed — the impostor answers *IDN? "healthy", but the recorded
        # mismatch refuses before any probe, and set_voltage never runs.
        s = self._session()
        self._flag(s)
        r2 = self._guided(s)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "IdentityChanged")
        self.assertEqual(r2["endpoints"], [_EP_A])
        self.assertEqual(r2["changes"][_EP_A],
                         {"cached": "ACME PSU,SN1", "current": "ACME PSU,SN9"})
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 0)
        self.assertEqual(s.bench.fingerprints[_EP_A], "ACME PSU,SN1")

    def test_raw_second_call_still_refused(self):
        s = self._session()
        self._flag(s)
        r2 = er._cmd_raw(s, {"device": "PSU1", "etype": "psu", "subtype": "",
                             "text": "MEAS:VOLT?",
                             "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                    "_dev": "GUIDED"}}})
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "IdentityChanged")
        self.assertNotIn("HealthRefusal", json.dumps(r2))
        # refused BEFORE the probe: the impostor never saw a single query
        self.assertEqual(len(self._devcalls("PHYS", "query_raw")), 0)
        self.assertEqual(s.bench.fingerprints[_EP_A], "ACME PSU,SN1")

    def test_acquire_run_on_flagged_endpoint_refused(self):
        s = self._session()
        self._flag(s)
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")])
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "IdentityChanged")
        self.assertEqual(r2["endpoints"], [_EP_A])
        self.assertIsNone(s.bench.active)          # no run created
        self.assertEqual(s.bench.fingerprints[_EP_A], "ACME PSU,SN1")

    def test_run_test_on_stale_handle_refused_after_flag(self):
        # A run handle acquired BEFORE the boundary flagged its endpoint
        # must not execute on the unverified instrument.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r = self._acquire(s, [_claim("BENCH-Q", "PSU1", dev="PHYS")])
        self.assertTrue(r["ok"], r)
        _kill_link(s.held[_EP_A])
        self.idn_box["idn"] = "ACME PSU,SN9"
        g = self._guided(s)
        self.assertEqual(g["kind"], "IdentityChanged")
        resp, stop = self._dispatch(s, {
            "cmd": "run_test", "run_id": r["run_id"],
            "module_path": "unused.py", "results_dir": self.tmp.name})
        self.assertFalse(stop)                     # benign refusal, no ladder
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["kind"], "IdentityChanged")
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 0)
        self.assertIn(_EP_A, s.held)               # session untouched

    def test_safe_off_bypasses_flag_and_offs_the_impostor(self):
        # A SAFETY action ignores the sticky flag: whatever instrument
        # answers at the endpoint gets its outputs driven off — and the
        # flag survives (safe-off is not a resolution).
        s = self._session()
        self._flag(s)
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED"}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("unsafe", r)
        self.assertEqual([c[1] for c in self._offs()], ["PHYS"])
        self.assertIn(_EP_A, s.bench.identity_mismatch)
        r2 = self._guided(s)                       # still refused after
        self.assertEqual(r2["kind"], "IdentityChanged")

    def test_flag_dies_with_session_matching_identity_proceeds(self):
        s = self._session()
        self._flag(s)
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)                      # end_session == shutdown
        # fresh daemon session; the RIGHT instrument answers again
        s2 = self._session()
        self.idn_box["idn"] = "ACME PSU,SN1"
        r = self._acquire(s2, [_claim("T-C", "PSU1", dev="PHYS")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["identities"][_EP_A], "ACME PSU,SN1")
        self.assertEqual(s2.bench.identity_mismatch, {})


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

    # -- stop-at-first-fail options ride the frame, set as ATTRIBUTES ------
    _RECORDER = (
        "import json, pathlib\n"
        "BENCH_SESSION_API = 1\n"
        "def main(ctx):\n"
        "    pathlib.Path(ctx.results_dir, 'seen.json').write_text(json.dumps(\n"
        "        [ctx.stop_at_first_fail, ctx.switch_off_on_stop]))\n"
        "    return {'overall': 'PASS', 'aborted': False, 'error': False}\n")

    def _seen(self, extra, ctx_class=None):
        s = self._session()
        rid = self._acquired(s)
        self._reader([])
        req = {"cmd": "run_test", "run_id": rid,
               "module_path": str(self._module(self._RECORDER)),
               "results_dir": self.tmp.name, **extra}
        if ctx_class is None:
            resp, _ = self._dispatch(s, req)
        else:
            from unittest import mock
            with mock.patch("rules_packager_base.execution_context."
                            "ExecutionContext", ctx_class):
                resp, _ = self._dispatch(s, req)
        return resp, json.loads(
            (Path(self.tmp.name) / "seen.json").read_text(encoding="utf-8"))

    def test_stop_at_first_fail_frame_sets_ctx(self):
        self.assertEqual(self._seen({"stop_at_first_fail": True})[1],
                         [True, True])

    def test_stop_at_first_fail_absent_is_off(self):
        self.assertEqual(self._seen({})[1], [False, True])

    def test_switch_off_on_stop_false_frame_sets_ctx(self):
        self.assertEqual(self._seen({"switch_off_on_stop": False})[1],
                         [False, False])

    def test_old_wheel_context_without_the_fields_does_not_raise(self):
        # An older bundle's ExecutionContext has no such fields and its
        # __init__ rejects unknown kwargs: the daemon must set attributes.
        class Old:
            def __init__(self, devices, owned, keep_state, results_dir,
                         prompt, cancelled):
                self.results_dir = results_dir

            def install(self):
                pass

            def restore(self):
                pass

        resp, seen = self._seen({"stop_at_first_fail": True,
                                 "switch_off_on_stop": False}, ctx_class=Old)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(seen, [True, False])

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
# Per-run timeout retargeting (capability-gated pack LIFECYCLE data).
# ---------------------------------------------------------------------------

_CAP = {"method": "set_timeout", "field": "timeout_ms"}


class _TimeoutFake(_Fake):
    """_Fake plus a REAL ``set_timeout`` recording its positional value both
    per instance and in the shared calls list (the ``__getattr__`` recorder
    keeps kwargs only, and the holder calls positionally)."""

    def __init__(self, *a):
        super().__init__(*a)
        self.timeouts: list = []

    def set_timeout(self, value):
        self.timeouts.append(value)
        self._calls.append((self._e, self._d, "set_timeout", {"value": value}))


class TimeoutRetargetTests(_BenchBase):
    """Design tests 1-8 for the two retarget seams: verbatim native-unit
    application at acquire, no-op without the capability, non-fatal failure,
    redundant-skip, the guided seam, safety-path exemption, rebuild
    invalidation, and the sticky-mismatch ordering guarantee."""

    def setUp(self):
        super().setUp()
        calls = self.calls
        er._build_device = lambda bench, etype, lifecycle, log: _TimeoutFake(
            etype, bench.get("_dev", bench.get("visa", "?")), calls)

    def _session(self, cap=True):
        """Session whose psu lifecycle entry deterministically HAS (or has
        NOT) the capability — independent of the installed pack version.
        Registry inner dicts are never mutated (get_lifecycle is shallow)."""
        s = super()._session()
        s.lifecycle = dict(s.lifecycle)
        psu = {k: v for k, v in (s.lifecycle.get("psu") or {}).items()
               if k != "retarget_timeout"}
        if cap:
            psu["retarget_timeout"] = dict(_CAP)
        s.lifecycle["psu"] = psu
        return s

    def _retargets(self, dev=None):
        return [c for c in self.calls
                if c[2] == "set_timeout" and (dev is None or c[1] == dev)]

    def _set_v(self, device, volts=5.0):
        return {"node_path": "s", "op": {
            "op": "psu.set_voltage", "device": device, "channel": 1,
            "volts": volts, "volts_unit": "V"}}

    def _release(self, s, resp):
        self._dispatch(s, {"cmd": "release_run", "run_id": resp["run_id"]})

    def test_acquire_applies_merged_value_verbatim(self):
        # Test 1: the run's MAX-merged timeout_ms reaches the held instance
        # verbatim (native ms, no unit conversion), once, with a log line.
        s = self._session()
        r = self._acquire(s, [
            _claim("T-A", "PSU1", _ADDR_A, dev="PHYS", timeout_ms=3000),
            _claim("T-B", "MAIN", _ADDR_A2, dev="PHYS", timeout_ms=5000),
        ])
        self.assertTrue(r["ok"], r)
        rt = self._retargets("PHYS")
        self.assertEqual(len(rt), 1)
        self.assertEqual(rt[0][3]["value"], 5000)       # max-merged, VERBATIM
        self.assertIsInstance(rt[0][3]["value"], int)   # native unit, unscaled
        self.assertEqual(s.bench.applied_timeout, {_EP_A: 5000})
        self.assertTrue(any(f"{_EP_A} timeout_ms -> 5000" in ln
                            and "T-A, T-B" in ln for ln in s.res.log), s.res.log)

    def test_capability_absent_is_noop(self):
        # Test 2: no capability in the namespace -> never called, max stands.
        s = self._session(cap=False)
        r = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._retargets(), [])
        self.assertEqual(s.bench.applied_timeout, {})

    def test_malformed_capability_is_noop_not_fatal(self):
        # R2: register_lifecycle validates nothing — a non-dict truthy
        # capability must be a silent no-op, never an escaped AttributeError.
        s = self._session(cap=False)
        s.lifecycle["psu"] = {**s.lifecycle["psu"], "retarget_timeout": True}
        r = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._retargets(), [])

    def test_set_timeout_failure_is_nonfatal_and_retried_later(self):
        # Test 3: a raising set_timeout never kills the acquire; the failure
        # is logged, applied_timeout is NOT stocked, and a later boundary
        # retries instead of skipping.
        calls = self.calls

        class _Boom(_Fake):
            def set_timeout(self, value):
                calls.append((self._e, self._d, "set_timeout",
                              {"value": value}))
                raise RuntimeError("firmware refuses")

        er._build_device = lambda bench, etype, lifecycle, log: _Boom(
            etype, bench.get("_dev", "?"), calls)
        s = self._session()
        r = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self.assertTrue(r["ok"], r)                       # acquire SUCCEEDS
        self.assertEqual(len(self._retargets("PHYS")), 1)
        self.assertNotIn(_EP_A, s.bench.applied_timeout)  # not recorded
        self.assertTrue(any("timeout retarget failed, keeping current value"
                            in ln for ln in s.res.log), s.res.log)
        self._release(s, r)
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(len(self._retargets("PHYS")), 2)  # retried, not skipped

    def test_redundant_retarget_skipped(self):
        # Test 4: same value twice -> one driver call; a different value
        # retargets again.
        s = self._session()
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS")])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(len(self._retargets("PHYS")), 1)   # second skipped
        self._release(s, r2)
        r3 = self._acquire(s, [_claim("T-C", "PSU1", dev="PHYS",
                                      timeout_ms=8000)])
        self.assertTrue(r3["ok"], r3)
        self.assertEqual([c[3]["value"] for c in self._retargets("PHYS")],
                         [5000, 8000])

    def test_guided_op_retargets_with_op_bench_value(self):
        # Test 5: the _cmd_exec_ops per_session seam applies the OP's own
        # declared timeout to the held endpoint (and skips when redundant).
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        req = {"ops": [self._set_v("PSU1")],
               "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED",
                                      "timeout_ms": 1500}}}
        r = er._cmd_exec_ops(s, req)
        self.assertFalse(r["aborted"], r)
        self.assertEqual([c[3]["value"] for c in self._retargets("PHYS")],
                         [5000, 1500])
        self.assertEqual(s.bench.applied_timeout[_EP_A], 1500)
        self.assertEqual(len(self._devcalls("GUIDED", "connect")), 0)
        r2 = er._cmd_exec_ops(s, dict(req))                 # same value again
        self.assertFalse(r2["aborted"], r2)
        self.assertEqual(len(self._retargets("PHYS")), 2)   # skipped

    def test_absent_field_inherits_previous_retarget(self):
        # Accepted residual (b), pinned: a claim declaring NO timeout is a
        # no-op — the previous run's retarget stands, not the admission
        # default.
        s = self._session()
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        claim = _claim("T-B", "PSU1", dev="PHYS")
        del claim["bench"]["timeout_ms"]
        r2 = self._acquire(s, [claim])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(len(self._retargets("PHYS")), 1)   # no second call
        self.assertEqual(s.bench.applied_timeout[_EP_A], 5000)

    def test_zero_timeout_claim_is_noop(self):
        # R5: a verbatim 0 would mean non-blocking serial reads / instant
        # VISA TMO — falsy values are "no declared patience", a no-op.
        s = self._session()
        r = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS",
                                     timeout_ms=0)])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._retargets(), [])
        self.assertEqual(s.bench.applied_timeout, {})

    def test_safety_paths_never_retarget(self):
        # Test 6: _cmd_safe_off, the _retry_unconfirmed reconnect and the
        # shutdown teardown all run with whatever is applied — the spy's
        # set_timeout never fires on any of them.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        base = len(self._retargets())
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED",
                                   "timeout_ms": 250}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self._retargets()), base)
        # R3 consistency: a dropped handle leaves NO stale applied record,
        # and the _retry_unconfirmed reconnect does not retarget either.
        er._record_unconfirmed(s, _EP_A, "psu", {1})
        er._close_held_device(s, _EP_A)
        self.assertNotIn(_EP_A, s.bench.applied_timeout)
        r2 = er._cmd_safe_off(s, {"bench_map": {}, "safe_off": []})
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(len(self._retargets()), base)
        self.assertNotIn(_EP_A, s.bench.applied_timeout)
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertEqual(len(self._retargets()), base)

    def test_rebuild_invalidates_and_reapplies_same_value(self):
        # Test 7 (the reviewer's guided-rebuild case): after a check_held
        # rebuild the stale skip record is gone, so the SAME claim value is
        # re-applied to the NEW instance instead of being skipped while the
        # device sits at its constructor value.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        first = s.held[_EP_A]
        self.assertEqual(first.timeouts, [5000])
        _kill_link(first)
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                        "_dev": "GUIDED",
                                                        "timeout_ms": 5000}}})
        self.assertFalse(r["aborted"], r)
        rebuilt = s.held[_EP_A]
        self.assertIsNot(rebuilt, first)
        self.assertEqual(rebuilt.timeouts, [5000])      # RE-applied, not skipped
        self.assertEqual(s.bench.applied_timeout[_EP_A], 5000)

    def test_sticky_identity_mismatch_refuses_before_retarget(self):
        # Test 8: a fresh IdentityChanged and its sticky re-raise both refuse
        # BEFORE the seam — the unverified instrument never sees a retarget.
        idn_box = {"idn": "ACME,SN1"}
        calls = self.calls

        class _IdnTimeoutFake(_TimeoutFake):
            def __init__(self, *a):
                super().__init__(*a)
                self.idn = idn_box["idn"]

        er._build_device = lambda bench, etype, lifecycle, log: (
            _IdnTimeoutFake(etype, bench.get("_dev", "?"), calls))
        s = self._session()
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        _kill_link(s.held[_EP_A])
        idn_box["idn"] = "ACME,SN9"          # a DIFFERENT instrument answers
        base = len(self._retargets())
        r2 = self._acquire(s, [_claim("T-B", "PSU1", dev="PHYS",
                                      timeout_ms=8000)])
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["kind"], "IdentityChanged")
        self.assertEqual(len(self._retargets()), base)   # fresh mismatch
        r3 = self._acquire(s, [_claim("T-C", "PSU1", dev="PHYS",
                                      timeout_ms=8000)])
        self.assertFalse(r3["ok"])
        self.assertEqual(r3["kind"], "IdentityChanged")
        self.assertEqual(len(self._retargets()), base)   # sticky re-raise

    def test_readdressed_name_retargets_own_instance_not_stale_alias(self):
        # MAJOR pin: Seam B keys the retarget by the hold key _ensure_held
        # actually bound — NEVER the alias map (written at claim/route time,
        # never popped).  A name claimed at endpoint A by an earlier run and
        # re-addressed to un-held B by a guided op opens NAME-keyed; the
        # retarget lands on that instance, and A's instrument is untouched.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        self._release(s, r1)
        self.assertEqual(s.bench.alias, {"PSU1": _EP_A})  # stale for B below
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_B,
                                                        "_dev": "GUIDED-B",
                                                        "timeout_ms": 1500}}})
        self.assertFalse(r["aborted"], r)
        self.assertIn("PSU1", s.held)                     # name-keyed at B
        self.assertEqual([c[3]["value"] for c in self._retargets("GUIDED-B")],
                         [1500])                          # B's OWN instance
        self.assertEqual([c[3]["value"] for c in self._retargets("PHYS")],
                         [5000])                          # A: acquire only
        self.assertEqual(s.bench.applied_timeout.get("PSU1"), 1500)
        self.assertEqual(s.bench.applied_timeout.get(_EP_A), 5000)

    def test_guided_op_never_retargets_while_run_active(self):
        # MINOR pin (chosen rule, documented at Seam B): while ANY run is
        # active the run's Seam-A values stand — a guided op routed onto a
        # held endpoint does NOT re-time it mid-run; the same op's declared
        # value applies once the run is released.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r1 = self._acquire(s, [_claim("T-A", "PSU1", dev="PHYS")])
        req = {"ops": [self._set_v("PSU1")],
               "bench_map": {"PSU1": {"visa": _ADDR_A, "_dev": "GUIDED",
                                      "timeout_ms": 1500}}}
        r = er._cmd_exec_ops(s, req)
        self.assertFalse(r["aborted"], r)
        self.assertEqual([c[3]["value"] for c in self._retargets("PHYS")],
                         [5000])                          # Seam A only
        self.assertEqual(s.bench.applied_timeout[_EP_A], 5000)
        self._release(s, r1)
        r2 = er._cmd_exec_ops(s, dict(req))               # between runs
        self.assertFalse(r2["aborted"], r2)
        self.assertEqual([c[3]["value"] for c in self._retargets("PHYS")],
                         [5000, 1500])
        self.assertEqual(s.bench.applied_timeout[_EP_A], 1500)

    def test_guided_manual_session_never_instantiates_benchstate(self):
        # R4: the exec_ops seam must not create a BenchState in a pure
        # guided-manual session — and without one there is no retarget.
        s = self._session()
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        r = er._cmd_exec_ops(s, {"ops": [self._set_v("PSU1")],
                                 "bench_map": {"PSU1": {"visa": _ADDR_A,
                                                        "_dev": "GUIDED",
                                                        "timeout_ms": 1500}}})
        self.assertFalse(r["aborted"], r)
        self.assertIsNone(s.bench)
        self.assertEqual(self._retargets(), [])


# ---------------------------------------------------------------------------
# Hold resolution (owner ruling 2026-08-27): PSU1 for a given op is whatever
# its CURRENT address resolves to — _ensure_held folds the op's OWN bench
# dict to its canonical endpoint FIRST and binds the name to the instance
# that endpoint resolves to (sibling _hold_resolution.py).
# ---------------------------------------------------------------------------


class StaleNameHoldTests(_BenchBase):
    """Verifier pins for the re-addressed-name family: the op's driver calls
    land on the CURRENT device's instance, never a remembered one — plus the
    stale-instance fate (chokepoint close + R2 record under the OLD
    endpoint), the wheel-less fallback, and safe-off record semantics."""

    def setUp(self):
        super().setUp()
        calls = self.calls          # same retarget spy as TimeoutRetargetTests
        er._build_device = lambda bench, etype, lifecycle, log: _TimeoutFake(
            etype, bench.get("_dev", bench.get("visa", "?")), calls)

    def _session(self, cap=True):
        s = super()._session()      # deterministic capability (see retarget tests)
        s.lifecycle = dict(s.lifecycle)
        psu = {k: v for k, v in (s.lifecycle.get("psu") or {}).items()
               if k != "retarget_timeout"}
        if cap:
            psu["retarget_timeout"] = dict(_CAP)
        s.lifecycle["psu"] = psu
        s.procedure_json = {"equipment": [{"id": "PSU1", "type": "psu"}]}
        return s

    def _exec(self, s, visa, dev, timeout=1500):
        return er._cmd_exec_ops(s, {
            "ops": [{"node_path": "s", "op": {
                "op": "psu.set_voltage", "device": "PSU1", "channel": 1,
                "volts": 5.0, "volts_unit": "V"}}],
            "bench_map": {"PSU1": {"visa": visa, "_dev": dev,
                                   "timeout_ms": timeout}}})

    def test_readdressed_name_op_executes_on_own_instance_not_stale_ns(self):
        # MAJOR pin (the verifier's divergence scenario): PSU1 previously
        # routed onto held endpoint A, name-keyed at B in between — the op
        # re-addressed back to B must EXECUTE on B's instance (the hold key
        # Seam B retargets), never on A's instance left in the namespace
        # slot by the earlier routing.
        s = self._session()
        r1 = self._acquire(s, [_claim("T-A", "PSU1", _ADDR_A, dev="PHYS")])
        self.assertTrue(r1["ok"], r1)
        self._dispatch(s, {"cmd": "release_run", "run_id": r1["run_id"]})
        rb = self._exec(s, _ADDR_B, "GUIDED-B", 1500)   # name-keyed open at B
        self.assertFalse(rb["aborted"], rb)
        inst_b = s.held["PSU1"]
        ra = self._exec(s, _ADDR_A, "GUIDED-A", 5000)   # routed onto held A
        self.assertFalse(ra["aborted"], ra)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 1)
        r = self._exec(s, _ADDR_B, "GUIDED-B", 2500)    # re-addressed to B
        self.assertFalse(r["aborted"], r)
        self.assertIs(s.held["PSU1"], inst_b)           # reused, not reopened
        self.assertEqual(len(self._devcalls("GUIDED-B", "set_voltage")), 2)
        self.assertEqual(len(self._devcalls("PHYS", "set_voltage")), 1)
        self.assertIs(s.ns[er._safe_name("PSU1")], inst_b)
        self.assertEqual(inst_b.timeouts, [1500, 2500])  # retarget: SAME instance
        self.assertEqual(s.bench.applied_timeout.get("PSU1"), 2500)

    def test_readdressed_name_drops_stale_hold_via_chokepoint(self):
        # Stale-instance fate: a name-keyed hold whose name PROVABLY moved
        # (endpoint differs) is closed through _close_held_device — its
        # energized channel first recorded unconfirmed-off under the OLD
        # endpoint with the stored bench, so the next safety pass reconnects
        # and offs the old physical device (never false-clean).
        s = self._session()
        rb = self._exec(s, _ADDR_B, "GUIDED-B")
        self.assertFalse(rb["aborted"], rb)
        inst_b = s.held["PSU1"]
        rc = self._exec(s, _ADDR_C, "GUIDED-C")
        self.assertFalse(rc["aborted"], rc)
        self.assertIsNot(s.held["PSU1"], inst_b)
        self.assertEqual(len(self._devcalls("GUIDED-B", "close")), 1)
        self.assertEqual(len(self._devcalls("GUIDED-C", "set_voltage")), 1)
        self.assertEqual(len(self._devcalls("GUIDED-B", "set_voltage")), 1)
        self.assertIs(s.ns[er._safe_name("PSU1")], s.held["PSU1"])
        self.assertEqual(s.unconfirmed_off[_EP_B]["channels"], {1})
        self.assertEqual(s.unconfirmed_off[_EP_B]["bench"]["visa"], _ADDR_B)
        self.assertIsNone(s.bench)      # pure guided session: no BenchState
        r = er._cmd_safe_off(s, {"bench_map": {}, "safe_off": []})
        self.assertTrue(r["ok"], r)
        self.assertNotIn(_EP_B, s.unconfirmed_off)      # retried + confirmed
        self.assertEqual(len(self._devcalls("GUIDED-B", "connect")), 2)
        self.assertEqual(len([c for c in self.calls if c[1] == "GUIDED-B"
                              and c[3].get("on") is False]), 1)

    def test_same_endpoint_other_spelling_reuses_name_hold(self):
        # Staleness is judged on the CANONICAL fold, not the raw string: a
        # re-spelled address of the SAME endpoint reuses the held instance.
        s = self._session()
        self._exec(s, _ADDR_B, "GUIDED-B")
        inst_b = s.held["PSU1"]
        r = self._exec(s, _ADDR_B2, "GUIDED-B2")
        self.assertFalse(r["aborted"], r)
        self.assertIs(s.held["PSU1"], inst_b)
        self.assertEqual(len(self._devcalls("GUIDED-B", "connect")), 1)
        self.assertEqual(len(self._devcalls("GUIDED-B2", "connect")), 0)
        self.assertEqual(len(self._devcalls("GUIDED-B", "close")), 0)
        self.assertEqual(len(self._devcalls("GUIDED-B", "set_voltage")), 2)

    def test_wheel_less_fallback_keeps_name_keyed_reuse(self):
        # No canonical_endpoint (old wheel): staleness is unprovable — the
        # name-keyed hold is reused exactly as today, nothing closed.
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
        self._exec(s, _ADDR_B, "GUIDED-B")
        inst_b = s.held["PSU1"]
        r = self._exec(s, _ADDR_C, "GUIDED-C")          # re-address: unprovable
        self.assertFalse(r["aborted"], r)
        self.assertIs(s.held["PSU1"], inst_b)           # today's behavior
        self.assertEqual(len(self._devcalls("GUIDED-B", "close")), 0)
        self.assertEqual(len(self._devcalls("GUIDED-C", "connect")), 0)
        self.assertEqual(len(self._devcalls("GUIDED-B", "set_voltage")), 2)
        self.assertEqual(s.unconfirmed_off, {})

    def test_safe_off_reaches_recorded_device_after_readdress(self):
        # Safety semantics: a safe-off frame reaches the device its RECORD
        # points at (stored bench data).  With PSU1 re-addressed to C, a
        # record naming B's bench must off B — not the instance currently
        # answering to the name — and C's interrupted energized state is
        # recorded under ITS endpoint and swept at shutdown (never
        # false-clean).
        s = self._session()
        self._exec(s, _ADDR_B, "GUIDED-B")              # held at B, CH1 live
        rc = self._exec(s, _ADDR_C, "GUIDED-C")         # re-addressed: held at C
        self.assertFalse(rc["aborted"], rc)
        self.assertIn(_EP_B, s.unconfirmed_off)         # B's fate (test above)
        r = er._cmd_safe_off(s, {
            "bench_map": {"PSU1": {"visa": _ADDR_B, "_dev": "GUIDED-B",
                                   "timeout_ms": 1500}},
            "safe_off": [{"device": "PSU1", "etype": "psu", "channels": [1]}]})
        self.assertTrue(r["ok"], r)
        offs_b = [c for c in self.calls if c[1] == "GUIDED-B"
                  and c[3].get("on") is False]
        self.assertTrue(offs_b)                         # record's device reached
        self.assertEqual([c for c in self.calls if c[1] == "GUIDED-C"
                          and c[3].get("on") is False], [])
        self.assertIn(_EP_C, s.unconfirmed_off)         # C not silently forgotten
        resp, stop = self._dispatch(s, {"cmd": "shutdown"})
        self.assertTrue(stop)
        self.assertNotIn("unsafe", resp)
        self.assertEqual(len([c for c in self.calls if c[1] == "GUIDED-C"
                              and c[3].get("on") is False]), 1)
        self.assertEqual(s.unconfirmed_off, {})


# ---------------------------------------------------------------------------
# Real daemon over a pipe: EOF backstop + the BenchSessionClient channel.
# ---------------------------------------------------------------------------


class DaemonWireTests(unittest.TestCase):
    def test_eof_during_idle_reaches_teardown_backstop(self):
        # ping, then close stdin WITHOUT shutdown: the reader signals EOF
        # through the queue and the loop exits cleanly through _teardown.
        proc = subprocess.run([sys.executable, str(_RUNNER), "--role", "run"],
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
        from procedo.studio.pack_ops import bench_client as bc
        c = bc.BenchSessionClient(_proc_file(self), Path("/nonexistent"))

        def _start():            # THIS interpreter (no project venv in tests)
            c._bundle_dir = None
            c._proc = subprocess.Popen(
                [sys.executable, str(_RUNNER), "--role", "run"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
            c._dead = False
        c._start = _start
        self.addCleanup(c.kill)
        return c

    def test_end_session_is_shutdown(self):
        from tests._qt_stub import ensure_workflow_editor_importable
        ensure_workflow_editor_importable()
        from procedo.studio.pack_ops import bench_client as bc
        from procedo.studio.pack_ops.pack_parsers import RemoteSession
        self.assertIs(bc.BenchSessionClient.end_session, RemoteSession.shutdown)

    def test_run_test_frame_carries_the_options_only_when_non_default(self):
        from types import SimpleNamespace
        c = self._client()
        frames: list = []
        c._post = frames.append
        c._proc = SimpleNamespace(
            stdout=SimpleNamespace(readline=lambda: '{"ok": true}\n'),
            poll=lambda: None)
        c._alive = lambda: True
        for kw, extra in (
                ({}, {}),
                ({"stop_at_first_fail": True}, {"stop_at_first_fail": True}),
                ({"switch_off_on_stop": False}, {"switch_off_on_stop": False}),
                ({"stop_at_first_fail": True, "switch_off_on_stop": False},
                 {"stop_at_first_fail": True, "switch_off_on_stop": False})):
            frames.clear()
            c.run_test("r", "m.py", "res", **kw)
            self.assertEqual(frames, [{"cmd": "run_test", "run_id": "r",
                                       "module_path": "m.py",
                                       "results_dir": "res", **extra}])

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
