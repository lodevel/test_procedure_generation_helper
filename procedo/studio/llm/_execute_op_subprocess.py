"""Persistent daemon runner for executing procedure ops against live equipment.

Invoked by FILE PATH (NOT ``-m``) in the *project* venv::

    <machine-local venv>/<bin>/python <abs>/_execute_op_subprocess.py --role run|tools

Running via ``-m`` would trigger ``procedo/studio/__init__.py`` (PySide6, absent
from the project venv); invoking by path keeps ``__name__ == "__main__"`` and
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
The daemon is the only party that knows off-confirmation truth, so it keeps
the memory: ``_Session.unconfirmed_off`` records every safe-off target whose
OFF failed or errored — recorded AT the failure site (a per-channel off
failure in ``_safe_off``, an open/rebuild failure in ``_cmd_safe_off``, a
held-endpoint rebuild failure in ``check_held``) BEFORE any handle drop can
hide the target, keyed by the canonical endpoint when the bench session
routed the name (``BenchState.alias``).  An entry carries the channels plus
the admission bench data needed to reconnect and is removed ONLY when a
later off of its recorded channels is CONFIRMED (the off op executed without
error) — never by a handle drop, a failed rebuild, or a rebuild that merely
succeeded.  Every later safe-off pass retries: ``safe_off`` retries ALL
recorded targets first (restoring held routing before the frame's own
targets run), and the teardown/shutdown sweep retries every recorded target
its held pass did not attempt — reconnecting from the stored bench when the
target is no longer held — so shutdown reports a still-unconfirmed target in
``unsafe`` even when nothing holds it, instead of false-clean, and the
host's persistent UNSAFE latch survives ``BenchSession.fatal()`` until those
exact targets are confirmed off.  Responses surface the memory in BOTH
forms — display strings in ``unsafe`` plus the structured ``unsafe_targets``
records (:func:`_attach_unsafe`) — so the host recovery receives the exact
endpoint/channel set with its stored reconnect data, never a parsed string.
"""
from __future__ import annotations

import argparse
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
    support: VISA (psu/eload/scope) and serial (fncore controller).
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
    if "port" in bench:  # serial transport (fncore controller)
        port = bench.get("port")
        if not port:
            raise _NotRemote(f"{etype} has no serial PORT set; cannot execute remotely.")
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


def _raw_send(dev, text: str) -> str:
    """Send ONE raw command line to an already-open device and return its reply.

    A line containing ``?`` is a QUERY (returns the instrument's response); any
    other line is a WRITE (returns ``""``).  This is the single raw-command
    adapter seam, dispatching on the FIRST interface a driver exposes, in the
    same order as the code below:

    1. a public raw passthrough — ``query_raw`` / ``write_raw`` (psu/eload/scope
       all expose these);
    2. the fncore line protocol — the public ``raw_command`` (the full reply,
       one line or many);
    3. (defensive) an SCPI-session wrapper ``.s`` / ``._session``;
    4. (defensive) a bare pyvisa ``_resource`` / ``resource``.

    Tiers 3-4 are fallbacks for a driver exposing neither public surface; every
    SHIPPED driver resolves at tier 1 or 2.  Dispatch is by attribute presence
    only — no driver class or pack-namespace names — so a new pack's driver is
    terminal-addressable by exposing the public ``query_raw``/``write_raw`` (or a
    multi-line ``raw_command``), and the seam needs no per-driver branch here.
    """
    is_query = "?" in text
    # 1. public raw passthrough (psu/eload/scope: query_raw / write_raw)
    if is_query and hasattr(dev, "query_raw"):
        return str(dev.query_raw(text))
    if (not is_query) and hasattr(dev, "write_raw"):
        dev.write_raw(text)
        return ""
    # 2. fncore line protocol: raw_command reads up to the board's prompt, so a
    #    terminal command like 'help'/'listPins' comes back whole.
    if hasattr(dev, "raw_command"):
        return str(dev.raw_command(text))
    # 3. SCPI-session wrapper (psu/eload: .s ; scope: ._session) — handles
    #    read/write terminations + error checking the bare resource would not.
    sess = getattr(dev, "s", None) or getattr(dev, "_session", None)
    if sess is not None and hasattr(sess, "query") and hasattr(sess, "write"):
        if is_query:
            return str(sess.query(text))
        sess.write(text)
        return ""
    # 4. last resort: the raw pyvisa resource
    res = getattr(dev, "_resource", None) or getattr(dev, "resource", None)
    if res is not None and hasattr(res, "query") and hasattr(res, "write"):
        if is_query:
            return str(res.query(text))
        res.write(text)
        return ""
    raise RuntimeError(
        f"{type(dev).__name__} exposes no raw-command interface "
        f"(query_raw/write_raw, raw_command, .s/._session, or a resource).")


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

    # Mirror _Generator.emit's indentation: a pack that emits a BLOCK via
    # ctx.emit_block("try:")/end_block bumps gen.indent, and the body lines must
    # be indented or the captured snippet won't compile (bodyless 'try:').
    gen.emit = lambda line="": lines.append(("    " * gen.indent + line) if line else "")

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
        return False        # scope/fncore (no output) — never energizing
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
    from rules_packager_base import bytes_to_int   # a pack's bus read binds its bytes through it
    return {
        "results": {}, "res": res, "manual_flags": {}, "_stats": None,
        "float": float, "int": int, "str": str, "bool": bool,
        "abs": abs, "round": round, "min": min, "max": max,
        "bytes_to_int": bytes_to_int,
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


def _safe_off(procedure_json, ns, energized, etype_of, lifecycle, res,
              session=None, bench_map=None) -> list:
    """Drive energized PSU/ELOAD outputs OFF in cleanup-priority order (ELOAD
    priority 10 before PSU priority 20).  Reuses the pack emit by synthesizing an
    ``<etype>.<off_verb> … on=False`` op per touched channel.  Best-effort — a
    failure to turn one off is logged AND returned (the caller surfaces it as
    ``unsafe`` so a bench left energized is never silent).

    With ``session`` set, this is also the unconfirmed-off chokepoint (module
    docstring): a per-channel failure is RECORDED at this site — before any
    handle drop can hide the target — and a per-channel success CONFIRMS
    (removes) the matching record.  ``bench_map`` supplies the reconnect data
    for name-keyed devices the record would otherwise lack.

    ``off_verb`` is the OP-LEVEL verb (default "output", the convention psu/eload
    follow), pack-overridable via ``cleanup.off_op``.  It is NOT the same as
    ``cleanup.off_method`` — that is the *driver method* name used by codegen
    (eload's is ``set_output``), which is not a valid op verb here."""
    items = []
    for device, channels in energized.items():
        etype = etype_of.get(device, "")
        cleanup = (lifecycle.get(etype) or {}).get("cleanup")
        if not cleanup:                # scope/fncore: nothing to power down
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
                if session is not None:
                    _confirm_off(session, device, ch)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{device} CH{ch}")
                res.log.append(f"UNSAFE: safe-off {device} CH{ch} FAILED: {exc}")
                if session is not None:
                    _record_unconfirmed(session, device, etype, {ch},
                                        (bench_map or {}).get(device))
    return failures


# ---------------------------------------------------------------------------
# Session: persists across commands for the whole guided-manual run.
# ---------------------------------------------------------------------------


def _policy_for(bench: dict, namespace: str, lifecycle: dict) -> str:
    """Resolved connection-lifecycle policy for a device.  The GUI-stamped
    ``session_policy`` is authoritative; the fallback is lifecycle metadata, not
    a hardcoded namespace list: a pack declaring ``remote: False`` (serial, e.g.
    fncore) drops safely between steps -> per_step; every other (or unknown)
    namespace may hold physical state -> per_session."""
    p = (bench or {}).get("session_policy")
    if p in ("per_step", "per_session"):
        return p
    if (lifecycle.get(namespace) or {}).get("remote") is False:
        return "per_step"
    return "per_session"


def _namespace_for(etype: str, subtype: str = "") -> str:
    """Map a DECLARED equipment type to its pack op-NAMESPACE — the key the
    lifecycle / driver_class is registered under (e.g. ``controller`` -> the
    ``fncore`` namespace).  The exec path derives this from the op-const prefix;
    a raw terminal command has no op, so resolve it via the equipment dispatch.
    Falls back to the type itself, correct for packs where namespace == type
    (psu/eload/scope)."""
    try:
        from rules_packager_base.rules.v2_0_2.parser._default_registry import (
            get_namespace,
        )
        return get_namespace(etype, subtype) or etype
    except Exception:  # noqa: BLE001 — pre-dispatch bundle / unclaimed type
        return etype


class _Session:
    def __init__(self) -> None:
        self.res = _ResStub()
        self.ns = _ns_base(self.res)
        self.held: dict = {}        # device -> driver instance (per_session)
        self.etype_of: dict = {}    # device -> equipment type (held only)
        self.energized: dict = {}   # device -> {channels} touched (held only)
        self.lifecycle: dict = {}
        self.bundle_loaded = False
        self.procedure_json: dict | None = None   # the last document a frame carried
        self.bench = None           # BenchState (bench-session runs; sibling-owned)
        # device/endpoint -> {"etype","channels","bench"}: safe-off targets
        # whose OFF was never confirmed (module docstring: unconfirmed-off
        # memory).  Removed ONLY by _confirm_off; survives handle drops.
        self.unconfirmed_off: dict = {}
        self._in_teardown = False   # run-once latch (also signal re-entrancy guard)


def _safe_name(device: str) -> str:
    from rules_packager_base.rules.v2_0_2.parser.codegen_helpers import _safe_name as _sn
    return _sn(device)


def _ensure_held(session: _Session, bench_map: dict, device: str, etype: str,
                 probe_refuse: bool = True, identity_gate: bool = True) -> str:
    """Open a per_session device ONCE (no reset) into ``held`` + bind it in the
    shared namespace.  Idempotent — a held device is reused across commands.
    RETURNS the hold key the instance lives under in ``held`` — the routed
    canonical endpoint on the reuse path, else ``device`` (Seam B's key).
    Resolution rule (owner ruling 2026-08-27, sibling ``_hold_resolution``):
    ``device`` is whatever its CURRENT bench dict resolves to — the dict is
    folded to its canonical endpoint FIRST; a held endpoint (active run-view
    claim, or the fold) reuses that instance; a name-keyed hold PROVABLY at a
    different endpoint than the fold is stale and never returned — its
    energized channels are recorded unconfirmed-off under the OLD endpoint,
    the instance closes via the ``_close_held_device`` chokepoint, and the
    name re-opens fresh here.  Unprovable staleness (old wheel, no address,
    no record) keeps today's name-keyed reuse; every reuse REBINDS the
    namespace slot, so the instance returned is the instance executed.
    The F-H6 probe and one-rebuild always run on the routed reuse; the two
    flags select which refusals a context retains:
    * ``probe_refuse=False`` (safe-off / raw-terminal contexts) suppresses
      StateLost — the caller proceeds on the rebuilt instance (keep_state
      semantics apply to neither a safety action nor a terminal line);
    * ``identity_gate=False`` (safe-off context ONLY) additionally ignores
      IdentityChanged, sticky or fresh — a safety action offs whatever
      instrument answers.  The raw context keeps the gate ON: a raw line
      still refuses, typed, on an unresolved identity mismatch.
    A rebuild failure still propagates as the ordinary error it always was."""
    key = _hold().resolve(globals(), session, bench_map, device, etype,
                          probe_refuse, identity_gate)
    if key is not None:
        return key
    if device not in bench_map:
        raise _NotRemote(f"no connection parameters for {device!r}.")
    dev = _build_device(bench_map[device], etype, session.lifecycle, session.res.log)
    _open_no_reset(dev)
    session.held[device] = dev
    session.etype_of[device] = etype
    session.ns[_safe_name(device)] = dev
    _hold().record_open(session, device, bench_map[device], etype)
    return device


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


def _record_unconfirmed(session: _Session, device, etype: str, channels,
                        bench: dict | None = None) -> None:
    """Record one safe-off target whose OFF failed or errored (unconfirmed-off
    memory, module docstring).  Called AT the failure site so a later handle
    drop can never hide the target.  A bench-session name is folded onto its
    claimed canonical endpoint (``alias``) so the endpoint-keyed and
    name-keyed failure sites merge into ONE reconnectable record; only
    cleanup-bearing (power) equipment is recorded — a scope/controller has
    nothing to confirm off."""
    if not (session.lifecycle.get(etype) or {}).get("cleanup"):
        return
    bench = dict(bench) if bench else None
    if session.bench is not None:
        routed = session.bench.alias.get(device)
        if routed is not None:
            device = routed
            bench = dict(session.bench.bench_of.get(routed) or bench or {}) or None
    entry = session.unconfirmed_off.setdefault(
        device, {"etype": etype, "channels": set(), "bench": None})
    entry["channels"] |= {c for c in channels if c is not None} or {1}
    if bench and entry["bench"] is None:
        entry["bench"] = bench
    elif entry["bench"] is None and session.bench is not None:
        stored = session.bench.bench_of.get(device)
        if stored:
            entry["bench"] = dict(stored)


def _confirm_off(session: _Session, device, ch) -> None:
    """A safe-off of ``device`` CH``ch`` executed without error: that channel
    is CONFIRMED off — the ONLY event that shrinks/removes an
    unconfirmed-off record (exact key: a confirmation never guesses across
    name/endpoint aliases)."""
    entry = session.unconfirmed_off.get(device)
    if entry is None:
        return
    entry["channels"].discard(ch)
    if not entry["channels"]:
        del session.unconfirmed_off[device]


def _retry_unconfirmed(session: _Session, devices) -> list:
    """Second-chance OFF for recorded unconfirmed-off targets in ``devices``:
    a still-held target is driven directly; one no longer held is RECONNECTED
    from its stored admission/bench data first (never refusing — safe-off
    context flags).  Success confirms and removes the record (inside
    ``_safe_off``); anything still unconfirmed comes back as the usual
    ``"device CHn"`` unsafe strings so no caller can report false-clean."""
    unsafe: list = []
    for device in sorted(devices):
        entry = session.unconfirmed_off.get(device)
        if entry is None:
            continue
        chans = set(entry["channels"]) or {1}
        if device not in session.held:
            bench = entry.get("bench")
            if not bench:
                session.res.log.append(
                    f"UNSAFE: {device} off never confirmed and no stored "
                    "connection data to retry")
                unsafe.extend(f"{device} CH{ch}" for ch in sorted(chans))
                continue
            try:
                _ensure_held(session, {device: bench}, device, entry["etype"],
                             probe_refuse=False, identity_gate=False)
            except Exception as exc:  # noqa: BLE001
                session.res.log.append(
                    f"UNSAFE: retry reconnect {device} FAILED: {exc}")
                unsafe.extend(f"{device} CH{ch}" for ch in sorted(chans))
                continue
        unsafe.extend(_safe_off(
            session.procedure_json or {}, session.ns, {device: chans},
            {**session.etype_of, device: entry["etype"]},
            session.lifecycle, session.res, session=session))
    return unsafe


def _attach_unsafe(session: _Session, resp: dict, unsafe: list) -> dict:
    """Attach a nonempty unsafe report to a response in BOTH forms: the
    display strings (``unsafe``) plus ``unsafe_targets`` — the structured
    snapshot of the unconfirmed-off memory (endpoint/device, etype,
    channels, stored reconnect bench or None) that the host recovery
    consumes without parsing strings.  The snapshot is the memory's CURRENT
    truth, so it may name residuals older than this pass's strings."""
    if unsafe:
        resp["unsafe"] = unsafe
        resp["unsafe_targets"] = [
            {"device": device, "etype": rec["etype"],
             "channels": sorted(rec["channels"]) or [1],
             "bench": dict(rec["bench"]) if rec["bench"] else None}
            for device, rec in sorted(session.unconfirmed_off.items())]
    return resp


def _safe_off_all_held(session: _Session) -> list:
    """Safe-off EVERY held psu/eload by its DECLARED channels (a superset of the
    tracked energized set), ELOAD-before-PSU.  Returns the ``unsafe`` list of
    devices that could not be powered down.  Used by shutdown / atexit / signal.

    Also RETRIES every recorded unconfirmed-off target the held sweep did not
    attempt — reconnecting from stored bench data when nothing holds it — so
    a shutdown after a failed-and-dropped off still reports that target in
    ``unsafe`` (never false-clean), and a retry that genuinely confirms the
    off makes the clean report legitimate."""
    prior = set(session.unconfirmed_off)
    declared: dict = {}
    for device, etype in list(session.etype_of.items()):
        if device not in session.held:
            continue
        cleanup = (session.lifecycle.get(etype) or {}).get("cleanup")
        if cleanup:
            chans = set(_device_channels(session.procedure_json or {}, device))
            if session.bench is not None:  # + UNION of channels seen across runs
                chans |= session.bench.channels_for(session, device)
            declared[device] = chans
    unsafe: list = []
    if declared:
        try:
            unsafe = _safe_off(session.procedure_json or {}, session.ns, declared,
                               session.etype_of, session.lifecycle, session.res,
                               session=session)
        except Exception as exc:  # noqa: BLE001
            session.res.log.append(f"UNSAFE: safe-off-all FAILED: {exc}")
            for device, chans in declared.items():
                _record_unconfirmed(session, device,
                                    session.etype_of.get(device, ""), chans)
            unsafe = sorted(declared)
    session.energized.clear()
    unsafe.extend(_retry_unconfirmed(session, prior - set(declared)))
    # Backstop: NEVER report clean while any off is unconfirmed — e.g. a
    # still-held device with a recorded channel outside its declared set
    # was neither swept nor retried above.
    leftover = [f"{d} CH{c}"
                for d, rec in sorted(session.unconfirmed_off.items())
                for c in sorted(rec["channels"])]
    unsafe.extend(x for x in leftover if x not in unsafe)
    return unsafe


def _close_held_device(session: _Session, device) -> None:
    """Close + forget ONE held device so a later op re-opens it fresh. The single
    chokepoint where a held device is actually closed — used by the full teardown
    AND after a per_session op error (a broken handle must not be reused)."""
    dev = session.held.pop(device, None)
    if dev is not None:
        try:
            if hasattr(dev, "close"):
                dev.close()
        except Exception as exc:  # noqa: BLE001
            session.res.log.append(f"close() failed: {exc}")
    session.ns.pop(_safe_name(device), None)
    session.etype_of.pop(device, None)
    session.energized.pop(device, None)
    if session.bench is not None:
        # Timeout-retarget bookkeeping only (safe inside shared/safety
        # paths): the live instance for this key is gone — a rebuilt one
        # opens from bench_of, so the next retarget seam must RE-APPLY
        # instead of skipping stale.  Endpoint keys reach here ONLY via the
        # check_held rebuild (per-op-error/_cmd_raw drops are NAME-keyed).
        session.bench.applied_timeout.pop(device, None)


def _close_held(session: _Session) -> None:
    """Close (and on EA, unlock via the driver's shutdown) every held device."""
    for device in list(session.held.keys()):
        _close_held_device(session, device)


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
    """A hard, visible failure without a procedure document — exec must not run
    without it (channel resolution / safe-off would silently degrade to guesses)."""
    if session.procedure_json is None:
        return {"ok": False, "kind": "NoProcedure",
                "error": "no procedure document (procedure_json) came with the "
                         "frame; refusing to execute ops."}
    return None


def _int_keyed(d) -> dict:
    """Restore integer measurement-ref keys lost crossing the JSON boundary
    (JSON object keys are always strings; the wheel keys ``measurements`` by the
    integer ``{N}`` ref). A non-integer key passes through untouched."""
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


def _live_verdict(procedure_json, measurements, controls, operator_verdicts,
                  node_path: str) -> str:
    """``PASS|FAIL|SKIP|''`` for the step at ``node_path``, materialized from the
    measurements known SO FAR via the SAME ``build_manual_run`` the GUI calls at
    batch end — so a streamed live verdict can never diverge from the
    authoritative one. Best-effort: any failure yields ``''`` (a live verdict
    must never break the batch)."""
    try:
        from rules_packager_base.rules.v2_0_2.parser import build_manual_run
        run = build_manual_run(
            procedure_json, measurements, controls, operator_verdicts)
        for s in list(getattr(run, "steps", []) or []):
            if getattr(s, "node_path", "") == node_path:
                return getattr(s, "verdict", "") or ""
    except Exception:  # noqa: BLE001 — verdict is a visual nicety, never fatal
        return ""
    return ""


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
    acc_meas = dict(req.get("measurements") or {})      # node_path keys, as the controls'
    controls = req.get("controls") or {}
    operator_verdicts = _int_keyed(req.get("operator_verdicts") or {})
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
                            "error": f"{type(exc).__name__}: {exc}"})
        # A reading that just landed changes this step's verdict (and only this
        # step's — an auto segment never spans an @IF barrier, so no later
        # node_path shifts). Fold it in and materialize the live PASS/FAIL.
        last = results[-1]
        verdict = ""
        if last.get("ok") and last.get("has_value") and last.get("ref") is not None:
            acc_meas[node_path] = last.get("value")
            verdict = _live_verdict(procedure_json, acc_meas, controls,
                                    operator_verdicts, node_path)
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
        # send, and finish / dialog-close / session-lost drive _recovery_safe_off
        # over that set (which re-opens the device fresh). NOTE the latent gap a
        # future change must respect: after this drop the device leaves
        # session.held, so the daemon's HELD-only shutdown safe-off will NOT
        # power it down — only the GUI's _auto_energized path will.
        if policy == "per_session" and device in session.held:
            _close_held_device(session, device)
        session.res.log.append(
            f"raw {device} FAILED: {type(exc).__name__}: {exc}")
        return {"ok": False, "kind": "ExecError",
                "error": f"{type(exc).__name__}: {exc}",
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

    Recorded unconfirmed-off targets are retried FIRST (module docstring) —
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
            session.res.log.append(f"UNSAFE: safe-off open {device} FAILED: {exc}")
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
def _sib(name: str):
    """Lazy sibling import, never through the procedo.studio package (PySide6
    is absent from project venvs); sys.modules is the cache."""
    try:
        return sys.modules[name]
    except KeyError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        return __import__(name)


def _bench():
    return _sib("_bench_session_runner")


def _hold():
    return _sib("_hold_resolution")


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
        _load_bundle(session, req)
        err = _take_document(session, req)
        if err and cmd == "exec_ops":
            # Only exec is gated on the document. safe_off/shutdown/ping proceed with the last
            # one received or {} — a recovery safe-off must never be blocked (explicit channels).
            return err, False
        if cmd == "ping":
            return {"ok": True, "pong": True}, False
        if cmd == "exec_ops":       # in the frame's work_dir: relative writes land there
            with _bench().in_work_dir(req.get("work_dir")):
                return _cmd_exec_ops(session, req), False
        if cmd == "raw":
            return _cmd_raw(session, req), False
        if cmd == "safe_off":
            return _cmd_safe_off(session, req), False
        if cmd == "shutdown":
            return _cmd_shutdown(session, req), True
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
