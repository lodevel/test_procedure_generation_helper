"""Persistent daemon runner for executing procedure ops against live equipment.

Invoked by FILE PATH (NOT ``-m``) in the *project* venv::

    <project>/.venv/<bin>/python <abs>/_execute_op_subprocess.py

Running via ``-m`` would trigger ``workflow_editor/__init__.py`` (PySide6, absent
from the project venv); invoking by path keeps ``__name__ == "__main__"`` and
skips the package init.  Sibling to ``_pack_parsers_subprocess.py``, but where
that runner only imports ``rules_packager_base`` (the parser), this one ALSO
imports the hardware drivers (labscpi / fncore) — it is the only runner that
talks to a real instrument.

Why a daemon (not one-shot)
---------------------------
Standard equipment (psu/eload/scope) holds physical state, and on some
instruments (e.g. the EA PS 9080) ``close()`` releases the remote lock which
DROPS the output.  A one-shot connect->op->close therefore zeroes the bench
between every step.  So this runner stays alive for the whole guided-manual
session and holds a SINGLE session per "per_session" device, closing (and
unlocking) only at ``shutdown`` / interpreter exit.  Devices whose policy is
"per_step" (controllers, by default) keep the cheap connect->op->close-per-op
model — they can go down safely between steps.

NOTE: the matching persistent PARENT transport (pack_parsers) is the Phase-2
seam.  Until it speaks this framed protocol, this runner is unreachable — do NOT
drive any UI ⚡/run against it on real hardware yet.

Connection-lifecycle policy (per device)
-----------------------------------------
``bench_map[device]["session_policy"]`` in {"per_step","per_session"} selects
the model.  The dialog resolves it (default-by-type: controller -> per_step,
else per_session) and stamps it; this runner obeys, falling back to the same
default when unset.  per_session devices live in ``Session.held`` (opened once,
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

  Request  (one line) — every frame may carry "procedure_path" (abs path to
  procedure.json; the parent injects it per frame).  The document is
  json.load'ed ONCE per session from that path (latched like ``_bundle_dir``);
  frames never embed the document itself:
    {"cmd": "exec_ops", "_bundle_dir": "<abs>", "procedure_path": "<abs>",
     "ops": [{"node_path": str, "op": {...}}, ...],
     "bench_map": {device: {"visa"|"port"..., "session_policy": "..."}}}
    {"cmd": "safe_off", "_bundle_dir": "<abs>", "procedure_path": "<abs>",
     "bench_map": {...}, "safe_off": [{"device","etype","channels":[...]}, ...]}
    {"cmd": "shutdown"}        # safe-off all held + close (unlock) + exit
    {"cmd": "ping"}            # liveness probe

  Response (one line):
    exec_ops : {"ok": true, "results": [...], "aborted": bool, "log": [str],
                "unsafe": [str]?}     # unsafe = devices that could NOT power down
    safe_off : {"ok": true, "log": [str], "unsafe": [str]?}
    shutdown : {"ok": true, "log": [str], "unsafe": [str]?}   (then exits)
    error    : {"ok": false, "kind": "<NotRemote|NoCodegen|NoLifecycle
                |NoProcedure|ExecError|Protocol>", "error": str,
                "traceback": str?}

Crash safety: ``atexit`` + SIGTERM/SIGINT run the same teardown (safe-off ALL
held psu/eload by DECLARED channels, ELOAD-before-PSU, then close()->unlock).
On a hard kill (SIGKILL / Windows TerminateProcess) those do NOT run — the
PARENT is responsible for killing a wedged daemon and spawning a fresh
``safe_off`` runner.
"""
from __future__ import annotations

import atexit
import json
import numbers
import os
import signal
import sys
import traceback
from pathlib import Path


class _ResStub:
    """Minimal stand-in for the generated test.py ``Result`` object."""

    def __init__(self) -> None:
        self.log: list = []
        self.operator_verdicts: dict = {}

    def add_evidence(self, *args, **kwargs) -> None:  # scope.screenshot
        pass


class _NotRemote(Exception):
    """Device is not remote-capable (no address / in manual mode)."""


class _NoCodegen(Exception):
    """The owning pack produced no remote driver call for the op."""


def _build_device(bench: dict, etype: str, lifecycle: dict, log: list):
    """Construct the driver instance for ``etype`` from resolved bench values.

    The driver CLASS and its import come from the pack's own LIFECYCLE
    ``prelude_import`` block + ``driver_class`` name — so a new pack's driver
    works with no change here.  Connection params follow the two transports we
    support: VISA (psu/eload/scope) and serial (controller).
    """
    lc = lifecycle.get(etype) or {}
    class_name = lc.get("driver_class")
    if not class_name:
        raise RuntimeError(f"no driver_class declared for equipment type {etype!r}")
    ns: dict = {}
    prelude = ((lc.get("prelude_import") or {}).get("block")) or ""
    if prelude:
        exec(prelude, ns)  # noqa: S102 — pack-declared import block, not user input
    cls = ns.get(class_name)
    if cls is None:
        raise RuntimeError(
            f"driver class {class_name!r} for {etype!r} not importable in the "
            f"project venv (prelude import failed)."
        )
    if "visa" in bench:  # VISA transport: psu / eload / scope
        visa = bench.get("visa")
        if not visa:
            raise _NotRemote(f"{etype} has no VISA address set; cannot execute remotely.")
        timeout_ms = int(bench.get("timeout_ms") or 3000)
        return cls(visa, timeout_ms=timeout_ms)
    if "port" in bench:  # serial transport: controller
        port = bench.get("port")
        if not port:
            raise _NotRemote("controller has no serial PORT set; cannot execute remotely.")
        return cls(
            port=port,
            baud=int(bench.get("baud") or 115200),
            timeout_s=float(bench.get("timeout_s") or 2.0),
            log_list=log,
            manual_override=bool(bench.get("manual_override")),
        )
    raise _NotRemote(f"no usable connection parameters for {etype!r}.")


def _open_no_reset(dev) -> None:
    """Open a session WITHOUT resetting: connect()/open() + initialize() only.

    Deliberately never calls ``reset()`` — that is the sole ``*RST`` path and
    the whole point of this runner.  ``initialize()`` is required (it binds the
    SCPI adapter the op methods assert on) and itself issues no reset.
    """
    if hasattr(dev, "connect"):
        dev.connect()
    elif hasattr(dev, "open"):
        dev.open()
    if hasattr(dev, "initialize"):
        dev.initialize()
    # NOTE: intentionally NO dev.reset() — leave device state as-is.


def _capture_remote_lines(procedure_json: dict, target_op: dict):
    """Drive the owning pack's ``emit_python`` through a capture-context.

    Returns ``(var_name, lines, meta)`` where ``lines`` is the remote branch's
    driver call(s) and ``meta`` carries ``ref``/``unit``/``has_result`` for a
    measuring op.  No test.py scaffolding (if/else, prompts) is emitted — only
    the remote driver call(s).
    """
    from rules_packager_base.rules.v2_0_2.parser.codegen import _Generator
    from rules_packager_base.rules.v2_0_2.parser._default_registry import (
        get_default_pack_parsers,
    )

    gen = _Generator(procedure_json, None)
    lines: list = []
    meta = {"ref": None, "unit": "", "has_result": False}

    gen.emit = lambda line="": lines.append(line)

    def _cap_action(eid, remote_line, manual_msg):
        lines.append(remote_line)

    def _cap_measure(eid, ref_key, remote_expr, manual_msg, default_unit="V", label=""):
        meta.update(ref=ref_key, unit=default_unit, has_result=True)
        lines.append(f"__RESULT__ = ({remote_expr})")

    def _cap_query(eid, ref_key, remote_expr, manual_msg):
        meta.update(ref=ref_key, unit="", has_result=True)
        lines.append(f"__RESULT__ = ({remote_expr})")

    def _cap_block(eid, manual_msg, remote_emitter):
        remote_emitter()

    def _cap_block_measure(eid, ref_key, manual_msg, unit, label, remote_emitter):
        meta.update(ref=ref_key, unit=unit, has_result=True)
        remote_emitter()  # emits via gen.emit, incl. record_measurement(...)

    gen._emit_remote_or_manual_action = _cap_action
    gen._emit_remote_or_manual_measure = _cap_measure
    gen._emit_remote_or_manual_query = _cap_query
    gen._emit_remote_or_manual_block = _cap_block
    gen._emit_remote_or_manual_block_measure = _cap_block_measure

    op_const = target_op.get("op") or ""
    etype = op_const.split(".", 1)[0]
    pack = get_default_pack_parsers().get(etype)
    emit_python = getattr(pack, "emit_python", None) if pack else None
    if emit_python is None or not emit_python(target_op, gen._ctx):
        raise _NoCodegen(
            f"pack for {etype!r} did not emit a driver call for op {op_const!r}."
        )

    var_name = gen.device_vars.get(target_op.get("device"), target_op.get("device"))
    return var_name, lines, meta


# ---------------------------------------------------------------------------
# State-changing / safe-off helpers.
# ---------------------------------------------------------------------------

_READ_VERBS = {"measure_voltage", "measure_current", "measure_power",
               "get_voltage", "get_current", "get_power"}
# NB: query_raw / write_raw are deliberately NOT reads here — a raw SCPI command
# MAY assert an output, so they count as state-changing for energized-tracking.
# Over-tracking is harmless: it only adds a redundant safe-off on a failed batch.


def _is_state_changing(op: dict, lifecycle: dict) -> bool:
    """True iff this op can leave a held instrument energized: a non-read verb on
    a device whose pack declares a ``cleanup`` (i.e. holds physical output).

    Driven by lifecycle METADATA, not a hardcoded type list — so a new
    state-holding instrument (e.g. a signal generator) is tracked automatically
    once its pack declares a ``cleanup``, with no edit here.  Unknown verbs
    default to state-changing (errs toward a redundant, harmless safe-off)."""
    etype, _, verb = (op.get("op") or "").partition(".")
    if not (lifecycle.get(etype) or {}).get("cleanup"):
        return False        # scope/controller (no output) — never energizing
    return verb not in _READ_VERBS


def _device_channels(procedure_json: dict, device: str) -> list:
    """Declared channel numbers for a device (from the procedure equipment), so a
    channel-less state-changer (raw SCPI / tracking) can be safe-off'd on every
    channel rather than just CH1. Falls back to [1] when undeterminable."""
    for eq in (procedure_json.get("equipment") or []):
        if eq.get("id") != device:
            continue
        out = []
        for c in (eq.get("channels") or []):
            if isinstance(c, int):
                out.append(c)
            elif isinstance(c, dict):
                cid = c.get("id", c.get("channel", c.get("n")))
                if isinstance(cid, int):
                    out.append(cid)
        return sorted(set(out)) or [1]
    return [1]


def _ns_base(res) -> dict:
    return {
        "results": {}, "res": res, "manual_flags": {}, "_stats": None,
        "float": float, "int": int, "str": str, "bool": bool,
        "abs": abs, "round": round, "min": min, "max": max,
    }


def _jsonable(value):
    """Coerce a measured value to a JSON-native type so it round-trips through
    the frame (a numpy scalar / Decimal would otherwise be stringified by
    json.dumps(default=str) and silently mis-compare against a float verdict)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Real) and not isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def _exec_op(procedure_json: dict, op: dict, ns: dict):
    """Capture + exec one op's remote driver line(s) in the shared namespace
    (all opened device vars stay bound across ops). Returns (has, value, unit,
    ref). A fresh per-op holder + cleared ``__RESULT__`` prevent cross-op bleed."""
    holder: dict = {}

    def _record_measurement(results, ref, value, *, unit="", label="", res=None):
        holder["value"] = value
        holder["unit"] = unit
        holder["ref"] = ref

    ns["record_measurement"] = _record_measurement
    ns.pop("__RESULT__", None)
    _var, lines, meta = _capture_remote_lines(procedure_json, op)
    exec("\n".join(lines), ns)  # noqa: S102 — pack-generated driver call(s)
    if "__RESULT__" in ns:
        return True, _jsonable(ns["__RESULT__"]), meta["unit"], meta["ref"]
    if "value" in holder:
        ref = meta["ref"] if meta["ref"] is not None else holder.get("ref")
        return True, _jsonable(holder["value"]), holder.get("unit", ""), ref
    return False, None, "", meta["ref"]


def _safe_off(procedure_json, ns, energized, etype_of, lifecycle, res) -> list:
    """Drive energized PSU/ELOAD outputs OFF in cleanup-priority order (ELOAD
    priority 10 before PSU priority 20).  Reuses the pack emit by synthesizing an
    ``<etype>.<off_verb> … on=False`` op per touched channel.  Best-effort — a
    failure to turn one off is logged AND returned (the caller surfaces it as
    ``unsafe`` so a bench left energized is never silent).

    ``off_verb`` is the OP-LEVEL verb (default "output", the convention psu/eload
    follow), pack-overridable via ``cleanup.off_op``.  It is NOT the same as
    ``cleanup.off_method`` — that is the *driver method* name used by codegen
    (eload's is ``set_output``), which is not a valid op verb here."""
    items = []
    for device, channels in energized.items():
        etype = etype_of.get(device, "")
        cleanup = (lifecycle.get(etype) or {}).get("cleanup")
        if not cleanup:                # scope/controller: nothing to power down
            continue
        off_verb = cleanup.get("off_op", "output")
        items.append((cleanup.get("priority", 100), etype, device, channels, off_verb))
    failures: list = []
    for _prio, etype, device, channels, off_verb in sorted(items, key=lambda t: t[0]):
        known = sorted(c for c in channels if c is not None)
        if None in channels:   # a channel-less state-changer (raw SCPI / tracking)
            known = sorted(set(known) | set(_device_channels(procedure_json, device)))
        for ch in (known or [1]):
            off_op = {"op": f"{etype}.{off_verb}", "device": device,
                      "channel": ch, "on": False}
            try:
                _exec_op(procedure_json, off_op, ns)
                res.log.append(f"safe-off {device} CH{ch}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{device} CH{ch}")
                res.log.append(f"UNSAFE: safe-off {device} CH{ch} FAILED: {exc}")
    return failures


# ---------------------------------------------------------------------------
# Session: persists across commands for the whole guided-manual run.
# ---------------------------------------------------------------------------


def _policy_for(bench: dict, etype: str) -> str:
    """Resolved connection-lifecycle policy for a device.  The dialog stamps
    ``session_policy``; this is the belt-and-suspenders default-by-type fallback
    (controller -> per_step, every other equipment -> per_session)."""
    p = (bench or {}).get("session_policy")
    if p in ("per_step", "per_session"):
        return p
    return "per_step" if etype == "controller" else "per_session"


class _Session:
    def __init__(self) -> None:
        self.res = _ResStub()
        self.ns = _ns_base(self.res)
        self.held: dict = {}        # device -> driver instance (per_session)
        self.etype_of: dict = {}    # device -> equipment type (held only)
        self.energized: dict = {}   # device -> {channels} touched (held only)
        self.lifecycle: dict = {}
        self.bundle_loaded = False
        self.procedure_json: dict | None = None
        self._in_teardown = False   # run-once latch (also signal re-entrancy guard)


def _safe_name(device: str) -> str:
    from rules_packager_base.rules.v2_0_2.parser.codegen_helpers import _safe_name as _sn
    return _sn(device)


def _ensure_held(session: _Session, bench_map: dict, device: str, etype: str) -> None:
    """Open a per_session device ONCE (no reset) into ``held`` + bind it in the
    shared namespace.  Idempotent — a held device is reused across commands."""
    if device in session.held:
        return
    if device not in bench_map:
        raise _NotRemote(f"no connection parameters for {device!r}.")
    dev = _build_device(bench_map[device], etype, session.lifecycle, session.res.log)
    _open_no_reset(dev)
    session.held[device] = dev
    session.etype_of[device] = etype
    session.ns[_safe_name(device)] = dev


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


def _safe_off_all_held(session: _Session) -> list:
    """Safe-off EVERY held psu/eload by its DECLARED channels (a superset of the
    tracked energized set), ELOAD-before-PSU.  Returns the ``unsafe`` list of
    devices that could not be powered down.  Used by shutdown / atexit / signal."""
    declared: dict = {}
    for device, etype in list(session.etype_of.items()):
        if device not in session.held:
            continue
        cleanup = (session.lifecycle.get(etype) or {}).get("cleanup")
        if cleanup:
            declared[device] = set(_device_channels(session.procedure_json or {}, device))
    unsafe: list = []
    if declared:
        try:
            unsafe = _safe_off(session.procedure_json or {}, session.ns, declared,
                               session.etype_of, session.lifecycle, session.res)
        except Exception as exc:  # noqa: BLE001
            session.res.log.append(f"UNSAFE: safe-off-all FAILED: {exc}")
            unsafe = sorted(declared)
    session.energized.clear()
    return unsafe


def _close_held(session: _Session) -> None:
    """Close (and on EA, unlock via the driver's shutdown) every held device.
    The ONLY place a held device is closed — the chokepoint."""
    for device in list(session.held.keys()):
        dev = session.held.pop(device)
        try:
            if hasattr(dev, "close"):
                dev.close()
        except Exception as exc:  # noqa: BLE001
            session.res.log.append(f"close() failed: {exc}")
        session.ns.pop(_safe_name(device), None)
        session.etype_of.pop(device, None)
        session.energized.pop(device, None)


def _teardown(session: _Session | None) -> list:
    """Idempotent, run-once full teardown: safe-off all held outputs, then
    close+unlock.  The per-session ``_in_teardown`` latch makes it run exactly
    once and lets a signal landing mid-teardown bail without re-entering VISA
    I/O.  Per-session (not module-global) so a reused process — i.e. the test
    suite — tears down each session independently."""
    if session is None or session._in_teardown:
        return []
    session._in_teardown = True
    unsafe = _safe_off_all_held(session)
    _close_held(session)
    return unsafe


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
    """A hard, visible failure if no procedure document was ever latched —
    exec must not run without it (channel resolution / safe-off would silently
    degrade to guesses)."""
    if session.procedure_json is None:
        return {"ok": False, "kind": "NoProcedure",
                "error": "no procedure document loaded (procedure_path missing "
                         "or unreadable on every frame); refusing to execute ops."}
    return None


def _cmd_exec_ops(session: _Session, req: dict) -> dict:
    guard = _require_lifecycle(session) or _require_procedure(session)
    if guard:
        return guard
    procedure_json = session.procedure_json
    ops = req.get("ops") or []
    bench_map = req.get("bench_map") or {}
    log_mark = len(session.res.log)
    results: list = []
    failed = False
    for entry in ops:
        op = entry.get("op") or {}
        node_path = entry.get("node_path", "")
        device = op.get("device")
        etype = (op.get("op") or "").split(".", 1)[0]
        policy = _policy_for(bench_map.get(device, {}), etype)
        try:
            if policy == "per_session":
                _ensure_held(session, bench_map, device, etype)
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
            failed = True
            results.append({"node_path": node_path, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
            break
    unsafe: list = []
    if failed and session.energized:
        # Abnormal exit: return the bench to safety (outputs off) but KEEP the
        # held session open — the operator can re-arm.  per_step devices are
        # already closed (per-op).
        unsafe = _safe_off(procedure_json, session.ns, session.energized,
                           session.etype_of, session.lifecycle, session.res)
        session.energized.clear()
    resp = {"ok": True, "results": results, "aborted": failed,
            "log": session.res.log[log_mark:]}
    if unsafe:
        resp["unsafe"] = unsafe
    return resp


def _cmd_safe_off(session: _Session, req: dict) -> dict:
    """Turn specified outputs OFF, keeping devices held (the session continues).
    Doubles as the fresh-recovery path: a brand-new daemon opens the targets
    here, offs them, and a following ``shutdown`` closes+unlocks them."""
    guard = _require_lifecycle(session)
    if guard:
        return guard
    # Tolerates a never-latched document ({}): recovery safe-off must not be
    # blocked — targets carry explicit channels, the document is only the
    # declared-channels fallback for channel-less state-changers.
    procedure_json = session.procedure_json or {}
    bench_map = req.get("bench_map") or {}
    targets = req.get("safe_off") or []   # [{"device","etype","channels":[...]}]
    log_mark = len(session.res.log)
    todo: dict = {}
    for t in targets:
        device, etype = t.get("device"), t.get("etype")
        try:
            _ensure_held(session, bench_map, device, etype)
            todo[device] = set(t.get("channels") or [1])
        except Exception as exc:  # noqa: BLE001
            session.res.log.append(f"UNSAFE: safe-off open {device} FAILED: {exc}")
    unsafe: list = []
    if todo:
        unsafe = _safe_off(procedure_json, session.ns, todo, session.etype_of,
                           session.lifecycle, session.res)
        for d in todo:
            session.energized.pop(d, None)
    resp = {"ok": True, "log": session.res.log[log_mark:]}
    if unsafe:
        resp["unsafe"] = unsafe
    return resp


def _cmd_shutdown(session: _Session, req: dict) -> dict:
    del req  # teardown uses the latched session.procedure_json (or none — fine)
    log_mark = len(session.res.log)
    unsafe = _teardown(session)
    resp = {"ok": True, "log": session.res.log[log_mark:]}
    if unsafe:
        resp["unsafe"] = unsafe
    return resp


# ---------------------------------------------------------------------------
# Framed NDJSON protocol + crash-safety teardown.
# ---------------------------------------------------------------------------

_SESSION: _Session | None = None


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


def _load_procedure(session: _Session, req: dict):
    """Latch the procedure document ONCE per session from the frame's
    ``procedure_path`` (sibling of :func:`_load_bundle`). The document is
    frozen at the first frame that carries the key — a file regenerated
    mid-session is deliberately NOT re-read. A frame without the key is
    fine (ping/shutdown need no document); a path that cannot be read (or
    holds a non-dict) returns an error response — fatal for exec_ops
    ONLY (see _dispatch)."""
    if session.procedure_json is not None:
        return None
    path = req.get("procedure_path")
    if not path:
        return None
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "kind": "NoProcedure",
                "error": f"cannot read procedure file {path!r}: {exc}"}
    if not isinstance(doc, dict):
        return {"ok": False, "kind": "NoProcedure",
                "error": f"procedure file {path!r} is not a JSON object"}
    session.procedure_json = doc
    return None


def _dispatch(session: _Session, req: dict):
    """Return ``(response, stop)``.  ``stop`` ends the loop (shutdown)."""
    cmd = req.get("cmd")
    try:
        _load_bundle(session, req)
        err = _load_procedure(session, req)
        if err and cmd == "exec_ops":
            # Only exec is gated on the document. safe_off/shutdown/ping
            # proceed with the latched copy or {} — a recovery safe-off
            # must run even when procedure.json vanished mid-session
            # (its targets carry explicit channels).
            return err, False
        if cmd == "ping":
            return {"ok": True, "pong": True}, False
        if cmd == "exec_ops":
            return _cmd_exec_ops(session, req), False
        if cmd == "safe_off":
            return _cmd_safe_off(session, req), False
        if cmd == "shutdown":
            return _cmd_shutdown(session, req), True
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
    global _SESSION
    # Isolate the protocol channel at the OS level: dup the real stdout to a
    # private fd for frames, then point fd 1 (what C-level VISA/driver code calls
    # "stdout") at stderr so stray bytes can't corrupt a frame.  Python-level
    # stdout is also redirected to stderr.
    frame_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_out = os.fdopen(frame_fd, "w", buffering=1)

    _SESSION = _Session()
    atexit.register(_emergency_teardown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except Exception:  # noqa: BLE001 — not all signals settable on Windows
            pass

    stdin = sys.stdin
    while True:
        line = stdin.readline()        # readline is partial-read safe (full line or EOF)
        if not line:                   # EOF — parent closed the pipe
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
        response, stop = _dispatch(_SESSION, req)
        _write_frame(proto_out, response)
        if stop:
            break
    # EOF / shutdown — final idempotent teardown (no-op if shutdown already ran).
    _teardown(_SESSION)


if __name__ == "__main__":
    main()
