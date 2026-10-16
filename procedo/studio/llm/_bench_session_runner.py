"""Bench-session holder machinery for the persistent daemon (S3).

Imported by ``_execute_op_subprocess.py`` as a PLAIN SIBLING module —
file-path invocation puts ``llm/`` on ``sys.path``, so ``import
_bench_session_runner`` resolves without touching the ``procedo.studio``
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
   "results_dir"?: str, "dry_run"?: true}
    With "dry_run": reconcile only — the full plan (or conflict) comes back
    but NO endpoint is opened, nothing is held, no run is created and no
    identity is read: {"ok": true, "dry_run": true, "plan": {...}}.  A dry
    run is allowed even while a run holds the bench (it takes nothing).
    -> {"ok": true, "run_id": str,
        "plan": {"endpoints": [{"endpoint","etype","policy","promoted",
                                "usages": [labels]}],
                 "manual": [labels], "per_step": [endpoints]},
        "identities": {endpoint: str},          # from held drivers' .identity/.idn
        "reconnected"?: [endpoints]}   # held endpoints found dead and rebuilt
    -> {"ok": false, "kind": "unsupported_bundle"} on an old wheel (no
       bench_endpoints in the project venv); "RunActive"; "EndpointConflict".
    Boundary health check (F-H6, ``check_held``): every ALREADY-HELD
    per_session endpoint the incoming run claims is probed (a live *IDN?
    through the raw seam — the same read that stocks the ``.identity``
    fingerprint) before reuse; per_step and manual equipment are never
    probed.  A dead endpoint gets exactly ONE rebuild through the normal
    construction path, then its identity is compared to the session's
    fingerprint: mismatch -> {"ok": false, "kind": "IdentityChanged",
    "endpoints", "changes"} with the fingerprint untouched — and the
    mismatch is STICKY: it is recorded per endpoint in
    ``BenchState.identity_mismatch``, and every later acquire_run, guided
    op, raw line and run_test claiming that endpoint keeps returning the
    same typed refusal.  The rebuilt (unverified) instance stays held, but
    its healthy liveness probe can never clear the flag, and no in-session
    identity re-read clears it either: the ONLY resolution is ending the
    session (end_session == shutdown -> a fresh daemon holds a fresh
    BenchState), after which the host's Start-Bench-Session preflight
    surfaces the cached-vs-observed CHANGED row for explicit operator
    confirmation.  Session keep_state ON + a rebuilt POWER endpoint (its
    lifecycle has a safe-off action) -> {"ok": false, "kind": "StateLost",
    "endpoints"} — fires once: the rebuilt instance stays held, so the
    NEXT acquire probes healthy and proceeds.  Rebuild failure -> the
    errored ladder (safe-off all held, teardown, "session_ended": true,
    daemon stops).  The same check guards the guided-op held-reuse routing
    (``_ensure_held``).  Context contract: the raw context suppresses ONLY
    StateLost (``probe_refuse=False``) — it still refuses on
    IdentityChanged, fresh or sticky; the safe_off context ignores BOTH
    refusal kinds (``identity_gate=False`` too) — a safety action always
    proceeds and drives OFF whatever instrument answers at the endpoint,
    flagged or not.

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
    "session_ended"?: true}.  "unsafe_targets" rides with "unsafe" (the
    structured unconfirmed-off snapshot — daemon module docstring).
    Verdicts are read host-side from results.json.

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
  import / context-build failure (broken test file, held endpoint gone) ->
                 same as res.error — a test that cannot even load mid-session
                 is an UNEXPECTED execution failure, never a benign refusal.
                 Genuinely-benign refusals (UnknownRun, RunActive,
                 unsupported_test/bundle, frame rejections) stay plain errors
                 with the session untouched.
  plain PASS/FAIL -> nothing (host applies between-test behavior).

During ``main(ctx)`` the daemon's CWD is the run's results dir (restored
after, ladder paths included) so RELATIVE artifact writes — generated scope
ops emit relative screenshot filenames — land beside results.json, matching
the legacy subprocess path.  Outside ``run_test`` the CWD stays the parent's
choice (guided-manual screenshot tmp dir): the exec_ops harvesting contract
is unchanged.
"""
from __future__ import annotations

import itertools
import json
import os
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
        self.keep_state = False    # last acquire's keep_state (guided F-H6 gate)
        self.fingerprints: dict = {}  # endpoint -> identity last read at acquire
        self.bench_of: dict = {}   # endpoint -> admission bench dict (rebuilds)
        # endpoint -> {"was","now"}: UNRESOLVED post-rebuild identity mismatch.
        # STICKY — set once by check_held, never cleared in-session (only a
        # fresh BenchState, i.e. end_session/shutdown, forgets it); safe_off
        # is the one context allowed to act on a flagged endpoint.
        self.identity_mismatch: dict = {}
        # endpoint -> last timeout value applied by _retarget_timeout (skip
        # cache for redundant retargets).  Same lifetime as fingerprints /
        # bench_of — forgotten at end_session; invalidated per endpoint by
        # _close_held_device (the rebuilt instance opens from bench_of, so
        # the next seam call must re-apply instead of skipping).
        self.applied_timeout: dict = {}
        self.run_seq = itertools.count(1)
        self.prompt_seq = itertools.count(1)

    def enter(self, session, device: str, bench=None, etype: str = "") -> str:
        """Hold key for a name-keyed op: the ACTIVE run's endpoint when the
        name is claimed onto a held endpoint; else — run active or not — the
        op's OWN bench dict folded to a canonical endpoint (the same wheel
        fold ``acquire_run`` uses) when THAT endpoint is held.  A guided op
        addressing a held endpoint must land on the held instance: a second
        same-process connection would contend the instrument's remote lock
        (the EA supply drops its output over exactly that).  Only a genuinely
        un-held endpoint keeps today's name-keyed guided-manual semantics;
        so does an old wheel without ``bench_endpoints`` (the same
        unsupported-bundle conditional pattern as ``acquire_run``)."""
        run = self.runs.get(self.active) if self.active else None
        key = run.view.get(device) if run is not None else None
        if key is not None and key in session.held:
            return key
        if bench:
            try:
                from rules_packager_base import bench_endpoints as be
            except ImportError:      # old wheel: name-keyed, as today
                return device
            try:
                key = be.canonical_endpoint(
                    bench, (session.lifecycle or {}).get(etype))
            except ValueError:       # no address: name-keyed, as today
                return device
            if key in session.held:
                # Record the routing so channels_for's safe-off union sees
                # channels this guided name energizes on the held endpoint.
                self.alias[device] = key
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
# Boundary health check (F-H6): probe + one rebuild for held endpoints.
# ---------------------------------------------------------------------------


class HealthRefusal(Exception):
    """Typed health-check refusal (IdentityChanged / StateLost) carrying its
    response dict — folded into the acquire_run response at that call site,
    raised through ``_ensure_held`` on the guided-op held-reuse path."""

    def __init__(self, resp: dict) -> None:
        super().__init__(resp.get("error") or resp.get("kind"))
        self.resp = resp


def _read_identity(dev):
    """Stored *IDN? string where the driver exposes one (the same attribute
    read that stocks the acquire response's ``identities``)."""
    for attr in ("identity", "idn"):
        ident = getattr(dev, attr, None)
        if ident is not None and not callable(ident):
            return str(ident)
    return None


def _probe(d: dict, dev) -> bool:
    """Live liveness probe: ``*IDN?`` through the daemon's raw seam.  A
    driver exposing no public raw interface is not probeable — treated as
    healthy rather than falsely rebuilt."""
    if not (hasattr(dev, "query_raw") or hasattr(dev, "raw_command")):
        return True
    try:
        d["_raw_send"](dev, "*IDN?")
        return True
    except Exception:  # noqa: BLE001 — any I/O failure == dead link
        return False


def identity_refusal(st: BenchState, endpoints) -> HealthRefusal:
    """The typed IdentityChanged refusal for flagged endpoints, built from
    the sticky ``BenchState.identity_mismatch`` records (single message
    shape for the fresh raise, the sticky re-raise and the run_test gate)."""
    eps = sorted(endpoints)
    return HealthRefusal({
        "ok": False, "kind": "IdentityChanged", "endpoints": eps,
        "changes": {k: {"cached": st.identity_mismatch[k]["was"],
                        "current": st.identity_mismatch[k]["now"]}
                    for k in eps},
        "error": (
            "equipment identity changed at " + ", ".join(eps) + " after a "
            "lost connection was re-established (connected instrument "
            "differs from the held fingerprint). Run Start Bench Session "
            "to review and confirm the change before executing.")})


def check_held(d: dict, session, key: str, identity_gate: bool = True) -> str:
    """Boundary health check for ONE bench-held endpoint — shared by
    ``acquire_run`` and the guided-op held-reuse routing in ``_ensure_held``.

    An endpoint flagged in ``identity_mismatch`` refuses FIRST — before any
    probe, so a healthy liveness answer from the unverified instrument can
    never clear the sticky refusal.  Otherwise: a device that answers the
    probe is healthy — returns "ok", nothing touched.  Dead -> exactly ONE
    rebuild attempt: best-effort close/drop, reconstruct through the NORMAL
    construction path from the stored admission bench, then compare the
    fresh identity to the session's fingerprint.  Mismatch -> recorded
    STICKY in ``identity_mismatch`` (fingerprint untouched, rebuilt
    instance stays held; nothing runs), then ``HealthRefusal``
    (IdentityChanged); match or no cache -> "rebuilt".  A failed rebuild
    raises RuntimeError with the dead instance dropped: held-endpoint-gone
    for the caller's existing ladder — but FIRST the endpoint is recorded
    unconfirmed-off (the daemon's ``_record_unconfirmed``): its outputs may
    be live and its handle just left ``held``, so without the record every
    later held-only safe-off sweep would report false-clean.

    ``identity_gate=False`` is the safe_off context ONLY: the sticky flag
    and a fresh mismatch are logged instead of raised — a safety action
    offs whatever instrument answers at the endpoint (the mismatch is
    still recorded for every refusing context).
    """
    dev = session.held.get(key)
    st = _state(session)
    if key in st.identity_mismatch and identity_gate:
        raise identity_refusal(st, [key])
    bench = st.bench_of.get(key)
    if dev is None or bench is None or _probe(d, dev):
        return "ok"
    etype = session.etype_of.get(key, "")
    d["_close_held_device"](session, key)
    try:
        d["_ensure_held"](session, {key: bench}, key, etype)
    except Exception as exc:
        # Record BEFORE raising: the possibly-energized endpoint has just
        # left ``held``, and only the unconfirmed-off memory keeps it
        # visible to every later safe-off/shutdown sweep (R2 closure).
        d["_record_unconfirmed"](session, key, etype,
                                 st.channels_for(session, key), bench)
        raise RuntimeError(
            f"held endpoint {key} is gone (reconnect failed: {exc}); "
            "restart the bench session") from exc
    ident = _read_identity(session.held[key])
    cached = st.fingerprints.get(key)
    if cached is not None and ident is not None and ident != cached:
        st.identity_mismatch[key] = {"was": cached, "now": ident}
        if identity_gate:
            raise identity_refusal(st, [key])
        session.res.log.append(
            f"{key} connection lost — re-established with a DIFFERENT "
            f"identity ({cached!r} -> {ident!r}); safety action proceeding")
        return "rebuilt"
    session.res.log.append(
        f"{key} connection lost — re-established, identity verified")
    return "rebuilt"


def state_lost_refusal(session, rebuilt: list) -> dict | None:
    """StateLost rule: session keep_state promised DUT continuity between
    tests; a rebuilt POWER endpoint (its lifecycle declares a safe-off
    action) means the DUT power-cycled behind it.  Returns the typed
    refusal dict, or None when the rule does not fire.  Fires once — the
    rebuilt instances stay held, so the operator's re-run probes healthy."""
    st = _state(session)
    if not st.keep_state:
        return None
    lost = [k for k in rebuilt
            if (session.lifecycle.get(session.etype_of.get(k)) or {}).get("cleanup")]
    if not lost:
        return None
    return {"ok": False, "kind": "StateLost", "endpoints": lost,
            "error": (
                ", ".join(lost) + " reconnected after a lost connection — "
                "the keep-state chain is broken there (the DUT may have "
                "power-cycled). Nothing was run; the session is still "
                "live. Re-run to continue from here.")}


# ---------------------------------------------------------------------------
# Per-run timeout retargeting (capability-gated, pack-agnostic).
# ---------------------------------------------------------------------------


def _retarget_timeout(session, endpoint: str, namespace: str, bench,
                      label: str) -> None:
    """Apply the acquiring claim's declared timeout to a HELD endpoint.

    Capability contract (declared in the owning pack's LIFECYCLE data, so the
    holder never sees a driver class)::

        "retarget_timeout": {"method": "set_timeout", "field": "timeout_s"}

    ``field`` names the bench-connection field to read from ``bench`` (the
    claim's merged bench dict at acquire; the op's bench dict on the guided
    seam); ``method`` names the driver method to call on the held instance.
    The value passes VERBATIM, in its NATIVE unit — the holder performs no
    conversion, ever.  Absent/malformed capability, missing/falsy field value
    (zero = no declared patience), or nothing held at ``endpoint`` -> no-op:
    the max-merged open value stands.

    Called from exactly TWO seams — acquire_run (after _ensure_held, i.e.
    after check_held, so sticky identity_mismatch / HealthRefusal raise before
    any retarget) and the guided-op per_session call site in _cmd_exec_ops
    (Seam B — keyed by the hold key _ensure_held bound, and SKIPPED while a
    run is active: the active run's Seam-A values stand until release, so a
    guided op never re-times a held endpoint mid-run).
    Deliberately NOT inside _ensure_held / BenchState.enter: those are shared
    with the safety paths (_cmd_safe_off, _retry_unconfirmed, teardown, the
    gone/errored ladders), which never retarget — they run with whatever is
    applied.  Accepted consequences of that ruling, documented here:

    * a boundary *IDN?* liveness probe runs at the currently-applied timeout
      (a long value slows dead-endpoint detection; a short one persists
      through safe-off/recovery);
    * a refused acquire (sticky mismatch on endpoint N, or the post-loop
      StateLost) leaves endpoints 1..N-1 retargeted to the refused run's
      values with no active run;
    * a run declaring NO timeout inherits the previous run's retarget, not
      the admission default (field-absent = no-op).

    NON-fatal by contract: a timeout tweak never kills a session — any
    failure is logged, the currently-applied value stands (the max-merged
    open value when no earlier retarget landed), and ``applied_timeout``
    is NOT updated (so a later seam call retries).
    """
    st = session.bench
    if st is None:
        return
    cap = (session.lifecycle.get(namespace) or {}).get("retarget_timeout")
    if not isinstance(cap, dict):
        return                       # absent/malformed capability: max stands
    try:
        field = cap.get("field")
        value = (bench or {}).get(field)
        dev = session.held.get(endpoint)
        if not value or dev is None:
            return                   # no declared patience / nothing held
        if st.applied_timeout.get(endpoint) == value:
            return                   # redundant retarget skipped
        getattr(dev, cap["method"])(value)
    except Exception as exc:  # noqa: BLE001 — NON-fatal, see docstring
        session.res.log.append(
            f"{endpoint} timeout retarget failed, keeping current value: {exc}")
        return
    st.applied_timeout[endpoint] = value
    session.res.log.append(f"{endpoint} {field} -> {value} ({label})")


def _gone_ladder(d: dict, session, key: str, exc) -> dict:
    """Rebuild failure at acquire == held-endpoint-gone: the errored ladder
    (safe-off ALL held, teardown, session ends, daemon stops) — the health
    check adds a second chance BEFORE the ladder, it never replaces it."""
    unsafe = d["_teardown"](session)
    return d["_attach_unsafe"](
        session,
        {"ok": False, "kind": "ExecError", "session_ended": True,
         "error": f"{exc}"},
        unsafe)


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
    """Channel numbers a claim's INVENTORY-style bench dict declares — the
    wheel's ONE normalizer (``channels_of``: ``channels`` list ∪ singular
    ``channel``, ``[1]`` fallback), so this daemon, host recovery and
    generated cleanup can never read the contract apart.  The naive branch
    mirrors it for an old wheel without ``channels_of``."""
    try:
        from rules_packager_base.bench_endpoints import channels_of
    except ImportError:
        out: set = set()
        for c in (bench.get("channels") or []):
            if isinstance(c, dict):
                c = c.get("id", c.get("channel", c.get("n")))
            if isinstance(c, int) and not isinstance(c, bool):
                out.add(c)
        single = bench.get("channel")
        if isinstance(single, int) and not isinstance(single, bool):
            out.add(single)
        return out or {1}
    return set(channels_of(bench))


def _cmd_acquire_run(d: dict, session, req: dict) -> dict:
    try:
        from rules_packager_base import bench_endpoints as be
    except ImportError:
        return {"ok": False, "kind": "unsupported_bundle",
                "error": "installed wheel lacks bench_endpoints; regenerate "
                         "the bundle with rules_packager >= 2.0.5"}
    st = _state(session)
    dry = bool(req.get("dry_run"))
    if not dry and st.active is not None:
        # A dry run takes nothing, so it may plan even while a run is active.
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
    reconnected: list = []
    if not dry:
        st.keep_state = bool(req.get("keep_state"))
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
                view[u.claim.local_name] = spec.endpoint
            if not dry:
                namespace = d["_namespace_for"](spec.etype, spec.subtype)
                bench = dict(spec.usages[0].claim.bench)
                bench.update(spec.merged_connection)
                st.bench_of[spec.endpoint] = bench
                # Boundary health check (F-H6): a held endpoint this run
                # claims must still answer; ONE rebuild before the ladder.
                try:
                    if check_held(d, session, spec.endpoint) == "rebuilt":
                        reconnected.append(spec.endpoint)
                except HealthRefusal as exc:
                    return exc.resp
                except Exception as exc:  # noqa: BLE001 — rebuild failed
                    return _gone_ladder(d, session, spec.endpoint, exc)
                # Lazy admission: open ONCE (no reset) under the ENDPOINT key.
                d["_ensure_held"](session, {spec.endpoint: bench},
                                  spec.endpoint, namespace)
                # Seam A (per-run timeout retargeting): AFTER _ensure_held —
                # i.e. after check_held — so a sticky refusal raised above
                # never retargets.  ``bench`` carries the run's max-merged
                # connection values (merged_connection update above).
                _retarget_timeout(
                    session, spec.endpoint, namespace, bench,
                    ", ".join(sorted({u.claim.test_id for u in spec.usages})))
                chans = st.channels.setdefault(spec.endpoint, set())
                for u in spec.usages:
                    chans.update(_claim_channels(u.claim.bench))
                    st.alias[u.claim.local_name] = spec.endpoint
                ident = _read_identity(session.held[spec.endpoint])
                if ident is not None:
                    identities[spec.endpoint] = ident
                    st.fingerprints[spec.endpoint] = ident
        summary.append({"endpoint": spec.endpoint, "etype": spec.etype,
                        "policy": spec.effective_policy,
                        "promoted": spec.promoted,
                        "usages": [u.claim.label for u in spec.usages]})
    if dry:
        # Plan only: nothing opened, nothing held, no run handle, no
        # identity reads — the full plan (or the conflict above) is the
        # whole answer.
        return {"ok": True, "dry_run": True,
                "plan": {"endpoints": summary,
                         "manual": [c.label for c in plan.manual_usages],
                         "per_step": per_step}}
    if reconnected:
        refusal = state_lost_refusal(session, reconnected)
        if refusal is not None:
            return refusal
    run_id = f"run-{next(st.run_seq)}"
    st.runs[run_id] = _Run(
        run_id=run_id, keep_state=bool(req.get("keep_state")),
        results_dir=str(req.get("results_dir") or ""),
        view=view, manual=tuple(c.local_name for c in plan.manual_usages))
    st.active = run_id
    resp = {"ok": True, "run_id": run_id,
            "plan": {"endpoints": summary,
                     "manual": [c.label for c in plan.manual_usages],
                     "per_step": per_step},
            "identities": identities}
    if reconnected:
        resp["reconnected"] = reconnected
    return resp


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
        return d["_attach_unsafe"](
            session, {"ok": True, "result": summary, "session_ended": True},
            unsafe), True
    if aborted:
        # Operator stop: ordered safe-off of held power equipment ONLY —
        # session and connections REMAIN, devices stay held (design row 3).
        unsafe = d["_safe_off_all_held"](session)
        return d["_attach_unsafe"](
            session, {"ok": True, "result": summary}, unsafe), False
    return {"ok": True, "result": summary}, False


def _cmd_run_test(d: dict, session, req: dict):
    """Run one generated test inside the holder.  Returns ``(response, stop)``."""
    st = _state(session)
    run = st.runs.get(req.get("run_id"))
    if run is None:
        return {"ok": False, "kind": "UnknownRun",
                "error": f"unknown run_id {req.get('run_id')!r}"}, False
    flagged = set(run.view.values()) & set(st.identity_mismatch)
    if flagged:
        # Sticky IdentityChanged: a run handle acquired BEFORE a guided/raw
        # boundary flagged one of its endpoints must not execute on the
        # unverified instrument.  Benign typed refusal — session untouched.
        return identity_refusal(st, flagged).resp, False
    results_dir = req.get("results_dir") or run.results_dir or "."
    rdir = Path(results_dir)
    try:
        from rules_packager_base.execution_context import ExecutionContext
        mod = _import_test(req.get("module_path") or "")
    except Exception as exc:  # noqa: BLE001 — broken test file: errored ladder
        # A test file that cannot import mid-session is an UNEXPECTED
        # execution failure (design ladder: abort, safe-off all held, end
        # session) — not a benign refusal.  The session must not stay hot
        # behind an ExecError.
        return _ladder(d, session, run, None, exc, rdir)
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
            # Context-build failure: the session lost a held endpoint — same
            # errored ladder as res.error (unexpected failure, session ends).
            gone = RuntimeError(f"held endpoint for {name!r} ({key}) is "
                                "gone; restart the bench session")
            return _ladder(d, session, run, None, gone, rdir)
        devices[name] = inst
        owned[name] = False           # held: never reset/closed by the test
    for name in run.manual:
        devices.setdefault(name, None)   # None-for-manual
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
    saved_cwd = os.getcwd()
    sys.stdout = out
    ctx.install()                     # route test_helpers.prompt() to frames
    res, escaped = None, None
    try:
        if rdir.is_dir():
            # Generated scope ops write RELATIVE artifact filenames (e.g.
            # save_screenshot) — run with CWD at the results dir so they
            # land beside results.json, exactly like the legacy subprocess
            # path whose CWD was the results dir.  The daemon is
            # single-threaded with one run at a time; the prior CWD (the
            # guided-manual screenshot tmp dir) is restored below so the
            # exec_ops harvesting contract is unchanged.
            os.chdir(rdir)
        res = mod.main(ctx=ctx)
    except BaseException as exc:  # noqa: BLE001 — backstop: same as res.error
        escaped = exc
    finally:
        try:
            os.chdir(saved_cwd)       # restored even on the ladder paths
        except OSError:
            pass
        ctx.restore()
        sys.stdout = saved_stdout
        out.close()
    return _ladder(d, session, run, res, escaped, rdir)


def handle(d: dict, session, req: dict):
    """Dispatch one bench-session frame.  Returns ``(response, stop)`` with
    the same contract as the daemon's ``_dispatch`` branches."""
    cmd = req.get("cmd")
    if cmd == "acquire_run":
        resp = _cmd_acquire_run(d, session, req)
        # session_ended only appears on the acquire-time gone-ladder: the
        # daemon has already safed-off + torn down — stop like shutdown.
        return resp, bool(resp.get("session_ended"))
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
