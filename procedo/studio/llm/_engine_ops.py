"""The engine's op table, and which frames each engine role takes.

Loaded by FILE PATH by the persistent daemon ``_execute_op_subprocess.py``
(through ``_sib``) in the project venv — never through the ``procedo.studio``
package, whose init imports PySide6 (absent from the project venv). Each op is
computation on the spec it is given plus the in-memory pack registry: a
document travels IN the spec, never as a path.

Spec: ``{"op": "<op_name>", ...op-specific fields...}``

Result (success): ``{"ok": true, ...op-specific result fields...}``
Result (error):   ``{"ok": false, "error": "<message>",
                     "kind": "<ParserUnavailable|ParseFailure|Other>"}``

Ops:
  is_available  → {"ok": true, "available": bool, "reason": str}
  parse_text    → {"ok": true, "json": dict, "warnings": [str]}
                  or {"ok": false, "kind": "ParseFailure", "error": str,
                      "code": str, "line": int|null, "column": int|null,
                      "fix_hint": str, "findings": [{...}]}
  render_text   → {"ok": true, "text": str}
  refresh_media → {"ok": true, "json": dict}
  export_rows   → {"ok": true, "steps": [rows], "expected": [rows]}
                  (batch: input {"procedures": [...]} → {"ok": true, "results": [...]})
  renumber_steps → {"ok": true, "text": str}
  sync_equipment → {"ok": true, "text": str, "warnings": [str]}
  sync_meta     → {"ok": true, "text": str, "warnings": [str]}
  generate_code → {"ok": true, "code": str, "warnings": []}
  validate      → {"ok": true, "findings": [{code, message, severity, line,
                                              col, fix_hint, fixable_by}]}
  section_ownership → {"ok": true, "ownership": {section: owner}}
  reconstruct   → {"ok": true, "success": bool, "text": str|null,
                   "json": dict|null, "findings": [{...}]}
  build_manual_run → {"ok": true, "test_name": str, "aborted": bool,
                      "steps": [{...StepDescriptor...}],
                      "skipped": [{...StepDescriptor...}]}
                     (input: {"procedure_json": {...}, "measurements",
                      "controls", "operator_verdicts"})
                     (each Wait step adds ``wait_s`` or ``wait_refused``;
                      each step what the Values panel shows: ``value_text``,
                      ``value_note``, ``names`` [{name, var, value, origin}];
                      + "channels": {device: [declared power channels]},
                      "constants": [{name, value, origin}])
  build_manual_result → {"ok": true, "result": {...results.json schema...}}
                        (input: the same fields)
  manual_reading → {"ok": true, "value": any, "refusal": str}
                   (input: {"step": {kind, expected_text, resolved_op}, "text"})
  criterion_refs → {"ok": true, "refs": {key: [str]}}  (input: {"criteria": {key: criterion}})
  bench_identity → {"ok": true, "endpoints": [str|null], "channels": [[int]]}  (input: {"benches": [bench]})
  pack_contract → {"ok": true, "packs": {module: {"items": [ContractItem], "error": str|null}}}
                  (input: {"modules": [pack parser module]})
  display_snapshot → {"ok": true, "wheel": {module, file, version},
                      "tests": [{folder, mtime_ns, size, text_newer,
                                 data: {meta, steps, expected, equipment},
                                 errors: {key: why}}]}
                     (input: {"project", "tests": null | [folder, ...]}; the
                      bundle is the frame's ``_bundle_dir``)
  extract       → {"ok": true, "results": [{"data": {key: value}, "errors": {key: why}}]}
                  (input: {"project", "procedures": [{...}, ...], "keys": [key, ...]}:
                   a display key or ``equipment_validate`` / ``steps_validate`` /
                   ``expected_validate``, run by the extractor the bundle declares;
                   one result per procedure, in order)
"""
from __future__ import annotations

import functools
import importlib
import json
import re
import traceback
from dataclasses import asdict
from pathlib import Path


def _finding_to_dict(f) -> dict:
    return {
        "code": getattr(f, "code", "") or "",
        "message": getattr(f, "message", "") or "",
        "severity": getattr(f, "severity", "error") or "error",
        "line": getattr(f, "line", 0) or 0,
        "col": getattr(f, "col", 0) or 0,
        "fix_hint": getattr(f, "fix_hint", "") or "",
        "fixable_by": getattr(f, "fixable_by", "either") or "either",
    }


def _format_finding(f) -> str:
    code = getattr(f, "code", "?")
    line = getattr(f, "line", 0)
    msg = getattr(f, "message", str(f))
    return f"line {line} [{code}] {msg}"


def _import_wheel():
    import rules_packager_base.rules.v2_0_2.parser as _parser
    return _parser


def _import_codegen():
    from rules_packager_base.rules.v2_0_2.parser import codegen as _codegen
    return _codegen


def _op_is_available(spec: dict) -> dict:
    del spec  # no inputs
    try:
        _import_wheel()
        return {"ok": True, "available": True, "reason": "deterministic path active"}
    except ImportError as exc:
        return {
            "ok": True,
            "available": False,
            "reason": (
                f"rules_packager_base.rules.v2_0_2.parser is not importable: {exc}. "
                f"Reinstall the rules_packager_base wheel (>= 2.0.4)."
            ),
        }


def _op_parse_text(spec: dict) -> dict:
    raw_root = spec.get("project_root")
    result = _import_wheel().parse(
        spec["text"], project_root=Path(raw_root) if raw_root is not None else None)
    if not result.success or result.json is None:
        errors = result.errors
        primary = errors[0] if errors else None
        return {
            "ok": False,
            "kind": "ParseFailure",
            "error": str(primary) if primary else "Parsing failed.",
            "code": getattr(primary, "code", "PARSE_ERROR") if primary else "PARSE_ERROR",
            "line": getattr(primary, "line", None) if primary else None,
            "column": getattr(primary, "col", None) if primary else None,
            "fix_hint": getattr(primary, "fix_hint", "") if primary else "",
            "findings": [_finding_to_dict(f) for f in result.findings],
        }
    warnings = [
        _format_finding(f) for f in result.findings if f.severity == "warning"
    ]
    return {"ok": True, "json": result.json, "warnings": warnings}


def _op_refresh_media(spec: dict) -> dict:
    """Recompute a procedure JSON's derived media with the pack's one
    catalogue (what parse() does for its own output; a no-op there)."""
    from rules_packager_base.rules.v2_0_2 import media
    procedure_json = spec["procedure_json"]
    media.populate_media(procedure_json)
    return {"ok": True, "json": procedure_json}


def _op_render_text(spec: dict) -> dict:
    procedure_json = spec["procedure_json"]
    wheel = _import_wheel()
    text = wheel.render(procedure_json)
    return {"ok": True, "text": text}


def _op_export_rows(spec: dict) -> dict:
    """Full-fidelity document-export rows, computed IN THE PROJECT VENV
    where every bundle pack (labscpi/fncore/...) is importable — the GUI
    process typically lacks them, so pack ops would render blank there."""
    from rules_packager_base.rules.v2_0_2 import extractors as _extractors

    def _ser(rows):
        out = []
        for r in rows:
            media = []
            for m in r.media:
                media.append({
                    "component": m.component,
                    "pin": m.pin,
                    "is_tp": bool(getattr(m, "is_tp", False)),
                    "caption": m.caption,
                    "type": m.type,
                })
            out.append({"idx": r.idx, "text": r.text, "media": media})
        return out

    procedures = spec.get("procedures")
    single = procedures is None
    if single:
        procedures = [spec["procedure_json"]]
    results = [
        {
            "steps": _ser(_extractors.extract_export_steps(pj)),
            "expected": _ser(_extractors.extract_export_expected(pj)),
        }
        for pj in procedures
    ]
    if single:
        return {"ok": True, **results[0]}
    return {"ok": True, "results": results}


def _op_renumber_steps(spec: dict) -> dict:
    text = spec["text"]
    wheel = _import_wheel()
    return {"ok": True, "text": wheel.renumber_steps(text)}


def _op_sync_equipment(spec: dict) -> dict:
    text = spec["text"]
    profiles = spec.get("controller_profiles", [])
    wheel = _import_wheel()
    new_text, findings = wheel.sync_equipment_text(
        text, controller_profiles=profiles,
    )
    # Warnings come back as formatted strings (mirrors _op_parse_text)
    # so the GUI can display them without knowing the Finding shape.
    warnings = [_format_finding(f) for f in findings]
    return {"ok": True, "text": new_text, "warnings": warnings}


def _op_sync_meta(spec: dict) -> dict:
    text = spec["text"]
    wheel = _import_wheel()
    new_text, findings = wheel.sync_meta_text(text)
    warnings = [_format_finding(f) for f in findings]
    return {"ok": True, "text": new_text, "warnings": warnings}


def _op_generate_code(spec: dict) -> dict:
    procedure = spec["procedure"]
    codegen = _import_codegen()
    code = codegen.generate(procedure, None)
    return {"ok": True, "code": code, "warnings": []}


def _op_validate(spec: dict) -> dict:
    text = spec.get("text")
    json_obj = spec.get("json_obj")
    mode = spec.get("mode", "all")
    original_text = spec.get("original_text")
    check_names = spec.get("check_names", True)

    wheel = _import_wheel()
    findings = []

    parsed_json = json_obj
    if text is not None:
        result = wheel.parse(text)
        findings.extend(result.findings)
        if result.success and result.json is not None:
            parsed_json = parsed_json or result.json

    if parsed_json is not None and mode in ("all", "schema"):
        findings.extend(wheel.validate_schema(parsed_json))

    if text is not None and original_text and check_names:
        findings.extend(wheel.check_name_fidelity(original_text, text))

    return {"ok": True, "findings": [_finding_to_dict(f) for f in findings]}


def _op_section_ownership(spec: dict) -> dict:
    del spec  # no inputs
    return {"ok": True, "ownership": _import_wheel().section_ownership()}


def _op_reconstruct(spec: dict) -> dict:
    fragment = spec["fragment"]
    prior = spec.get("prior")
    ow = spec.get("owned_sections")
    owned = set(ow) if ow is not None else None
    r = _import_wheel().reconstruct(fragment, prior, owned)
    return {
        "ok": True,
        "success": r.success,
        "text": r.text,
        "json": r.json,
        "findings": [_finding_to_dict(f) for f in r.findings],
    }


def _int_keyed(d) -> dict:
    """Restore integer measurement-ref keys lost crossing the JSON boundary.

    The wheel keys readings by ref: an int for ``{3}``, the name for ``{Vout}`` /
    ``{Vrail[3]}``. JSON object keys are strings, so a dict sent over stdin arrives
    string-keyed: a number becomes an int again, a name stays as it is."""
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


def _document(spec: dict):
    """The procedure document the spec carries (``procedure_json``), never read
    from disk: the caller sends what it shows.

    Returns ``(procedure_json, None)`` or ``(None, error_result)`` — the error
    result is the uniform ``{"ok": false, ...}`` shape so the op can return it
    directly instead of dying with a bare traceback."""
    doc = spec.get("procedure_json")
    if not isinstance(doc, dict):
        return None, {"ok": False, "kind": "Other",
                      "error": "the procedure document is not a JSON object"}
    return doc, None


def run_inputs(spec: dict) -> dict:
    """A guided run's inputs as the wheel takes them, from an op spec or an
    exec_ops frame: ``measurements`` and ``controls`` keyed by row node_path,
    ``operator_verdicts`` by ``{N}`` ref — every engine call of the run reads
    them here."""
    return {"measurements": spec.get("measurements") or {}, "controls": spec.get("controls") or {},
            "operator_verdicts": _int_keyed(spec.get("operator_verdicts") or {})}


def _op_build_manual_run(spec: dict) -> dict:
    procedure_json, err = _document(spec)
    if err:
        return err
    inputs = run_inputs(spec)
    session = _shown_session()(procedure_json)
    run = session.materialize(**inputs)
    return {
        "ok": True,
        "test_name": run.test_name,
        "steps": _shown_steps(session, run, inputs["measurements"]),
        "skipped": [_step_payload(s) for s in run.skipped],
        "aborted": run.aborted,
        "channels": _declared_channels(procedure_json),
        "constants": _procedure_names(session.macros),
    }


def _declared_channels(procedure_json: dict) -> dict:
    """Each instrument's declared power channels — what a safe-off of it turns
    off — by the wheel's equipment extractor; a device declaring none is the
    single-channel default, [1]."""
    from rules_packager_base.rules.v2_0_2 import extractors

    return {eq.id: sorted({ch.channel for ch in eq.channels if isinstance(ch.channel, int)}) or [1]
            for eq in extractors.extract_equipment_requirements(procedure_json)}


def _step_payload(step) -> dict:
    """A StepDescriptor as the guided run takes it; a Wait's delay read here by
    the one setpoint reader: ``wait_s`` (seconds, none below 0, as test.py
    waits), or ``wait_refused`` — test.py's message for a value it refuses
    (`${t}` = MAX), never a silent 0 s."""
    d = asdict(step)
    op = d.get("resolved_op") or {}
    if d.get("op") == "wait" and op.get("duration") is not None:
        from rules_packager_base.test_helpers import setpoint
        try:
            d["wait_s"] = max(setpoint(op["duration"], op.get("duration_unit") or "s"), 0)
        except ValueError as exc:
            d["wait_refused"] = str(exc)
    return d


# -- what the guided run's Values panel shows, read from the wheel's own walk ----

_TURN_RE = re.compile(r"\.body\[\d+\]#(?:iter|run)=(\d+)")   # a loop's turn, after its node_path


@functools.cache
def _shown_session():
    """The wheel's StepperSession noting, as it walks, the ``${…}`` names of each
    row's loop turns and each Bind's value or why it has none, as its row says
    it — the walk's own, never worked out again here."""
    from rules_packager_base.rules.v2_0_2.parser._macro_eval import for_turns
    from rules_packager_base.rules.v2_0_2.parser.manual_run import StepperSession

    class ShownSession(StepperSession):
        def __init__(self, procedure_json: dict):
            super().__init__(procedure_json)
            self.loops: list = []   # the loops the walk is in: (node_path, origin, variables, " of N")
            self.names: dict = {}   # row node_path -> its turns' names
            self.binds: dict = {}   # Bind row node_path -> (value text, note)

        def _expand_for(self, block, block_path, bindings, ctx, skip):
            turns = for_turns(block, self.macros, bindings) or ()
            self.loops.append((block_path, "@FOR", block.get("vars") or [], f" of {len(turns)}"))
            super()._expand_for(block, block_path, bindings, ctx, skip)
            self.loops.pop()

        def _runtime_do(self, block, do_path, bindings, ctx, skip):
            var = block.get("var")
            self.loops.append((do_path, "@DO", [var] if isinstance(var, str) else [], ""))
            super()._runtime_do(block, do_path, bindings, ctx, skip)
            self.loops.pop()

        def _concrete(self, raw, node_path, bindings, status="pending"):
            self.names[node_path] = _turn_names(self.loops, node_path, bindings)
            return super()._concrete(raw, node_path, bindings, status)

        def _compute(self, row, raw, bindings, ctx):
            said = len(row.text)
            super()._compute(row, raw, bindings, ctx)   # its row says " → <value>" or " → <why none>"
            text = row.text[said:].removeprefix(" → ")
            self.binds[row.node_path] = (text, "") if row.ref in ctx.measurements else ("", text)

    return ShownSession


def _turn_names(loops: list, node_path: str, bindings: dict) -> list:
    """The ``${…}`` names of a row's loop turns, outermost first: each as written
    (``name``) and bare (``var``), its value as the row shows it and its turn
    (``@FOR turn 2 of 3``); a table row's columns each."""
    from rules_packager_base.test_helpers import shown_value

    names = []
    for path, kind, variables, of in loops:
        turn = _TURN_RE.match(node_path, len(path))
        origin = f"{kind} turn {int(turn.group(1)) + 1}{of}" if turn else kind
        for var in variables:
            value = bindings.get(var)
            cells = value.items() if isinstance(value, dict) else ((None, value),)
            bare = [var if key is None else f"{var}.{key}" for key, _cell in cells]
            names += [{"name": f"${{{b}}}", "var": b, "value": shown_value(cell), "origin": origin}
                      for b, (_key, cell) in zip(bare, cells, strict=True)]
    return names


def _shown_steps(session, run, readings: dict) -> list:
    """The plan's step payloads with what the Values panel shows of each:
    ``value_text`` (a reading in its criterion's unit, as the report shows a
    measured value; a Bind as its row says it), ``value_note`` (why a Bind has
    none) and ``names`` (its loop turns' ``${…}``)."""
    from rules_packager_base.test_helpers import entry_refs, value_text

    units: dict = {}
    for entry in session._build_rules(run.measurements, []).values():   # the RULES test.py judges now
        for ref in entry_refs(entry):
            if entry.get("units"):
                units.setdefault(ref, entry["units"])
    out = []
    for step in run.steps:
        text, note = session.binds.get(step.node_path, ("", ""))
        if readings.get(step.node_path) is not None:
            text = value_text(readings[step.node_path], units.get(step.ref, ""))
        out.append({**_step_payload(step), "value_text": text, "value_note": note,
                    "names": session.names.get(step.node_path, [])})
    return out


def _procedure_names(macros: dict) -> list:
    """The procedure's own names, read-only in a run, as the engine binds them:
    each @LET's value, each @ALLOC's first ``{N}``, each @TABLE's rows."""
    from rules_packager_base.rules.v2_0_2.parser._macro_eval import initial_static_bindings
    from rules_packager_base.test_helpers import shown_value

    bound = initial_static_bindings(macros)
    names = [{"name": name, "value": shown_value(bound[name]), "origin": origin}
             for key, origin in (("lets", "@LET"), ("allocations", "@ALLOC"))
             for name in macros.get(key) or {} if name in bound]
    return names + [{"name": name, "value": f"{len(rows)} rows", "origin": "@TABLE"}
                    for name, rows in (macros.get("tables") or {}).items() if isinstance(rows, list)]


def _op_build_manual_result(spec: dict) -> dict:
    procedure_json, err = _document(spec)
    if err:
        return err
    result = _import_wheel().build_manual_result(procedure_json, **run_inputs(spec))
    return {"ok": True, "result": result}


# -- a guided run's typed readings ----------------------------------------------

# A bus read typed by hand: `0x` + hex digits or decimal digits is the value;
# two or more hex pairs, single-spaced as the board prints them, are its bytes.
_PRINTED_BYTES_RE = re.compile(r"[0-9A-Fa-f]{2}(?: [0-9A-Fa-f]{2})+")


def _op_manual_reading(spec: dict) -> dict:
    """What a reading entered as text stores for its step (``value``) and why a
    typed one is refused (``refusal``, "" when it is not) — the one reading
    rule of the guided run, typed or from the bench. ``step`` carries the
    step's ``kind``, ``expected_text`` and ``resolved_op``."""
    step, text = spec.get("step") or {}, str(spec.get("text", ""))
    value = _reading_value(step, text)
    refused = _reading_expects_number(step) and not isinstance(value, (int, float))
    return {"ok": True, "value": value, "refusal": _reading_refusal(step, text) if refused else ""}


def _reading_value(step: dict, text: str):
    """A text reading (a UART answer, the board's hex) and a typed answer read as
    text keep their string, as test.py does — "4B" is not the number 4, "1" is
    the answer "1"; a bytes reading is :func:`_bytes_value`; every other
    reading is the number the one reader takes (parse_number, test.py's
    read_measurement and read_typed: ``8``, ``8e3``, ``0x10``, ``200 mV``),
    else the stripped text, which a step needing a number refuses."""
    kind = step.get("kind", "")
    if kind in ("text", "prompt"):
        return text
    if kind == "bytes":
        return _bytes_value(step.get("resolved_op") or {}, text.strip())
    from rules_packager_base import parse_number

    try:
        return parse_number(text.strip())
    except ValueError:
        return text.strip()


def _bytes_format(op: dict) -> tuple:
    """(count, little_endian, signed, lowest value, highest value) of a bus read."""
    count, signed = int(op.get("count") or 0), bool(op.get("signed"))
    bits = 8 * count
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    return count, bool(op.get("little_endian")), signed, low, high


def _typed_int(typed: str):
    """A value typed by hand, by the one reader (parse_number: `0x` + hex digits,
    or decimal digits): its int, else None (a fraction, a unit, a number past
    Python's digit limit)."""
    from rules_packager_base import parse_number

    try:
        value = parse_number(typed)
    except ValueError:
        return None
    return value if isinstance(value, int) else None


def _bytes_value(op: dict, typed: str):
    """A bus read typed by hand: a value within what the read holds, or the bytes
    pasted as the board prints them, read with the step's own format by
    bytes_to_int — the one conversion; anything else stays the string, refused."""
    from rules_packager_base import bytes_to_int

    count, little_endian, signed, low, high = _bytes_format(op)
    value = _typed_int(typed)
    if value is not None:
        return value if low <= value <= high else typed
    if _PRINTED_BYTES_RE.fullmatch(typed):
        try:
            return bytes_to_int(typed, count, little_endian=little_endian, signed=signed)
        except ValueError:
            pass  # fewer pairs than the read takes: refused
    return typed


def _reading_expects_number(step: dict) -> bool:
    """A bus read, an operator read, a measure and a typed answer read as a
    number (kind ``number``: the base decides) are always NUMBERS; a text
    reading and a typed answer read as text never are; another reading is one
    unless its expected value quotes a string literal (a digital answer, e.g.
    ``{1} = '1'``) — then a string is fine — or it has no expected criterion."""
    kind = step.get("kind", "")
    if kind in ("text", "prompt"):
        return False   # a UART answer, a typed answer read as text: whatever the criterion looks like
    if kind in ("bytes", "read", "measure", "number"):
        return True    # whatever an `or` / `and` hint quotes for another reading
    et = step.get("expected_text") or ""
    return bool(et) and "'" not in et and '"' not in et


def _reading_refusal(step: dict, typed: str) -> str:
    """Why a typed reading the step needs as a number was refused. A 0x value
    above a signed read's range but within its bytes is the board's bytes
    typed as one number: the refusal gives the signed value of one byte, or
    shows more as the board prints them."""
    typed = typed.strip()
    if step.get("kind") != "bytes":
        return f"“{typed}” isn't a number — enter a numeric reading (e.g. 5.2)."
    count, little_endian, _signed, low, high = _bytes_format(step.get("resolved_op") or {})
    value = _typed_int(typed) if typed.startswith("0x") else None
    if value is not None and high < value < 1 << 8 * count:
        if count == 1:
            return (f"“{typed}” is out of a signed 1-byte read's range — the signed value of {typed} is "
                    f"{value - 256}: type {value - 256}.")
        printed = value.to_bytes(count, "little" if little_endian else "big").hex(" ").upper()
        return (f"“{typed}” is out of a signed {count}-byte read's range — paste the bytes as the board "
                f"prints them, e.g. {printed}.")
    if count == 1:
        return (f"“{typed}” is no 1-byte reading — type the value ({low} to {high}): 0x… as the board prints it, "
                "or decimal.")
    return (f"“{typed}” is no {count}-byte reading — type the value ({low} to {high}, 0x… for hex), "
            "or paste the bytes as the board prints them.")


def _op_criterion_refs(spec: dict) -> dict:
    """The readings each results.json criterion judges (``spec["criteria"]``: key ->
    criterion), as the wheel's entry_refs reads them (test.py's reads), each ref
    as a string — what a report's units map and Measured column list."""
    from rules_packager_base.test_helpers import entry_refs

    criteria = spec.get("criteria") or {}
    return {"ok": True, "refs": {key: [str(r) for r in entry_refs(crit)] for key, crit in criteria.items()}}


def _op_bench_identity(spec: dict) -> dict:
    """Each bench dict's (``spec["benches"]``) canonical endpoint — the physical
    instrument it reaches, one key for every spelling of one port (``COM22`` of a
    serial controller, ``ASRL22::INSTR`` of a VISA instrument), None when it
    names no address — and its declared power channels: the wheel's one
    canonicalization (bench_endpoints), never a guess of the app's."""
    from rules_packager_base.bench_endpoints import canonical_endpoint, channels_of

    def endpoint(bench):
        try:
            return canonical_endpoint(bench)
        except ValueError:
            return None

    benches = spec.get("benches") or []
    return {"ok": True, "endpoints": [endpoint(b) for b in benches], "channels": [channels_of(b) for b in benches]}


def _op_pack_contract(spec: dict) -> dict:
    """Each pack parser module (``spec["modules"]``) checked against the
    pack-author contract, here in the project venv — the packs the bundle
    installed: its ContractItem rows, or why it could not be checked."""
    from rules_packager_base.pack_contract import check_pack_contract

    packs = {}
    for name in spec.get("modules") or []:
        try:
            rows = check_pack_contract(importlib.import_module(name), _rules_index(name))
            packs[name] = {"items": [asdict(row) for row in rows], "error": None}
        except Exception as exc:  # noqa: BLE001 — one pack's failure is its section's
            packs[name] = {"items": [], "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "packs": packs}


def _rules_index(module_name: str) -> dict | None:
    """The pack's parsed ``rules_index.json``, or None when it ships none: the
    installed package's ``rules/<version>/`` index whose
    ``pack_capabilities.parser_fragment_module`` names *module_name* —
    self-validating, so a stale unversioned ``rules/rules_index.json`` (no
    ``pack_capabilities``) never matches. None is a legal checker input."""
    import importlib.resources

    try:
        entries = list((importlib.resources.files(module_name.split(".")[0]) / "rules").iterdir())
    except Exception:  # noqa: BLE001 — no rules dir: no index
        return None
    for entry in entries:
        try:
            index = entry / "rules_index.json"
            data = json.loads(index.read_text(encoding="utf-8")) if entry.is_dir() and index.is_file() else {}
        except Exception:  # noqa: BLE001 — an unreadable index is no index
            continue
        caps = data.get("pack_capabilities")
        if isinstance(caps, dict) and caps.get("parser_fragment_module") == module_name:
            return data
    return None


# The four DISPLAY domains snapshotted for the app. The validate_* twins are
# asked per document (``extract``): an on-demand banner, not the paint path.
_DISPLAY_KEYS = ("meta", "steps", "expected", "equipment")


def _load_extractor_decls(bundle_dir: Path) -> dict:
    """Return the bundle's ``defaults.json:extractors`` block ({} when
    missing/malformed) — the same declaration pack_extractors uses."""
    defaults = bundle_dir / "defaults.json"
    try:
        doc = json.loads(defaults.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    extractors = doc.get("extractors")
    return extractors if isinstance(extractors, dict) else {}


def _resolve_extractors(bundle_dir: Path, keys=_DISPLAY_KEYS) -> tuple[dict, dict, dict]:
    """Resolve the extractors of ``keys`` exactly the way the bundle declares
    them (``{module, function}`` per key), importing INSIDE this (project)
    venv. Returns ``(callables, errors, wheel_info)``; a missing or
    unimportable key lands in ``errors``, never raises."""
    decls = _load_extractor_decls(bundle_dir)
    fns: dict = {}
    errors: dict = {}
    wheel_info: dict = {"module": "", "file": "", "version": ""}
    for key in keys:
        entry = decls.get(key)
        if not isinstance(entry, dict):
            errors[key] = f"bundle declares no {key!r} extractor"
            continue
        module_name = entry.get("module")
        function_name = entry.get("function")
        if not isinstance(module_name, str) or not isinstance(function_name, str):
            errors[key] = f"extractor {key!r} lacks module/function strings"
            continue
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — import errors of any shape
            errors[key] = f"module {module_name} not importable: {exc}"
            continue
        fn = mod
        for part in function_name.split("."):
            fn = getattr(fn, part, None)
            if fn is None:
                break
        if not callable(fn):
            errors[key] = f"{module_name}.{function_name} is not callable"
            continue
        fns[key] = fn
        if not wheel_info["module"]:
            wheel_info["module"] = module_name
            wheel_info["file"] = getattr(mod, "__file__", "") or ""
            wheel_info["version"] = _wheel_version(module_name)
    return fns, errors, wheel_info


def _wheel_version(module_name: str) -> str:
    """Best-effort distribution version of the wheel providing
    ``module_name`` (its top-level package)."""
    top = module_name.split(".")[0]
    try:
        from importlib import metadata

        return metadata.version(top)
    except Exception:  # noqa: BLE001
        return ""


def _meta_payload(meta) -> dict:
    """ProcedureMeta -> plain dict carrying EVERY wheel field PLUS the
    wheel-computed ``display_name`` (a property, so asdict misses it)."""
    try:
        d = dict(asdict(meta))
    except TypeError:
        d = {k: v for k, v in vars(meta).items() if not k.startswith("_")}
    display_name = getattr(meta, "display_name", "")
    d["display_name"] = display_name if isinstance(display_name, str) else ""
    return d


def _items_payload(items) -> list:
    """list[dataclass] -> list[dict] (asdict recurses media/channels)."""
    return [asdict(item) for item in items]


def _text_newer(procedure_json: Path) -> bool:
    """Same mtime compare as pack_extractors.canonical_text_is_newer,
    computed in the sweep so the LIST can show the stale badge."""
    text_path = procedure_json.parent / "procedure_text.md"
    try:
        text_mtime = text_path.stat().st_mtime
        json_mtime = procedure_json.stat().st_mtime
    except OSError:
        return False
    return text_mtime > json_mtime


def _extract_test(folder: str, proc_path: Path, fns: dict, resolve_errors: dict) -> dict:
    """Extract one test. Per-key failures become error markers."""
    entry: dict = {"folder": folder, "data": {}, "errors": dict(resolve_errors)}
    # Stat BEFORE reading: if the file is rewritten mid-read, the entry
    # carries the OLD mtime, so the app's freshness check (current stat
    # vs entry mtime) fails safe: that test is not served from the snapshot.
    try:
        _st = proc_path.stat()
        entry["mtime_ns"] = _st.st_mtime_ns
        entry["size"] = _st.st_size
    except OSError as exc:
        entry["mtime_ns"] = 0
        for key in _DISPLAY_KEYS:
            entry["errors"][key] = f"cannot stat {proc_path.name}: {exc}"
        entry["text_newer"] = False
        return entry
    entry["text_newer"] = _text_newer(proc_path)
    try:
        doc = json.loads(proc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        for key in _DISPLAY_KEYS:
            entry["errors"][key] = f"cannot read {proc_path.name}: {exc}"
        return entry
    _extract_into(entry, doc, fns)
    return entry


def _extract_into(entry: dict, doc, fns: dict) -> None:
    """Each extractor of ``fns`` run on ``doc``, its JSON payload in
    ``entry["data"]``; one that raises is explained in ``entry["errors"]``."""
    for key, fn in fns.items():
        try:
            if key == "meta":
                entry["data"][key] = _meta_payload(fn(doc))
            else:
                entry["data"][key] = _items_payload(fn(doc))
        except Exception as exc:  # noqa: BLE001 — one bad test/key must not kill the sweep
            entry["errors"][key] = f"{type(exc).__name__}: {exc}"


def _op_display_snapshot(spec: dict) -> dict:
    """The main window's display data for the tests of ``spec["project"]``
    (``spec["tests"]``: null = every test folder), extracted by the bundle's
    PINNED wheel with the extractors ``defaults.json`` declares. The pack
    registry is the daemon's, loaded from the frame's ``_bundle_dir``.
    Per-test / per-key failures never fail the sweep: the key is absent from
    ``data`` and explained in ``errors``."""
    project_root = Path(spec["project"])
    requested = spec.get("tests")

    fns, resolve_errors, wheel_info = _resolve_extractors(_bundle_dir_of(spec))

    tests_dir = project_root / "tests"
    if requested is None:
        try:
            folders = sorted(d.name for d in tests_dir.iterdir() if d.is_dir())
        except OSError as exc:
            return {"ok": False, "kind": "Other", "error": f"cannot list {tests_dir}: {exc}"}
    else:
        folders = [f for f in requested if isinstance(f, str)]

    out = []
    for folder in folders:
        proc_path = tests_dir / folder / "procedure.json"
        if not proc_path.exists():
            # Deleted/never-compiled test: omit it — the store drops
            # requested-but-absent folders from the snapshot.
            continue
        out.append(_extract_test(folder, proc_path, fns, resolve_errors))
    return {"ok": True, "wheel": wheel_info, "tests": out}


def _bundle_dir_of(spec: dict) -> Path:
    """The bundle a frame's extractors come from: the frame's ``_bundle_dir``,
    else the project's ``bundle/``."""
    return Path(spec.get("_bundle_dir") or (Path(spec["project"]) / "bundle"))


def _op_extract(spec: dict) -> dict:
    """``spec["keys"]`` of each document of ``spec["procedures"]``, by the
    extractors the bundle declares — what the display snapshot does not
    hold: an untagged or just-edited document, and the validators. A key
    that cannot be run is explained in that result's ``errors``, never
    fails the op."""
    fns, errors, _wheel = _resolve_extractors(_bundle_dir_of(spec), tuple(spec.get("keys") or ()))
    results = []
    for doc in spec.get("procedures") or []:
        entry: dict = {"data": {}, "errors": dict(errors)}
        _extract_into(entry, doc, fns)  # any shape: a validator reports PROC_NOT_OBJECT
        results.append(entry)
    return {"ok": True, "results": results}


_OPS = {
    "is_available": _op_is_available,
    "parse_text": _op_parse_text,
    "render_text": _op_render_text,
    "refresh_media": _op_refresh_media,
    "export_rows": _op_export_rows,
    "renumber_steps": _op_renumber_steps,
    "sync_equipment": _op_sync_equipment,
    "sync_meta": _op_sync_meta,
    "generate_code": _op_generate_code,
    "validate": _op_validate,
    "section_ownership": _op_section_ownership,
    "reconstruct": _op_reconstruct,
    "build_manual_run": _op_build_manual_run,
    "build_manual_result": _op_build_manual_result,
    "manual_reading": _op_manual_reading,
    "criterion_refs": _op_criterion_refs,
    "pack_contract": _op_pack_contract,
    "bench_identity": _op_bench_identity,
    "display_snapshot": _op_display_snapshot,
    "extract": _op_extract,
}


# The run's own ops: all a RUN engine computes besides its instrument frames.
_RUN_OPS = ("build_manual_run", "build_manual_result", "manual_reading")


def refusal(role: str, req: dict) -> dict | None:
    """``Protocol`` for a frame outside ``role``'s job, else None. RUN takes the
    instrument and bench frames and the run's own ops; TOOLS takes every op and
    never a frame that could open a device. Both take ping and shutdown."""
    cmd, op = req.get("cmd"), req.get("op")
    takes = cmd == "op" if role == "tools" else (cmd != "op" or op in _RUN_OPS)
    if takes or cmd in ("ping", "shutdown"):
        return None
    what = f"op {op!r}" if cmd == "op" else f"{cmd!r}"
    return {"ok": False, "kind": "Protocol", "error": f"the {role} engine does not take {what}"}


def run(spec: dict, registry=None) -> dict:
    """Run ``spec["op"]``; never raises. ``registry(spec)``, when given, loads the
    packs first (the daemon's once-per-process load). A load failure does
    not fail the op: it degrades to 'unknown pack' findings. Unknown op →
    ``Other``; ImportError → ``ParserUnavailable``; any other exception →
    ``Other`` with its traceback."""
    try:
        if registry is not None:
            registry(spec)
    except Exception:  # noqa: BLE001
        pass
    op = spec.get("op")
    if op not in _OPS:
        return {
            "ok": False,
            "kind": "Other",
            "error": f"Unknown op: {op!r}. Valid ops: {sorted(_OPS)}",
        }
    try:
        return _OPS[op](spec)
    except ImportError as exc:
        return {
            "ok": False,
            "kind": "ParserUnavailable",
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": "Other",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
