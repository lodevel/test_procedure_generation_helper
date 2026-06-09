"""Subprocess CLI runner for executing ONE procedure op against live equipment.

Invoked by FILE PATH (NOT ``-m``) in the *project* venv::

    <project>/.venv/<bin>/python <abs>/_execute_op_subprocess.py

Running via ``-m`` would trigger ``workflow_editor/__init__.py`` (PySide6, absent
from the project venv); invoking by path keeps ``__name__ == "__main__"`` and
skips the package init.  Sibling to ``_pack_parsers_subprocess.py``, but where
that runner only imports ``rules_packager_base`` (the parser), this one ALSO
imports the hardware drivers (labscpi / fncore) — it is the only runner that
talks to a real instrument.

Purpose (guided-manual "⚡ Execute remotely"): connect to one device WITHOUT
resetting it, run the single op's driver call, capture any measured value, close.

The "no reset" guarantee
------------------------
A generated test.py startup does ``connect()`` + ``initialize()`` and THEN a
separate ``_setup_devices`` loop ``if hasattr(dev,'reset'): dev.reset()``.
``initialize()`` only binds the SCPI adapter (required — every op method asserts
it exists) and issues no ``*RST``; the reset lives solely in that ``dev.reset()``
call.  This runner calls ``connect()``/``open()`` + ``initialize()`` and
**never** ``reset()`` — so device output/state is left exactly as the bench
holds it.  (psu/eload have no ``reset()`` at all; the scope's is the only
``*RST`` path, and we omit it.)

The op -> driver-call mapping is NOT duplicated here.  Each pack already owns it
in ``emit_python(op, ctx)``; we drive that emit through a capture-context that
records the *remote* branch's driver call(s) instead of writing test.py source,
then exec those line(s) with the live device bound.  One seam, every pack.

Wire format (stdin -> stdout):

  Input:
    {"op": "execute_op_remote",
     "_bundle_dir": "<abs>",          # populate the pack registry
     "procedure_json": {...},          # for _Generator (device var names)
     "target_op": {...raw op dict...}, # the single op to run (carries device/params)
     "bench": {"visa": ..., "timeout_ms": ..., "channel": ...}   # OR
              {"port": ..., "baud": ..., "timeout_s": ..., "manual_override": ...}}

  Output (success):
    {"ok": true, "has_value": bool, "value": <json>, "unit": str,
     "ref": <int|str|null>, "log": [str]}
  Output (error):
    {"ok": false, "error": str, "kind": "<NotRemote|NoCodegen|ExecError|Other>",
     "traceback": str}
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


# ---------------------------------------------------------------------------
# Runtime stubs bound into the exec namespace.  The captured remote lines are
# pack-generated and may funnel a measured value through ``record_measurement``
# (scope.measure_stats, controller.read_*); they also reference ``results`` /
# ``res``.  We never run a manual branch, so no prompt/read helpers are needed.
# ---------------------------------------------------------------------------


class _ResStub:
    """Minimal stand-in for the generated test.py ``Result`` object."""

    def __init__(self) -> None:
        self.log: list = []
        self.operator_verdicts: dict = {}

    def add_evidence(self, *args, **kwargs) -> None:  # scope.screenshot
        pass


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
    # Reuse the pack's declared import so the class name resolves exactly as the
    # generated test.py would resolve it (labscpi facade / fncore client).
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


class _NotRemote(Exception):
    """Device is not remote-capable (no address / in manual mode)."""


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

    # Intercept the emit sink + the five remote/manual helpers on this instance.
    # Each override records ONLY the remote branch (the manual else-branch is
    # never produced, so its prompt/read globals are never referenced).
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


class _NoCodegen(Exception):
    """The owning pack produced no remote driver call for the op."""


# ---------------------------------------------------------------------------
# Batch execution: open each distinct device ONCE (no reset), run the ops in
# order in a shared namespace, capture per-op results keyed by node_path. Only
# on a FAILURE mid-batch do we drive energized PSU/ELOAD outputs OFF in
# cleanup-priority order (ELOAD before PSU) before closing — an error is
# abnormal, so we return the bench to a safe state. A CLEAN batch leaves outputs
# as the procedure set them (the next manual step may need them); the controller
# requests an explicit safe-off on Stop / window-close.
# ---------------------------------------------------------------------------

_READ_VERBS = {"measure_voltage", "measure_current", "measure_power",
               "get_voltage", "get_current", "get_power"}
# NB: query_raw / write_raw are deliberately NOT reads here — a raw SCPI command
# MAY assert an output, so they count as state-changing for energized-tracking.
# Over-tracking is harmless: it only adds a redundant safe-off on a failed batch.


def _is_state_changing(op: dict) -> bool:
    """A PSU/ELOAD op that can energize an output (anything but a pure read)."""
    etype, _, verb = (op.get("op") or "").partition(".")
    return etype in ("psu", "eload") and verb not in _READ_VERBS


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
        return True, ns["__RESULT__"], meta["unit"], meta["ref"]
    if "value" in holder:
        ref = meta["ref"] if meta["ref"] is not None else holder.get("ref")
        return True, holder["value"], holder.get("unit", ""), ref
    return False, None, "", meta["ref"]


def _open_device(bench_map, device, etype, lifecycle, ns, opened, etype_of, res):
    """Lazily construct + open (no reset) a device, bind it in the namespace."""
    if device in opened:
        return
    if device not in bench_map:
        raise _NotRemote(f"no connection parameters for {device!r}.")
    from rules_packager_base.rules.v2_0_2.parser.codegen_helpers import _safe_name
    dev = _build_device(bench_map[device], etype, lifecycle, res.log)
    _open_no_reset(dev)
    opened[device] = dev
    etype_of[device] = etype
    ns[_safe_name(device)] = dev


def _safe_off(procedure_json, ns, energized, etype_of, lifecycle, res) -> None:
    """Drive energized PSU/ELOAD outputs OFF in cleanup-priority order (ELOAD
    priority 10 before PSU priority 20). Reuses the pack emit by synthesizing an
    ``<etype>.output … on=False`` op per touched channel. Best-effort — a
    failure to turn one off is logged, never raised (already cleaning up)."""
    items = []
    for device, channels in energized.items():
        etype = etype_of.get(device, "")
        cleanup = (lifecycle.get(etype) or {}).get("cleanup")
        if not cleanup:                # scope/controller: nothing to power down
            continue
        items.append((cleanup.get("priority", 100), etype, device, channels))
    for _prio, etype, device, channels in sorted(items, key=lambda t: t[0]):
        known = sorted(c for c in channels if c is not None)
        if None in channels:   # a channel-less state-changer (raw SCPI / tracking)
            known = sorted(set(known) | set(_device_channels(procedure_json, device)))
        for ch in (known or [1]):
            off_op = {"op": f"{etype}.output", "device": device,
                      "channel": ch, "on": False}
            try:
                _exec_op(procedure_json, off_op, ns)
                res.log.append(f"safe-off {device} CH{ch}")
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"safe-off {device} CH{ch} failed: {exc}")


def _run_batch(spec: dict) -> dict:
    procedure_json = spec["procedure_json"]
    ops = spec.get("ops") or []
    bench_map = spec.get("bench_map") or {}
    from rules_packager_base.rules.v2_0_2.parser._default_registry import get_lifecycle
    lifecycle = get_lifecycle()

    res = _ResStub()
    ns = _ns_base(res)
    opened: dict = {}      # device -> driver instance (one connect, never reset)
    etype_of: dict = {}    # device -> equipment type
    energized: dict = {}   # device -> {channels} touched by state-changing ops
    results: list = []
    failed = False
    try:
        for entry in ops:
            op = entry.get("op") or {}
            node_path = entry.get("node_path", "")
            device = op.get("device")
            etype = (op.get("op") or "").split(".", 1)[0]
            try:
                _open_device(bench_map, device, etype, lifecycle, ns,
                             opened, etype_of, res)
                has, value, unit, ref = _exec_op(procedure_json, op, ns)
                if _is_state_changing(op):
                    energized.setdefault(device, set()).add(op.get("channel"))
                results.append({"node_path": node_path, "ok": True,
                                "has_value": bool(has), "value": value,
                                "unit": unit, "ref": ref})
            except Exception as exc:  # noqa: BLE001 — record + stop the batch
                failed = True
                results.append({"node_path": node_path, "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"})
                break
    finally:
        if failed and energized:    # abnormal exit → return the bench to safety
            _safe_off(procedure_json, ns, energized, etype_of, lifecycle, res)
        for dev in opened.values():
            try:
                if hasattr(dev, "close"):
                    dev.close()
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"close() failed: {exc}")
    return {"ok": True, "results": results, "aborted": failed,
            "log": list(res.log)}


def _run_safe_off(spec: dict) -> dict:
    """Controller-initiated safe-off (Stop / window-close while energized): open
    each device (no reset), drive its outputs OFF in priority order, close."""
    procedure_json = spec["procedure_json"]
    bench_map = spec.get("bench_map") or {}
    targets = spec.get("safe_off") or []   # [{"device","etype","channels":[...]}]
    from rules_packager_base.rules.v2_0_2.parser._default_registry import get_lifecycle
    lifecycle = get_lifecycle()
    res = _ResStub()
    ns = _ns_base(res)
    opened: dict = {}
    etype_of: dict = {}
    energized: dict = {}
    try:
        for t in targets:
            device, etype = t.get("device"), t.get("etype")
            try:
                _open_device(bench_map, device, etype, lifecycle, ns,
                             opened, etype_of, res)
                energized[device] = set(t.get("channels") or [1])
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"safe-off open {device} failed: {exc}")
        _safe_off(procedure_json, ns, energized, etype_of, lifecycle, res)
    finally:
        for dev in opened.values():
            try:
                if hasattr(dev, "close"):
                    dev.close()
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"close() failed: {exc}")
    return {"ok": True, "log": list(res.log)}


def main() -> None:
    try:
        spec = json.loads(sys.stdin.read())
        bundle_dir = spec.get("_bundle_dir")
        if bundle_dir:
            from rules_packager_base.rules.v2_0_2.parser._pack_registry import (
                load_packs_into_registry,
            )
            load_packs_into_registry(Path(bundle_dir))
        if spec.get("safe_off") is not None:
            result = _run_safe_off(spec)
        else:
            if "ops" not in spec and "target_op" in spec:  # legacy single-op spec
                spec["ops"] = [{"node_path": "", "op": spec["target_op"]}]
                spec.setdefault("bench_map", {
                    spec["target_op"].get("device"): spec.get("bench") or {}})
            result = _run_batch(spec)
    except _NotRemote as exc:
        result = {"ok": False, "kind": "NotRemote", "error": str(exc)}
    except _NoCodegen as exc:
        result = {"ok": False, "kind": "NoCodegen", "error": str(exc)}
    except ImportError as exc:
        result = {"ok": False, "kind": "Other",
                  "error": f"import failed in project venv: {exc}",
                  "traceback": traceback.format_exc()}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "kind": "ExecError",
                  "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    sys.stdout.write(json.dumps(result, default=str))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
