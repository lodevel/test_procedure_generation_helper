"""Instrument op execution for the engine daemon (``_execute_op_subprocess.py``).

Builds a pack's driver and opens it WITHOUT ``reset()``, runs one op's remote driver call(s)
through the owning pack's ``emit_python`` (a capture-context, so the op -> driver-call mapping
stays the pack's), sends raw terminal lines, and resolves a device's connection policy and pack
namespace.  No session state lives here: the held devices and every safe-off are the sibling
``_safe_off.py``'s.

Loaded by BARE name next to the daemon (its folder on sys.path) in the project venv, which has
no ``procedo`` package; this module imports nothing from procedo.
"""
from __future__ import annotations

import numbers


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
    """The owning pack's driver call(s) for ``target_op`` (the engine's
    ``codegen.remote_lines``).

    Returns ``(var_name, lines, meta)`` where ``lines`` is the remote branch's
    driver call(s) and ``meta`` carries ``ref``/``unit``/``has_result`` for a
    measuring op.  No test.py scaffolding (if/else, prompts) is emitted — only
    the remote driver call(s).
    """
    from rules_packager_base.rules.v2_0_2.parser.codegen import remote_lines

    captured = remote_lines(procedure_json, target_op)
    if captured is None:
        op_const = target_op.get("op") or ""
        raise _NoCodegen(
            f"pack for {op_const.split('.', 1)[0]!r} did not emit a driver call for op {op_const!r}."
        )
    return captured


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


def _safe_name(device: str) -> str:
    from rules_packager_base.rules.v2_0_2.parser.codegen_helpers import _safe_name as _sn
    return _sn(device)


def _instrument_error(device, bench, exc) -> str:
    """``PSU1 at ASRL3::INSTR: <ExcType>: <message>`` — the engine's one formatter."""
    from rules_packager_base.bench_endpoints import instrument_error
    return instrument_error(str(device), bench, exc)
