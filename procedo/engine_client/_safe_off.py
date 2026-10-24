"""Safe-off core of the engine daemon (``_execute_op_subprocess.py``).

Owns which devices are held and which channels are energized (``_Session``), how they are
driven off (cleanup-priority order, ELOAD before PSU, by declared channels at teardown), and the
unconfirmed-off memory behind every ``unsafe`` report.  Op execution is the sibling
``_instrument_ops.py``'s.

Loaded by BARE name next to the daemon (its folder on sys.path) in the project venv, which has
no ``procedo`` package; siblings resolve by package when imported as ``procedo.engine_client``.
This module's ``globals()`` is the ``d`` that ``_ensure_held`` hands ``_hold_resolution`` (and
through it ``_bench_session_runner.check_held``): every name that seam reads — ``_bench``,
``_safe_name``, ``_raw_send``, ``_record_unconfirmed``, ``_close_held_device``,
``_ensure_held`` — is bound here.

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

import importlib


def _sibling(name: str):
    """A sibling module: by bare name in the daemon, by package when imported as one."""
    # The package branch serves imports as procedo.engine_client._safe_off (e.g. test_imports_without_qt).
    return importlib.import_module(f"{__package__}.{name}" if __package__ else name)


_ops = _sibling("_instrument_ops")
_NotRemote = _ops._NotRemote
_ResStub = _ops._ResStub
_build_device = _ops._build_device
_exec_op = _ops._exec_op
_ns_base = _ops._ns_base
_open_no_reset = _ops._open_no_reset
_raw_send = _ops._raw_send          # read only through the ``d`` seam (check_held's probe)
_safe_name = _ops._safe_name


def _bench():
    return _sibling("_bench_session_runner")


def _hold():
    return _sibling("_hold_resolution")


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
    name re-opens fresh here.  Unprovable staleness (no address, no record)
    keeps today's name-keyed reuse; every reuse REBINDS the
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
