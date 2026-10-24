"""Persistent daemon runner for executing procedure ops against live equipment.

Invoked by FILE PATH (NOT ``-m``) in the *project* venv::

    <machine-local venv>/<bin>/python <abs>/_execute_op_subprocess.py --role run|tools

Running via ``-m`` would need the ``procedo`` package (absent from the project
venv); invoking by path keeps ``__name__ == "__main__"`` and
skips the package init.  It serves the engine's op table (``op`` frames,
``_engine_ops.py``) and, as RUN, imports the hardware drivers (labscpi /
fncore).  Each role refuses frames outside its job (``_engine_ops.refusal``).

Why a daemon (not one-shot)
---------------------------
Standard equipment (psu/eload/scope) holds physical state, and on some
instruments (e.g. the EA PS 9080) ``close()`` releases the remote lock which
DROPS the output.  A one-shot connect->op->close therefore zeroes the bench
between every step.  So this runner stays alive for the whole guided-manual
session and holds a SINGLE session per "per_session" device, closing (and
unlocking) only at ``shutdown`` / interpreter exit.  Devices whose policy is
"per_step" (serial packs — lifecycle ``remote: False`` — by default) keep the
cheap connect->op->close-per-op model — they can go down safely between steps.

Connection-lifecycle policy (per device)
-----------------------------------------
``bench_map[device]["session_policy"]`` in {"per_step","per_session"} selects
the model.  The dialog resolves and stamps it (authoritative); this runner
obeys, falling back to lifecycle metadata when unset (namespace declares
``remote: False`` -> per_step, else per_session).  per_session devices live in ``Session.held`` (opened once,
closed only at teardown); per_step devices are opened + closed around the single
op (``_exec_transient``) and never enter ``held``.  A device is consistently one
policy for the whole run (enforced).

The "no reset" guarantee
------------------------
``connect()``/``open()`` + ``initialize()`` only, NEVER ``reset()`` — device
output/state is left exactly as the bench holds it.

The op -> driver-call mapping is NOT duplicated here.  Each pack owns it in
``emit_python(op, ctx)``; we drive that emit through a capture-context that
records the *remote* branch's driver call(s) instead of writing test.py source,
then exec those line(s) with the live device bound.  One seam, every pack.

Framed wire protocol (NDJSON: one JSON object per line, both directions)
------------------------------------------------------------------------
The protocol channel is a PRIVATE dup of the real stdout fd; fd 1 itself is then
redirected to stderr so even C-level (VISA backend) writes to "stdout" cannot
corrupt a frame.  Tracebacks travel inside the response JSON, never printed.

  Request  (one line) — the document travels IN the frame ("procedure_json");
  nothing is read from a path.  An instrument frame's document becomes the
  session's: the last one received is what the teardown resolves declared
  channels from (``_safe_off_all_held``); a frame without one keeps it, and
  ``exec_ops`` refuses to run without its own.  An ``op`` frame carries its own:
    {"cmd": "op", "op": "<name>", "procedure_json"?: {...}, ...}   # the op's
     spec (_engine_ops.py); answered with the op's result; never changes the
     session's document, never opens a device
    {"cmd": "exec_ops", "_bundle_dir": "<abs>", "procedure_json": {...}, "work_dir"?: "<abs>",
     "ops": [{"node_path": str, "op": {...}}, ...],
     "bench_map": {device: {"visa"|"port"..., "session_policy": "..."}},
     "measurements"?: {node_path: value}, "controls"?: {node_path: decision},
     "operator_verdicts"?: {ref: verdict}}   # prior run state -> live verdicts
    {"cmd": "safe_off", "_bundle_dir": "<abs>", "procedure_json"?: {...},
     "bench_map": {...}, "safe_off": [{"device","etype","channels":[...]}, ...]}
    {"cmd": "raw", "_bundle_dir": "<abs>", "device": str, "etype": str,
     "subtype": str, "text": "<one raw command line>", "bench_map": {device: {...}}}
    {"cmd": "shutdown"}        # safe-off all held + close (unlock) + exit
    {"cmd": "ping"}            # liveness probe
    {"cmd": "call", "procedure_json": {...}, "work_dir": "<abs>", "node_path": str,
     "measurements", "controls", "operator_verdicts"}   # a guided Call row's function
     (manual_run.make_call): one functions worker (``once``) in the run's folder,
     its functions snapshot there; streams {"kind": "progress", "node_path"}
     while it waits (the host's per-frame deadline), stopped within 1 s by
    {"cmd": "cancel_call"}     # POSTED, never answered (a no-op with no call running)
  Bench-session frames (S3; full contract in _bench_session_runner.py):
    {"cmd": "acquire_run"|"release_run"|"run_test"|"cancel_run"|
     "prompt_response", ...}   # run handles over endpoint-keyed held devices
  "end_session" is NOT a frame — it IS the existing "shutdown".  During
  run_test the daemon streams {"kind": "output"|"prompt"|"ack"|"reject"}
  frames (same channel as "progress") before the final response.

  Response (one line):
    exec_ops : {"ok": true, "results": [...], "aborted": bool, "log": [str],
                "unsafe": [str]?}     # unsafe = devices that could NOT power down
    safe_off : {"ok": true, "log": [str], "unsafe": [str]?}
    raw      : {"ok": true, "response": str, "log": [str]}   # "" reply for a write
    call     : {"ok": true, "reading": {"args_hash", "outcome"}}   # the row's reading
               or {"ok": false, "kind": "Stopped"|"NoCall"|"NoProcedure"|"NoWorkDir", "error"}
    shutdown : {"ok": true, "log": [str], "unsafe": [str]?}   (then exits)
    Every response carrying "unsafe" also carries "unsafe_targets":
    [{"device", "etype", "channels", "bench"}] — the unconfirmed-off memory
    ("device" = canonical endpoint when the bench session routed the name;
    "bench" = stored reconnect data, null when never stored; power targets
    only).  Hosts drive recovery from THIS, never by parsing the strings.
    error    : {"ok": false, "kind": "<NotRemote|NoCodegen|NoLifecycle
                |NoProcedure|ExecError|Protocol>", "error": str,
                "traceback": str?}

Crash safety: ``atexit`` + SIGTERM/SIGINT run the same teardown (safe-off ALL
held psu/eload by DECLARED channels, ELOAD-before-PSU, then close()->unlock).
On a hard kill (SIGKILL / Windows TerminateProcess) those do NOT run — the
PARENT is responsible for killing a wedged daemon and spawning a fresh
``safe_off`` runner.

Unconfirmed-off memory
----------------------
The held/energized state, every safe-off pass and the unconfirmed-off memory behind ``unsafe``
live in the sibling ``_safe_off.py`` (its module docstring is the contract); op execution
lives in ``_instrument_ops.py``.  Both load by bare name next to this file.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import traceback
from pathlib import Path


def _sib(name: str):
    """Lazy sibling import, never through the procedo package (it is
    absent from project venvs); sys.modules is the cache."""
    try:
        return sys.modules[name]
    except KeyError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        return __import__(name)


# The siblings load by BARE name (``_sib`` puts this folder on sys.path).  Every name below
# is used here or read back through this module's ``globals()`` (``d``) by
# _bench_session_runner / _guided_call (tests/test_daemon_d_seam.py pins every key they read).
_sib("_safe_off")
from _instrument_ops import (  # noqa: E402
    _build_device, _exec_op, _instrument_error, _namespace_for, _NoCodegen, _NotRemote,
    _open_no_reset, _policy_for, _raw_send, _safe_name,
)
from _safe_off import (  # noqa: E402
    _attach_unsafe, _bench, _close_held_device, _ensure_held, _is_state_changing,
    _record_unconfirmed, _retry_unconfirmed, _safe_off, _Session, _teardown,
)
from _safe_off import _safe_off_all_held  # noqa: E402, F401 -- read through d by _bench_session_runner


# ---------------------------------------------------------------------------
# Command handlers.
# ---------------------------------------------------------------------------


def _require_lifecycle(session: _Session):
    """A hard, visible failure if the bundle never loaded — otherwise
    ``_is_state_changing`` would silently report False for everything and NO
    safe-off would run (bench left energized with no error)."""
    if not session.lifecycle:
        return {"ok": False, "kind": "NoLifecycle",
                "error": "pack bundle not loaded (_bundle_dir missing on first "
                         "frame); refusing to drive hardware without lifecycle."}
    return None


def _require_procedure(session: _Session):
    """A hard, visible failure without a procedure document — exec must not run
    without it (channel resolution / safe-off would silently degrade to guesses)."""
    if session.procedure_json is None:
        return {"ok": False, "kind": "NoProcedure",
                "error": "no procedure document (procedure_json) came with the "
                         "frame; refusing to execute ops."}
    return None


def _live_verdict(procedure_json, inputs: dict, node_path: str) -> str:
    """``PASS|FAIL|SKIP|''`` for the step at ``node_path``, materialized from the
    run's ``inputs`` known SO FAR via the SAME ``build_manual_run`` the GUI calls
    at batch end — so a streamed live verdict can never diverge from the
    authoritative one. Best-effort: any failure yields ``''`` (a live verdict
    must never break the batch)."""
    try:
        from rules_packager_base.rules.v2_0_2.parser import build_manual_run
        run = build_manual_run(procedure_json, **inputs)
        for s in list(getattr(run, "steps", []) or []):
            if getattr(s, "node_path", "") == node_path:
                return getattr(s, "verdict", "") or ""
    except Exception:  # noqa: BLE001 — verdict is a visual nicety, never fatal
        return ""
    return ""


def _exec_transient(session: _Session, bench_map: dict, device: str, etype: str,
                    procedure_json: dict, op: dict):
    """per_step path: open the device, run the single op, CLOSE it immediately.
    The device never enters ``held`` (chokepoint: a device is per_step XOR
    per_session — enforced with an explicit raise, not an assert, so it survives
    ``python -O``).  The namespace slot is also guarded against a _safe_name
    collision evicting a HELD device's binding."""
    if device in session.held:
        raise RuntimeError(f"{device!r} routed both per_step and per_session")
    safe = _safe_name(device)
    held_safe = {_safe_name(d) for d in session.held}
    if safe in held_safe:
        raise RuntimeError(
            f"per_step device {device!r} collides (safe-name {safe!r}) with a "
            f"held device — refusing to evict the held binding")
    if device not in bench_map:
        raise _NotRemote(f"no connection parameters for {device!r}.")
    dev = _build_device(bench_map[device], etype, session.lifecycle, session.res.log)
    _open_no_reset(dev)
    session.ns[safe] = dev
    try:
        return _exec_op(procedure_json, op, session.ns)
    finally:
        try:
            if hasattr(dev, "close"):
                dev.close()
        except Exception as exc:  # noqa: BLE001
            session.res.log.append(f"close() failed: {exc}")
        session.ns.pop(safe, None)


def _cmd_exec_ops(session: _Session, req: dict) -> dict:
    guard = _require_lifecycle(session) or _require_procedure(session)
    if guard:
        return guard
    procedure_json = session.procedure_json
    ops = req.get("ops") or []
    bench_map = req.get("bench_map") or {}
    # Run state for LIVE verdicts: prior measurements (entered by hand or in
    # earlier batches) seed the accumulator; each reading this batch produces is
    # folded in as it executes, then materialized into the progress frame so a
    # PASS/FAIL lands the instant the op completes — not at batch end.
    inputs = _sib("_engine_ops_manual").run_inputs(req)
    acc_meas = inputs["measurements"] = dict(inputs["measurements"])   # node_path keys, as the controls'
    log_mark = len(session.res.log)
    results: list = []
    failed = False
    total = len(ops)
    for _i, entry in enumerate(ops):
        op = entry.get("op") or {}
        node_path = entry.get("node_path", "")
        device = op.get("device")
        etype = (op.get("op") or "").split(".", 1)[0]
        policy = _policy_for(bench_map.get(device, {}), etype, session.lifecycle)
        try:
            if policy == "per_session":
                hold_key = _ensure_held(session, bench_map, device, etype)
                # Seam B (per-run timeout retargeting): guided ops ONLY —
                # _cmd_safe_off / _retry_unconfirmed / _cmd_raw call
                # _ensure_held directly and never pass here.  Gated on an
                # EXISTING BenchState; keyed by EXACTLY the hold key just
                # bound (never the alias map — never popped, so stale-able).
                # SKIPPED while a run is active: its Seam-A values stand
                # until release — guided retargets apply between runs only.
                if session.bench is not None and session.bench.active is None:
                    _bench()._retarget_timeout(
                        session, hold_key, etype, bench_map.get(device), device)
                # Mark energized BEFORE exec: a multi-line driver call that
                # asserts the output then raises a later line still gets safe-off'd.
                if _is_state_changing(op, session.lifecycle):
                    session.energized.setdefault(device, set()).add(op.get("channel"))
                has, value, unit, ref = _exec_op(procedure_json, op, session.ns)
            else:  # per_step — transient open/close around this op only
                has, value, unit, ref = _exec_transient(
                    session, bench_map, device, etype, procedure_json, op)
            results.append({"node_path": node_path, "ok": True,
                            "has_value": bool(has), "value": value,
                            "unit": unit, "ref": ref})
        except Exception as exc:  # noqa: BLE001 — record + stop the batch
            if session.bench is not None and isinstance(exc, _bench().HealthRefusal):
                # Typed health refusal (F-H6) stops the batch AT this op: the
                # refused endpoint ran NOTHING (its rebuilt instance stays
                # held — no abnormal-exit safe-off, no broken-device drop),
                # but earlier ops in this batch DID execute — their results
                # ride along so the host records them and a re-run resumes
                # after them instead of re-executing state-changing ops.
                return dict(exc.resp, results=results,
                            log=session.res.log[log_mark:])
            failed = True
            results.append({"node_path": node_path, "ok": False,
                            "error": _instrument_error(device, bench_map.get(device), exc)})
        # A reading that just landed changes this step's verdict (and only this
        # step's — an auto segment never spans an @IF barrier, so no later
        # node_path shifts). Fold it in and materialize the live PASS/FAIL.
        last = results[-1]
        verdict = ""
        if last.get("ok") and last.get("has_value") and last.get("ref") is not None:
            acc_meas[node_path] = last.get("value")
            verdict = _live_verdict(procedure_json, inputs, node_path)
        # Stream a per-op progress frame so the GUI advances the cursor live AND
        # paints the verdict on the fly, instead of jumping (and badging the
        # whole segment at once) when the batch returns.
        if _PROTO_OUT is not None:
            try:
                _write_frame(_PROTO_OUT, {
                    "kind": "progress", "node_path": node_path,
                    "ok": last.get("ok", False), "verdict": verdict,
                    "i": _i, "total": total})
            except Exception:  # noqa: BLE001 — never let progress break the batch
                pass
        if failed:
            break
    unsafe: list = []
    if failed and session.energized:
        # Abnormal exit: return the bench to safety (outputs off) but KEEP the
        # held session open — the operator can re-arm.  per_step devices are
        # already closed (per-op).  Failures are recorded unconfirmed-off
        # (session=) so the per-target/teardown passes keep retrying them;
        # this mid-batch pass itself does NOT retry old records — no
        # reconnect stalls inside an exec batch.
        unsafe = _safe_off(procedure_json, session.ns, session.energized,
                           session.etype_of, session.lifecycle, session.res,
                           session=session, bench_map=bench_map)
        session.energized.clear()
    if failed and policy == "per_session" and device in session.held:
        # Drop ONLY the broken device (AFTER safe-off, so a failed energized PSU
        # was still powered down). The next op on it re-opens fresh while every
        # other held device stays up — this is what makes "retry on the next op"
        # work instead of the session wedging until the window is reopened.
        _close_held_device(session, device)
    return _attach_unsafe(session,
                          {"ok": True, "results": results, "aborted": failed,
                           "log": session.res.log[log_mark:]}, unsafe)


def _cmd_raw(session: _Session, req: dict) -> dict:
    """Send ONE raw command line to a device's live link and return its reply.

    Connection lifecycle MIRRORS the run's, resolved by :func:`_policy_for` so a
    terminal is coherent with how the run treats the same device:

    * ``per_session`` (psu/eload) -> :func:`_ensure_held`: hold the link OPEN
      (reuse it if a step already armed the device), so the terminal shares the
      run's live session and the device joins the shutdown safe-off net.
    * ``per_step`` (fncore controller) -> transient open-no-reset -> send ->
      close, exactly like a per_step op, so the link is free between commands
      (manual access preserved).

    On a held-device failure the broken handle is dropped (like
    :func:`_cmd_exec_ops`) so the next command/op re-opens fresh.  The reply is
    appended to the run log (returned in ``log``) so manual terminal traffic
    lands in the saved console naturally.  Needs the bundle (for driver_class)
    but NOT the procedure document.
    """
    guard = _require_lifecycle(session)
    if guard:
        return guard
    device = req.get("device")
    etype = req.get("etype") or ""              # DECLARED type (e.g. "controller")
    subtype = req.get("subtype") or ""
    text = (req.get("text") or "").strip()
    bench_map = req.get("bench_map") or {}
    log_mark = len(session.res.log)
    if not text:
        return {"ok": False, "kind": "Protocol", "error": "empty command",
                "log": session.res.log[log_mark:]}
    # Driver construction + lifecycle are keyed by the pack NAMESPACE, not the
    # declared type (they coincide only for psu/eload/scope; controller->fncore).
    namespace = _namespace_for(etype, subtype)
    policy = _policy_for(bench_map.get(device) or {}, namespace, session.lifecycle)
    try:
        if policy == "per_session":
            # Hold, reuse if armed.  probe_refuse=False: a raw command may
            # repair (probe + one rebuild) a dead held link, but StateLost
            # semantics don't apply to a terminal line — it proceeds on the
            # rebuilt instance.  IdentityChanged still surfaces, typed, below
            # (identity_gate stays ON: fresh mismatch AND sticky flag alike).
            _ensure_held(session, bench_map, device, namespace,
                         probe_refuse=False)
            # A guided name routed onto a bench endpoint holds the instance
            # under its ENDPOINT key; the name-keyed namespace binding covers
            # both the routed and the plain name-keyed case.
            dev = session.held.get(device) or session.ns.get(_safe_name(device))
            reply = _raw_send(dev, text)
        else:                                                 # per_step: transient
            bench = bench_map.get(device)
            if bench is None:
                raise _NotRemote(f"no connection parameters for {device!r}.")
            # Build with a THROWAWAY log: a line-protocol driver (fncore) self-logs
            # every "CMD: .. | RESP: .." + reconnect line into its log_list, which
            # would DUPLICATE the single clean "<device> -> <reply>" line below.
            # And cap the readline timeout so draining a finished multi-line reply
            # returns promptly instead of waiting the full per-op timeout. Neither
            # touches a VISA device (it ignores the log and uses timeout_ms).
            term_bench = dict(bench)
            term_bench["timeout_s"] = min(float(bench.get("timeout_s") or 2.0), 1.0)
            dev = _build_device(term_bench, namespace, session.lifecycle, [])
            _open_no_reset(dev)
            try:
                reply = _raw_send(dev, text)
            finally:
                try:
                    if hasattr(dev, "close"):
                        dev.close()
                except Exception as exc:  # noqa: BLE001
                    session.res.log.append(f"close() failed: {exc}")
        reply = reply.strip() if isinstance(reply, str) else reply
        if reply:
            session.res.log.append(f"{device} -> {reply}")
        return {"ok": True, "response": reply,
                "log": session.res.log[log_mark:]}
    except Exception as exc:  # noqa: BLE001
        if session.bench is not None and isinstance(exc, _bench().HealthRefusal):
            # Typed health refusal (IdentityChanged — StateLost is suppressed
            # above): kind + clean message pass through like _cmd_exec_ops',
            # never "ExecError: HealthRefusal: ...".  The rebuilt, healthy
            # instance stays held — there is nothing to drop.
            return dict(exc.resp, log=session.res.log[log_mark:])
        # Coherent with exec_ops: a broken per_session handle is dropped so the
        # next command/op re-opens fresh (retry-on-next) instead of wedging.
        # SAFETY SPLIT (deliberate, unlike _cmd_exec_ops): the daemon runs NO
        # safe-off here. A raw command is arbitrary SCPI/ASCII, so the daemon
        # cannot know it energized an output and does not track it in
        # session.energized. Safe-off of a raw-energized device is the GUI's
        # job: _on_raw_send adds the device to self._auto_energized BEFORE the
        # send, and finish / dialog-close / session-lost drive bench_recovery.recovery_safe_off
        # over that set (which re-opens the device fresh). NOTE the latent gap a
        # future change must respect: after this drop the device leaves
        # session.held, so the daemon's HELD-only shutdown safe-off will NOT
        # power it down — only the GUI's _auto_energized path will.
        if policy == "per_session" and device in session.held:
            _close_held_device(session, device)
        error = _instrument_error(device, bench_map.get(device), exc)
        session.res.log.append(f"raw FAILED: {error}")
        return {"ok": False, "kind": "ExecError", "error": error,
                "log": session.res.log[log_mark:]}


def _cmd_safe_off(session: _Session, req: dict) -> dict:
    """Turn specified outputs OFF, keeping devices held (the session continues).
    Doubles as the fresh-recovery path: a brand-new daemon opens the targets
    here, offs them, and a following ``shutdown`` closes+unlocks them.
    A SAFETY action never refuses (F-H6): a dead held endpoint is probed and
    rebuilt (``probe_refuse=False`` — no StateLost, keep_state irrelevant;
    ``identity_gate=False`` — a sticky or fresh IdentityChanged is bypassed
    too) and the off runs on whatever instrument answers at the endpoint; a
    target that cannot be (re)opened at all lands in ``unsafe`` AND in the
    unconfirmed-off memory so the host latches UNSAFE, exactly as a
    pre-F-H6 dead-link off failure did.

    Recorded unconfirmed-off targets are retried FIRST (``_safe_off`` module docstring) —
    restoring a dropped endpoint into ``held`` before this frame's targets
    route — so every safe-off pass gives the bench a second chance and a
    target is forgotten only once its off is confirmed."""
    guard = _require_lifecycle(session)
    if guard:
        return guard
    # Tolerates a frame that never carried a document ({}): recovery safe-off must not be
    # blocked — targets carry explicit channels, the document is only the
    # declared-channels fallback for channel-less state-changers.
    procedure_json = session.procedure_json or {}
    bench_map = req.get("bench_map") or {}
    targets = req.get("safe_off") or []   # [{"device","etype","channels":[...]}]
    log_mark = len(session.res.log)
    todo: dict = {}
    unsafe = _retry_unconfirmed(session, set(session.unconfirmed_off))
    for t in targets:
        device, etype = t.get("device"), t.get("etype")
        try:
            # Health check bypass for a SAFETY action: never refuse —
            # probe_refuse=False drops StateLost, identity_gate=False drops
            # IdentityChanged (sticky flag included) — whatever instrument
            # answers at the endpoint gets its outputs driven off.
            _ensure_held(session, bench_map, device, etype,
                         probe_refuse=False, identity_gate=False)
            todo[device] = set(t.get("channels") or [1])
        except Exception as exc:  # noqa: BLE001
            # Open/rebuild failed: these outputs could NOT be powered down —
            # they land in ``unsafe`` (not just the log) so the host latches
            # UNSAFE, the pre-F-H6 dead-link contract — and in the
            # unconfirmed-off memory so later passes keep retrying them.
            why = _instrument_error(device, bench_map.get(device), exc)
            session.res.log.append(f"UNSAFE: safe-off open FAILED: {why}")
            unsafe.extend(f"{device} CH{ch}" for ch in (t.get("channels") or [1]))
            _record_unconfirmed(session, device, etype or "",
                                t.get("channels") or [1], bench_map.get(device))
    if todo:
        unsafe.extend(_safe_off(procedure_json, session.ns, todo, session.etype_of,
                                session.lifecycle, session.res,
                                session=session, bench_map=bench_map))
        for d in todo:
            session.energized.pop(d, None)
    return _attach_unsafe(session, {"ok": True, "log": session.res.log[log_mark:]},
                          unsafe)


def _cmd_shutdown(session: _Session, req: dict) -> dict:
    del req  # teardown uses the last document received (or none — fine)
    log_mark = len(session.res.log)
    unsafe = _teardown(session)
    return _attach_unsafe(session, {"ok": True, "log": session.res.log[log_mark:]},
                          unsafe)


# ---------------------------------------------------------------------------
# Framed NDJSON protocol + crash-safety teardown.
# ---------------------------------------------------------------------------

_SESSION: _Session | None = None
# Protocol channel (set in main()). Used by _cmd_exec_ops to stream per-op
# "progress" frames so the GUI advances the cursor live during a batch.
_PROTO_OUT = None

# Bench-session frames — ALL new machinery lives in the sibling module
# _bench_session_runner.py; this file only dispatches.  "end_session" is
# deliberately NOT in this set: it IS the existing "shutdown" frame.
_BENCH_CMDS = ("acquire_run", "release_run", "run_test", "cancel_run", "prompt_response")


def _write_frame(out, obj: dict) -> None:
    out.write(json.dumps(obj, default=str) + "\n")
    out.flush()


def _emergency_teardown() -> None:
    """atexit / signal backstop — best effort, never raises."""
    try:
        _teardown(_SESSION)
    except Exception:  # noqa: BLE001
        pass


def _on_signal(signum, frame) -> None:  # noqa: ARG001
    # If teardown is already in flight on the main thread, do NOT re-enter VISA
    # I/O from signal context — just exit and let the parent's kill+fresh-safe-off
    # backstop finish the job.
    if not (_SESSION is not None and _SESSION._in_teardown):
        _emergency_teardown()
    os._exit(0)


def _load_bundle(session: _Session, req: dict) -> None:
    if session.bundle_loaded:
        return
    bundle_dir = req.get("_bundle_dir")
    if not bundle_dir:
        return
    from rules_packager_base.rules.v2_0_2.parser._pack_registry import (
        load_packs_into_registry,
    )
    from rules_packager_base.rules.v2_0_2.parser._default_registry import get_lifecycle
    load_packs_into_registry(Path(bundle_dir))
    session.lifecycle = get_lifecycle()
    session.bundle_loaded = True


def _take_document(session: _Session, req: dict):
    """The frame's document (``procedure_json``) becomes the session's; a frame
    without one keeps it (ping/shutdown/bench frames need none).  An
    ``exec_ops`` frame without one is refused (see _dispatch): a batch runs
    only on the document sent with it, never on an earlier frame's."""
    doc = req.get("procedure_json")
    if isinstance(doc, dict):
        session.procedure_json = doc
        return None
    if doc is None and req.get("cmd") != "exec_ops":
        return None
    return {"ok": False, "kind": "NoProcedure",
            "error": "the frame carries no procedure document (procedure_json)"}


def _dispatch(session: _Session, req: dict):
    """Return ``(response, stop)``.  ``stop`` ends the loop (shutdown)."""
    cmd = req.get("cmd")
    try:
        if cmd == "op":
            return _sib("_engine_ops").run(req, lambda r: _load_bundle(session, r)), False
        if cmd == "shutdown":       # never waits on the packs: a failed load must not keep us alive
            return _cmd_shutdown(session, req), True
        _load_bundle(session, req)
        err = _take_document(session, req)
        if err and cmd == "exec_ops":
            # Only exec is gated on the document. safe_off/ping proceed with the last
            # one received or {} — a recovery safe-off must never be blocked (explicit channels).
            return err, False
        if cmd == "ping":
            return {"ok": True, "pong": True}, False
        if cmd == "exec_ops":       # in the frame's work_dir: relative writes land there
            with _bench().in_work_dir(req.get("work_dir")):
                return _cmd_exec_ops(session, req), False
        if cmd == "raw":
            return _cmd_raw(session, req), False
        if cmd == "call":           # a guided Call row's function (the sibling _guided_call)
            return _sib("_guided_call").cmd_call(globals(), req), False
        if cmd == "cancel_call":    # posted: a call already over has nothing to stop
            return None, False
        if cmd == "safe_off":
            return _cmd_safe_off(session, req), False
        if cmd in _BENCH_CMDS:
            return _bench().handle(globals(), session, req)
        return {"ok": False, "kind": "Protocol",
                "error": f"unknown cmd {cmd!r}"}, False
    except _NotRemote as exc:
        return {"ok": False, "kind": "NotRemote", "error": str(exc)}, False
    except _NoCodegen as exc:
        return {"ok": False, "kind": "NoCodegen", "error": str(exc)}, False
    except ImportError as exc:
        return {"ok": False, "kind": "Other",
                "error": f"import failed in project venv: {exc}",
                "traceback": traceback.format_exc()}, False
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "ExecError",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()}, False


def main() -> None:
    global _SESSION, _PROTO_OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("run", "tools"), required=True)
    role = parser.parse_args().role
    # Isolate the protocol channel at the OS level: dup the real stdout to a
    # private fd for frames, then point fd 1 (what C-level VISA/driver code calls
    # "stdout") at stderr so stray bytes can't corrupt a frame.  Python-level
    # stdout is also redirected to stderr.
    frame_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_out = os.fdopen(frame_fd, "w", buffering=1)
    _PROTO_OUT = proto_out

    _SESSION = _Session()
    atexit.register(_emergency_teardown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except Exception:  # noqa: BLE001 — not all signals settable on Windows
            pass

    # THE one reader thread + queue (bench-session S3), shared with in-run waits
    # (prompt/cancel); EOF comes through it, so the loop reaches the _teardown.
    reader = _bench().start_reader(sys.stdin)
    while True:
        line = reader.next_line()      # blocking; None = EOF (parent closed pipe)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:  # noqa: BLE001
            _write_frame(proto_out, {"ok": False, "kind": "Protocol",
                                     "error": f"bad frame: {exc}"})
            continue
        refused = _sib("_engine_ops").refusal(role, req)
        response, stop = (refused, False) if refused else _dispatch(_SESSION, req)
        if response is not None:       # a late prompt_response gets none
            _write_frame(proto_out, response)
        if stop:
            break
    # EOF / shutdown — final idempotent teardown (no-op if shutdown already ran).
    _teardown(_SESSION)


if __name__ == "__main__":
    main()
