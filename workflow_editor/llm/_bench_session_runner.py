"""Bench-session holder machinery for the persistent daemon (S3).

Imported by ``_execute_op_subprocess.py`` as a PLAIN SIBLING module —
file-path invocation puts ``llm/`` on ``sys.path``, so ``import
_bench_session_runner`` resolves without touching the ``workflow_editor``
package (PySide6 is absent from project venvs).  The daemon passes its own
module-globals dict (``d``) into every entry point so this module reuses the
daemon's device machinery (``_ensure_held`` / ``_safe_off_all_held`` /
``_close_held`` / ``_teardown`` / ``_write_frame`` / ``_PROTO_OUT``) without a
package import cycle.

Concurrency (amended guardrail): EXACTLY ONE new thread — the stdin
:class:`Reader` — feeding ONE queue.  The daemon main loop and any in-run
nested waits (prompt / cancel polls) consume from that queue; nothing else is
concurrent.

Bench-session frames (requests ride the daemon's NDJSON protocol)
-----------------------------------------------------------------
``end_session`` is deliberately NOT a frame: it IS the existing ``shutdown``
(safe-off all held + close/unlock + exit).

  {"cmd": "acquire_run",
   "claims": [{"test_id", "local_name", "etype", "subtype"?, "bench": {...}}],
   "policies"?: {test_id: {local_name: "per_step"|"per_session"}},
   "profile_defaults"?: {etype: policy}, "keep_state"?: bool,
   "results_dir"?: str}
    -> {"ok": true, "run_id": str,
        "plan": {"endpoints": [{"endpoint","etype","policy","promoted",
                                "usages": [labels]}],
                 "manual": [labels], "per_step": [endpoints]},
        "identities": {endpoint: str}}          # from held drivers' .identity/.idn
    -> {"ok": false, "kind": "unsupported_bundle"} on an old wheel (no
       bench_endpoints in the project venv); "RunActive"; "EndpointConflict".

  {"cmd": "release_run", "run_id"} -> {"ok": true}   # devices STAY held

  {"cmd": "run_test", "run_id", "module_path", "results_dir"?}
    The daemon imports the test under a UNIQUE module name (a mid-session
    regeneration is never served from sys.modules), builds the holder-side
    ExecutionContext (devices from the run view, owned=False for held,
    keep_state from the run), wraps sys.stdout in ``{"kind":"output"}``
    frames (C-level VISA noise stays on stderr), then calls ``main(ctx=...)``.
    Streamed daemon->host frames before the final response:
      {"kind": "output", "text": line}
      {"kind": "prompt", "id": int, "text": str}      # correlated operator ask
      {"kind": "ack", "cmd": "cancel_run", ...}       # in-run cancel accepted
      {"kind": "reject", ...}                         # anything else in-run
    Final response: {"ok": true, "result": {run_id, overall, aborted, error,
    exception_type, exception_message, results_dir}, "unsafe"?: [...],
    "session_ended"?: true}.  Verdicts are read host-side from results.json.

  {"cmd": "cancel_run", "run_id"} -> sets the flag ``ctx.cancelled()`` reads
    (delivered at step boundaries / _sleep slices / prompt waits).  In-run it
    is consumed from the queue and acknowledged with an "ack" frame; idle it
    gets a normal response.

  {"cmd": "prompt_response", "id", "text"} -> resolves the pending prompt;
    with no prompt outstanding it is rejected (typed).

Failure ladder (keyed off the RETURNED Result — emitted ``main`` swallows its
own exceptions):
  res.error   -> safe-off ALL held + close + END session (respond, stop=True
                 like shutdown), ``unsafe`` surfaced.
  res.aborted -> ordered safe-off of held power equipment ONLY — session and
                 connections remain, devices stay held.
  escaped exception (backstop) -> same as res.error.
  plain PASS/FAIL -> nothing (host applies between-test behavior).
"""
from __future__ import annotations

import itertools
import json
import queue
import signal
import sys
import threading
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path

_EOF = object()          # queue sentinel: parent closed the pipe


# ---------------------------------------------------------------------------
# THE one reader thread + queue.
# ---------------------------------------------------------------------------


class Reader:
    """Daemon-side stdin reader: one thread pumping lines into one queue.

    Owns EOF detection (implementer note 2): EOF is signalled THROUGH the
    queue and RE-QUEUED on every consumption, so nested consumers (prompt
    waits, cancel drains) each observe it and the main loop still reaches
    the final ``_teardown`` backstop.
    """

    def __init__(self, stream) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._pump, args=(stream,), daemon=True,
            name="bench-stdin-reader")
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._q.put(line)
        except Exception:  # noqa: BLE001 — a dying pipe is EOF, not a crash
            pass
        self._q.put(_EOF)

    def get(self):
        """Blocking: next raw line, or the ``_EOF`` sentinel (re-queued)."""
        item = self._q.get()
        if item is _EOF:
            self._q.put(_EOF)
        return item

    def poll(self):
        """Non-blocking: next raw line, ``_EOF`` (re-queued), or None."""
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            return None
        if item is _EOF:
            self._q.put(_EOF)
        return item

    def next_line(self):
        """Main-loop accessor: blocking; ``None`` on EOF (mirrors readline)."""
        item = self.get()
        return None if item is _EOF else item


_READER: Reader | None = None


def start_reader(stream) -> Reader:
    """Create THE reader (called once from the daemon's ``main()``)."""
    global _READER
    _READER = Reader(stream)
    return _READER


# ---------------------------------------------------------------------------
# Per-session bench state (attached to ``_Session.bench`` on first use).
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    run_id: str
    keep_state: bool
    results_dir: str
    view: dict            # local equipment name -> canonical endpoint (held key)
    manual: tuple         # local names of manual (remote False) claims
    cancelled: bool = field(default=False)


class BenchState:
    """Holder-side bench-session state for one daemon ``_Session``.

    ``_Session.held`` itself is rekeyed by canonical endpoint for run-claimed
    equipment (guided-manual devices keep their name keys — behavior
    preserved); this object carries the run handles, the name->endpoint
    views, and the channel/alias unions the safe-off fallback needs.
    """

    def __init__(self) -> None:
        self.runs: dict = {}       # run_id -> _Run (acquired, not yet released)
        self.active: str | None = None   # the exclusive run holding the bench
        self.alias: dict = {}      # local name -> endpoint, union across ALL runs
        self.channels: dict = {}   # endpoint -> set(int), union across ALL runs
        self.run_seq = itertools.count(1)
        self.prompt_seq = itertools.count(1)

    def enter(self, session, device: str) -> str:
        """Hold key for a name-keyed op: the ACTIVE run's endpoint when the
        name is claimed onto a held endpoint; else the name itself (today's
        guided-manual semantics, preserved bit-for-bit)."""
        run = self.runs.get(self.active) if self.active else None
        key = run.view.get(device) if run is not None else None
        if key is not None and key in session.held:
            return key
        return device

    def channels_for(self, session, key: str) -> set:
        """UNION of channels seen for ``key`` across all runs this session
        (implementer note 3): claim-declared channels plus any channels a
        guided op energized through a name routed onto this endpoint."""
        out = set(self.channels.get(key, ()))
        for name, endpoint in self.alias.items():
            if endpoint == key:
                out |= set(session.energized.get(name, ()))
        out.discard(None)
        return out


def _state(session) -> BenchState:
    if session.bench is None:
        session.bench = BenchState()
    return session.bench


# ---------------------------------------------------------------------------
# Frame plumbing shared by the in-run waits.
# ---------------------------------------------------------------------------


def _emit(d: dict, obj: dict) -> None:
    """Write one daemon->host frame on the protocol channel (no-op when the
    channel is not wired, i.e. handler-level unit tests without frames)."""
    out = d.get("_PROTO_OUT")
    if out is not None:
        d["_write_frame"](out, obj)


def _parse_frame(d: dict, line: str):
    line = line.strip()
    if not line:
        return None
    try:
        req = json.loads(line)
    except Exception as exc:  # noqa: BLE001
        _emit(d, {"kind": "reject", "ok": False, "error": f"bad frame: {exc}"})
        return None
    return req if isinstance(req, dict) else None


def _handle_inrun_cancel(d: dict, st: BenchState, req: dict) -> None:
    rid = req.get("run_id")
    target = st.runs.get(rid)
    if target is None:
        _emit(d, {"kind": "reject", "ok": False, "cmd": "cancel_run",
                  "error": f"unknown run_id {rid!r}"})
        return
    target.cancelled = True
    _emit(d, {"kind": "ack", "cmd": "cancel_run", "ok": True, "run_id": rid})


_IN_RUN_REJECT = ("frame rejected: a test run is in progress "
                  "(only prompt_response/cancel_run are accepted)")


def _cancelled_poll(d: dict, st: BenchState, run: _Run) -> bool:
    """``ctx.cancelled``: drain frames the reader queued (cancel_run sets the
    flag; anything else is rejected), then report the flag.  Runs at every
    step boundary and inside every ``_sleep`` slice."""
    rd = _READER
    while rd is not None:
        item = rd.poll()
        if item is None:
            break
        if item is _EOF:
            run.cancelled = True   # host gone: stop the test; the main loop
            break                  # still sees the re-queued EOF -> _teardown
        req = _parse_frame(d, item)
        if req is None:
            continue
        cmd = req.get("cmd")
        if cmd == "cancel_run":
            _handle_inrun_cancel(d, st, req)
        elif cmd == "prompt_response":
            _emit(d, {"kind": "reject", "ok": False, "cmd": cmd,
                      "error": "no prompt outstanding"})
        else:
            _emit(d, {"kind": "reject", "ok": False, "cmd": cmd,
                      "error": _IN_RUN_REJECT})
    return run.cancelled


def _prompt_via_frames(d: dict, st: BenchState, run: _Run, text) -> str:
    """Holder-side ``ctx.prompt``: emit a correlated ``{"kind":"prompt"}``
    frame and BLOCK on the reader queue for its prompt_response.
    ``cancel_run`` during the wait aborts the test (OperatorStop — an abort,
    not an error); any other frame gets a typed rejection."""
    from rules_packager_base.execution_context import OperatorStop
    pid = next(st.prompt_seq)
    _emit(d, {"kind": "prompt", "id": pid, "text": str(text)})
    rd = _READER
    if rd is None:
        raise RuntimeError("no stdin reader; operator prompt unavailable")
    while True:
        if run.cancelled:
            raise OperatorStop("operator stop requested during a prompt")
        item = rd.get()
        if item is _EOF:
            run.cancelled = True
            raise OperatorStop("host closed the pipe during a prompt")
        req = _parse_frame(d, item)
        if req is None:
            continue
        cmd = req.get("cmd")
        if cmd == "prompt_response":
            if req.get("id") == pid:
                text_val = req.get("text")
                return "" if text_val is None else str(text_val)
            _emit(d, {"kind": "reject", "ok": False, "cmd": cmd,
                      "error": f"stale prompt_response (id {req.get('id')!r}, "
                               f"expected {pid})"})
            continue
        if cmd == "cancel_run":
            _handle_inrun_cancel(d, st, req)
            continue           # loop re-checks run.cancelled -> OperatorStop
        _emit(d, {"kind": "reject", "ok": False, "cmd": cmd,
                  "error": _IN_RUN_REJECT})


class _FrameStdout:
    """File-like sys.stdout stand-in for the run duration: each complete line
    becomes an ``{"kind":"output"}`` frame (precedent: progress frames).
    C-level VISA noise still lands on fd 1 -> stderr (daemon fd isolation)."""

    def __init__(self, d: dict) -> None:
        self._d = d
        self._buf = ""

    def write(self, s) -> int:
        s = str(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            _emit(self._d, {"kind": "output", "text": line})
        return len(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self._buf:
            _emit(self._d, {"kind": "output", "text": self._buf})
            self._buf = ""


# ---------------------------------------------------------------------------
# Command handlers.
# ---------------------------------------------------------------------------


def _claim_channels(bench) -> set:
    """Channel numbers a claim's INVENTORY-style bench dict declares (both the
    singular ``channel`` bench field and a ``channels`` list are honored)."""
    out: set = set()
    for c in (bench.get("channels") or []):
        if isinstance(c, int):
            out.add(c)
        elif isinstance(c, dict):
            cid = c.get("id", c.get("channel", c.get("n")))
            if isinstance(cid, int):
                out.add(cid)
    if isinstance(bench.get("channel"), int):
        out.add(bench["channel"])
    return out


def _cmd_acquire_run(d: dict, session, req: dict) -> dict:
    try:
        from rules_packager_base import bench_endpoints as be
    except ImportError:
        return {"ok": False, "kind": "unsupported_bundle",
                "error": "installed wheel lacks bench_endpoints; regenerate "
                         "the bundle with rules_packager >= 2.0.5"}
    st = _state(session)
    if st.active is not None:
        return {"ok": False, "kind": "RunActive",
                "error": f"run {st.active!r} still holds the bench; "
                         "release it first"}
    claims = [
        be.UsageClaim(
            test_id=str(c.get("test_id") or ""),
            local_name=str(c.get("local_name") or ""),
            etype=str(c.get("etype") or ""),
            subtype=str(c.get("subtype") or ""),
            bench=c.get("bench") or {},
        )
        for c in (req.get("claims") or [])
    ]
    policies = {(t, n): p
                for t, m in (req.get("policies") or {}).items()
                for n, p in (m or {}).items()}
    plan = be.reconcile(claims, policies, req.get("profile_defaults"),
                        session.lifecycle)
    if isinstance(plan, be.ConflictReport):
        return {"ok": False, "kind": "EndpointConflict",
                "error": plan.conflicts[0].message if plan.conflicts
                else "endpoint conflict",
                "conflicts": [c.message for c in plan.conflicts]}
    view: dict = {}
    identities: dict = {}
    summary: list = []
    per_step: list = []
    for spec in plan.endpoints:
        if spec.effective_policy != "per_session":
            per_step.append(spec.endpoint)   # test self-manages (full lifecycle)
        else:
            for u in spec.usages:
                prev = view.get(u.claim.local_name)
                if prev is not None and prev != spec.endpoint:
                    return {"ok": False, "kind": "EndpointConflict",
                            "error": f"{u.claim.local_name!r} claims two "
                                     f"endpoints in one run ({prev} and "
                                     f"{spec.endpoint})"}
            namespace = d["_namespace_for"](spec.etype, spec.subtype)
            bench = dict(spec.usages[0].claim.bench)
            bench.update(spec.merged_connection)
            # Lazy admission: open ONCE (no reset) under the ENDPOINT key.
            d["_ensure_held"](session, {spec.endpoint: bench},
                              spec.endpoint, namespace)
            chans = st.channels.setdefault(spec.endpoint, set())
            for u in spec.usages:
                chans.update(_claim_channels(u.claim.bench))
                view[u.claim.local_name] = spec.endpoint
                st.alias[u.claim.local_name] = spec.endpoint
            dev = session.held[spec.endpoint]
            for attr in ("identity", "idn"):   # stored *IDN? where present
                ident = getattr(dev, attr, None)
                if ident is not None and not callable(ident):
                    identities[spec.endpoint] = str(ident)
                    break
        summary.append({"endpoint": spec.endpoint, "etype": spec.etype,
                        "policy": spec.effective_policy,
                        "promoted": spec.promoted,
                        "usages": [u.claim.label for u in spec.usages]})
    run_id = f"run-{next(st.run_seq)}"
    st.runs[run_id] = _Run(
        run_id=run_id, keep_state=bool(req.get("keep_state")),
        results_dir=str(req.get("results_dir") or ""),
        view=view, manual=tuple(c.local_name for c in plan.manual_usages))
    st.active = run_id
    return {"ok": True, "run_id": run_id,
            "plan": {"endpoints": summary,
                     "manual": [c.label for c in plan.manual_usages],
                     "per_step": per_step},
            "identities": identities}


def _cmd_release_run(d: dict, session, req: dict) -> dict:
    del d
    st = _state(session)
    rid = req.get("run_id")
    run = st.runs.pop(rid, None)
    if run is None:
        return {"ok": False, "kind": "UnknownRun",
                "error": f"unknown run_id {rid!r}"}
    if st.active == rid:
        st.active = None
    # Held devices deliberately STAY open — releasing the handle never closes
    # the persistent session (end_session == shutdown does).
    return {"ok": True, "run_id": rid}


def _cmd_cancel_run(d: dict, session, req: dict) -> dict:
    """Idle-path cancel (a cancel arriving DURING run_test is consumed from
    the queue by the in-run waits and acknowledged with an "ack" frame)."""
    del d
    st = _state(session)
    run = st.runs.get(req.get("run_id"))
    if run is None:
        return {"ok": False, "kind": "UnknownRun",
                "error": f"unknown run_id {req.get('run_id')!r}"}
    run.cancelled = True
    return {"ok": True, "run_id": run.run_id, "cancelled": True}


_MOD_SEQ = itertools.count(1)


def _import_test(module_path: str):
    """Import a generated test under a UNIQUE module name from its CURRENT
    source bytes (implementer note 1: a file regenerated mid-session must
    never be served stale — neither from ``sys.modules`` nor from a
    ``__pycache__`` .pyc whose (mtime, size) key did not change, which a
    same-length regeneration within one mtime tick would hit)."""
    name = f"_bench_test_{next(_MOD_SEQ)}"
    path = Path(module_path)
    source = path.read_text(encoding="utf-8")
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    exec(compile(source, str(path), "exec"), mod.__dict__)  # noqa: S102
    return mod


def _reassert_signals(d: dict) -> None:
    """``test_helpers``' import installs a SIGBREAK handler; after importing a
    test module, re-assert the daemon's OWN SIGTERM/SIGINT handlers so the
    crash-safety teardown stays wired."""
    handler = d.get("_on_signal")
    if handler is None:
        return
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # non-main thread / unsupported
            pass


def _ladder(d: dict, session, run: _Run, res, escaped, rdir: Path):
    """Failure ladder keyed off the RETURNED Result (design failure table).
    Returns ``(response, stop)``."""
    aborted = bool(getattr(res, "aborted", False))
    errored = escaped is not None or res is None or bool(getattr(res, "error", False))
    summary = {
        "run_id": run.run_id,
        "overall": str(getattr(res, "overall", "ERROR")),
        "aborted": aborted and not errored,
        "error": errored,
        "exception_type": (getattr(res, "exception_type", "")
                           or (type(escaped).__name__ if escaped else "")),
        "exception_message": (getattr(res, "exception_message", "")
                              or (str(escaped) if escaped else "")),
        "results_dir": str(rdir),
    }
    if errored:
        # Driver error / unexpected exception: safe-off ALL held power
        # equipment, close, END the session — respond, then stop=True (the
        # same exit path as shutdown; _teardown's latch keeps atexit a no-op).
        if escaped is not None:
            summary["traceback"] = "".join(
                traceback.format_exception(type(escaped), escaped,
                                           escaped.__traceback__))
        unsafe = d["_teardown"](session)
        resp = {"ok": True, "result": summary, "session_ended": True}
        if unsafe:
            resp["unsafe"] = unsafe
        return resp, True
    if aborted:
        # Operator stop: ordered safe-off of held power equipment ONLY —
        # session and connections REMAIN, devices stay held (design row 3).
        unsafe = d["_safe_off_all_held"](session)
        resp = {"ok": True, "result": summary}
        if unsafe:
            resp["unsafe"] = unsafe
        return resp, False
    return {"ok": True, "result": summary}, False


def _cmd_run_test(d: dict, session, req: dict):
    """Run one generated test inside the holder.  Returns ``(response, stop)``."""
    st = _state(session)
    run = st.runs.get(req.get("run_id"))
    if run is None:
        return {"ok": False, "kind": "UnknownRun",
                "error": f"unknown run_id {req.get('run_id')!r}"}, False
    results_dir = req.get("results_dir") or run.results_dir or "."
    try:
        from rules_packager_base.execution_context import ExecutionContext
        mod = _import_test(req.get("module_path") or "")
    except Exception as exc:  # noqa: BLE001 — nothing ran; no ladder
        return {"ok": False, "kind": "ExecError",
                "error": f"test import failed: {type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()}, False
    finally:
        _reassert_signals(d)
    if getattr(mod, "BENCH_SESSION_API", None) != 1:
        return {"ok": False, "kind": "unsupported_test",
                "error": "test module lacks BENCH_SESSION_API; regenerate it "
                         "with rules_packager >= 2.0.5"}, False
    devices: dict = {}
    owned: dict = {}
    for name, key in run.view.items():
        inst = session.held.get(key)
        if inst is None:
            return {"ok": False, "kind": "ExecError",
                    "error": f"held endpoint for {name!r} ({key}) is gone; "
                             "restart the bench session"}, False
        devices[name] = inst
        owned[name] = False           # held: never reset/closed by the test
    for name in run.manual:
        devices.setdefault(name, None)   # None-for-manual
    rdir = Path(results_dir)
    try:
        rdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    ctx = ExecutionContext(
        devices=devices, owned=owned, keep_state=run.keep_state,
        results_dir=rdir,
        prompt=lambda text: _prompt_via_frames(d, st, run, text),
        cancelled=lambda: _cancelled_poll(d, st, run),
    )
    out = _FrameStdout(d)
    saved_stdout = sys.stdout
    sys.stdout = out
    ctx.install()                     # route test_helpers.prompt() to frames
    res, escaped = None, None
    try:
        res = mod.main(ctx=ctx)
    except BaseException as exc:  # noqa: BLE001 — backstop: same as res.error
        escaped = exc
    finally:
        ctx.restore()
        sys.stdout = saved_stdout
        out.close()
    return _ladder(d, session, run, res, escaped, rdir)


def handle(d: dict, session, req: dict):
    """Dispatch one bench-session frame.  Returns ``(response, stop)`` with
    the same contract as the daemon's ``_dispatch`` branches."""
    cmd = req.get("cmd")
    if cmd == "acquire_run":
        return _cmd_acquire_run(d, session, req), False
    if cmd == "release_run":
        return _cmd_release_run(d, session, req), False
    if cmd == "cancel_run":
        return _cmd_cancel_run(d, session, req), False
    if cmd == "prompt_response":
        # Reaching the MAIN loop means no prompt wait consumed it.
        return {"ok": False, "kind": "Protocol",
                "error": "prompt_response with no prompt outstanding"}, False
    if cmd == "run_test":
        return _cmd_run_test(d, session, req)
    return {"ok": False, "kind": "Protocol",
            "error": f"unknown cmd {cmd!r}"}, False
