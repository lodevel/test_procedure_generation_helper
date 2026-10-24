"""The guided run's one reading rule (the engine op ``manual_reading``): what a reading entered
as text stores for its step, and why a typed one is refused. Part of the engine's op table
(``_engine_ops``), loaded beside it — never through the ``procedo`` package, which is
absent from the project venv."""

from __future__ import annotations

import re

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
