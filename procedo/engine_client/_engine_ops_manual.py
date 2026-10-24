"""The engine's guided-run ops: ``build_manual_run``, ``build_manual_result``, ``manual_reading``
and ``criterion_refs`` — what a guided run plans, shows, reads and reports. Part of the engine's
op table (``_engine_ops``), loaded beside it — never through the ``procedo`` package, which is
absent from the project venv. Their specs and results are documented in ``_engine_ops``.

``manual_reading`` is the guided run's one reading rule: what a reading entered as text stores
for its step, and why a typed one is refused."""

from __future__ import annotations

import functools
import re
from dataclasses import asdict

# A bus read typed by hand: `0x` + hex digits or decimal digits is the value;
# two or more hex pairs, single-spaced as the board prints them, are its bytes.
_PRINTED_BYTES_RE = re.compile(r"[0-9A-Fa-f]{2}(?: [0-9A-Fa-f]{2})+")


def op_manual_reading(spec: dict) -> dict:
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
    them here; JSON keeps a key as text, read back by the wheel's ref_key_from_json."""
    from rules_packager_base.Result import ref_key_from_json

    return {"measurements": spec.get("measurements") or {}, "controls": spec.get("controls") or {},
            "operator_verdicts": {ref_key_from_json(k): v for k, v in (spec.get("operator_verdicts") or {}).items()}}


def op_build_manual_run(spec: dict) -> dict:
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
        "error": run.error,
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

        def _concrete(self, raw, node_path, bindings, status="pending", texts=None):
            self.names[node_path] = _turn_names(self.loops, node_path, bindings)
            return super()._concrete(raw, node_path, bindings, status, texts)

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
    none) and ``names`` (its loop turns' ``${…}``); a Call row's own
    ``outputs`` say each of its readings (its reading is the call's outcome)."""
    from rules_packager_base.test_helpers import entry_refs, value_text

    units: dict = {}
    for entry in session._build_rules(run.measurements, []).values():   # the RULES test.py judges now
        for ref in entry_refs(entry):
            if entry.get("units"):
                units.setdefault(ref, entry["units"])
    out = []
    for step in run.steps:
        text, note = session.binds.get(step.node_path, ("", ""))
        if step.op != "call" and readings.get(step.node_path) is not None:
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


def op_build_manual_result(spec: dict) -> dict:
    procedure_json, err = _document(spec)
    if err:
        return err
    from rules_packager_base.rules.v2_0_2.parser import build_manual_result

    result = build_manual_result(procedure_json, **run_inputs(spec), failures=spec.get("failures") or {},
                                 operator_stop=bool(spec.get("operator_stop")))
    return {"ok": True, "result": result}


# -- a guided run's typed readings ----------------------------------------------

def op_criterion_refs(spec: dict) -> dict:
    """The readings each results.json criterion judges (``spec["criteria"]``: key ->
    criterion), as the wheel's entry_refs reads them (test.py's reads), each ref
    as a string — what a report's units map and Measured column list."""
    from rules_packager_base.test_helpers import entry_refs

    criteria = spec.get("criteria") or {}
    return {"ok": True, "refs": {key: [str(r) for r in entry_refs(crit)] for key, crit in criteria.items()}}
