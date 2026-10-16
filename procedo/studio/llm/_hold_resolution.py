"""Hold resolution for the per-op daemon (owner ruling, 2026-08-27).

``PSU1`` for a given op is whatever its CURRENT address resolves to — the
correct physical device, independently of the name.  Every ``_ensure_held``
resolution therefore folds the op's CURRENT bench dict to its canonical
endpoint FIRST (the same wheel fold ``BenchState.enter`` / ``acquire_run``
use, with the same old-wheel/no-address fallbacks) and binds the name to the
instance that endpoint resolves to:

* endpoint held (active run-view claim, or the fold lands on a held
  endpoint) -> reuse that instance (F-H6 probe + one rebuild), REBIND the
  namespace slot, return the endpoint hold key — an active run's view stays
  authoritative for its names (a run's mapping IS its current claim);
* name-keyed hold whose RECORDED endpoint provably differs from the current
  fold -> the stale binding is never returned: its energized channels are
  first recorded unconfirmed-off under the OLD endpoint with the stored
  open bench (R2 — a handle drop must never hide a possibly-live output;
  the next safe-off/shutdown pass reconnects and offs the old physical
  device), then the instance closes through the ``_close_held_device``
  chokepoint and the caller re-opens fresh at the current address.  The
  instance has no other owner: run views hold canonical endpoints (never
  names), the alias map holds routing data (not instances), and the single
  namespace slot is rebound by the fresh open;
* staleness unprovable (old wheel without ``bench_endpoints``, address-less
  bench dict, no record) -> today's name-keyed reuse — but the namespace
  slot is STILL rebound to the held instance, so the instance returned is
  always the instance executed (pre-fix, a name previously routed onto a
  held endpoint kept executing on that endpoint's instance while the hold
  key — and Seam B's retarget — named the name-keyed one);
* nothing held -> ``None``: the caller opens fresh per policy (unchanged).

Safety contexts keep their semantics: ``_cmd_safe_off`` and the
``_retry_unconfirmed`` reconnect pass the safety record's STORED bench
data, so the fold reaches the device the record points at — a positively
re-addressed name no longer sends a recorded off to the wrong instrument.

Sibling of ``_execute_op_subprocess.py`` (lazy file-path import, Qt-free);
``d`` is that module's ``globals()`` — the established seam.
"""
from __future__ import annotations


def fold_endpoint(session, bench, etype):
    """Canonical endpoint of a bench dict — the SAME fold (and fallbacks) as
    ``BenchState.enter`` / ``acquire_run``: ``None`` for a missing dict, an
    old wheel without ``bench_endpoints``, or an address-less dict."""
    if not bench:
        return None
    try:
        from rules_packager_base import bench_endpoints as be
    except ImportError:                     # old wheel: name-keyed, as today
        return None
    try:
        return be.canonical_endpoint(bench, (session.lifecycle or {}).get(etype))
    except ValueError:                      # no address: name-keyed, as today
        return None


def _meta(session) -> dict:
    """``name -> {"endpoint", "bench"}`` recorded at each ``_ensure_held``
    open (lazy attribute: a session predating the record simply has none,
    which reads as staleness-unprovable — today's behavior)."""
    meta = getattr(session, "hold_meta", None)
    if meta is None:
        meta = session.hold_meta = {}
    return meta


def record_open(session, device, bench, etype) -> None:
    """Record where the hold just opened physically lives.  An unfoldable
    bench dict POPS the record — a later fold must not compare against an
    endpoint this hold cannot be proven to live at."""
    key = fold_endpoint(session, bench, etype)
    if key is None:
        _meta(session).pop(device, None)
    else:
        _meta(session)[device] = {"endpoint": key, "bench": dict(bench)}


def drop_stale(d: dict, session, device, current) -> bool:
    """Close a name-keyed hold whose name has PROVABLY moved (a record
    exists and its endpoint differs from ``current``).  Energized channels
    are recorded unconfirmed-off under the OLD endpoint first (module
    docstring), then the instance closes through the ONE chokepoint —
    which also invalidates its name-keyed ``applied_timeout`` skip record,
    so the fresh instance is retargeted instead of skipped.  Returns True
    when dropped (the caller resolves fresh at the current address)."""
    rec = _meta(session).get(device)
    if (device not in session.held or rec is None or current is None
            or rec["endpoint"] == current):
        return False
    chans = set(session.energized.get(device) or ())
    if chans:
        d["_record_unconfirmed"](session, rec["endpoint"],
                                 session.etype_of.get(device, ""),
                                 chans, rec["bench"])
    d["_close_held_device"](session, device)
    _meta(session).pop(device, None)
    session.res.log.append(
        f"{device}: address changed ({rec['endpoint']} -> {current}); "
        "stale instance closed, reopening at the current address")
    return True


def resolve(d: dict, session, bench_map: dict, device, etype,
            probe_refuse: bool, identity_gate: bool):
    """The ``_ensure_held`` resolution rule (module docstring).  Returns the
    hold key of a reused instance, or ``None`` when the caller must open
    fresh (nothing held, or a stale name binding was just dropped)."""
    current = fold_endpoint(session, bench_map.get(device), etype)
    if session.bench is not None:
        key = session.bench.enter(session, device, bench_map.get(device), etype)
        if key != device:          # routed onto a held endpoint (view or fold)
            # Boundary health check (F-H6): same probe + one-rebuild +
            # identity check as acquire_run, before reusing the instance.
            b = d["_bench"]()
            if (b.check_held(d, session, key, identity_gate) == "rebuilt"
                    and probe_refuse):
                refusal = b.state_lost_refusal(session, [key])
                if refusal is not None:
                    raise b.HealthRefusal(refusal)
            session.ns[d["_safe_name"](device)] = session.held[key]
            session.etype_of.setdefault(device, etype)
            return key
    if device in session.held:
        if drop_stale(d, session, device, current):
            return None
        session.ns[d["_safe_name"](device)] = session.held[device]
        return device
    return None
