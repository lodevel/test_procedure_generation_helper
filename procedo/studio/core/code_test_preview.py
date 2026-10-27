"""What Studio shows of a code test from the engine's *describe* of it (boards StudioCode and
StudioCompare): its phase list, the preview of its export, and its Check's results line.
Qt-free: the code view (``tabs.code_test_view``) paints them."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from procedo.core import test_format
from procedo.core.code_test_check import CodeCheck

#: The file a phase in it is written in: a phase's ``file`` in describe.
TEST_FILE = test_format.CODE_SCRIPT


@dataclass(frozen=True)
class PhaseRow:
    """One row of the phase list: a phase (``depth`` 0), or one of its sub-steps and
    measurements (1). ``line``: where a click jumps in openhtf/test.py (0: it is in another
    file, ``tip`` says which)."""

    depth: int
    text: str
    line: int
    tip: str = ""


def is_shared(phase: dict) -> bool:
    """The phase is one of the project's reusable phases (``functions/``)."""
    return str(phase.get("file", "")).startswith("functions/")


def _unit(measurement: dict) -> str:
    """The measurement's unit as describe gives it: OpenHTF's suffix (``V``)."""
    return str(measurement.get("units") or "")


def _with_unit(value, unit: str) -> str:
    return f"{value} {unit}" if unit else str(value)


def limits(measurement: dict) -> str:
    """``4.75–5.25 V``, ``≥ 4.75 V``, ``≤ 5.25 V``, else its validators as OpenHTF writes them
    ("" without any)."""
    low, high, unit = measurement.get("lower"), measurement.get("upper"), _unit(measurement)
    if low is not None and high is not None:
        return f"{low}–{_with_unit(high, unit)}"
    if low is not None:
        return f"≥ {_with_unit(low, unit)}"
    if high is not None:
        return f"≤ {_with_unit(high, unit)}"
    return "; ".join(measurement.get("validators") or ())


def _expected(measurement: dict) -> str:
    """A check as the export's Expected says it: ``between 4.75 V and 5.25 V``."""
    low, high, unit = measurement.get("lower"), measurement.get("upper"), _unit(measurement)
    if low is not None and low == high:
        return f"equal to {_with_unit(low, unit)}"
    if low is not None and high is not None:
        return f"between {_with_unit(low, unit)} and {_with_unit(high, unit)}"
    if low is not None:
        return f"at least {_with_unit(low, unit)}"
    if high is not None:
        return f"at most {_with_unit(high, unit)}"
    return "; ".join(measurement["validators"])


def phase_rows(described: dict) -> list[PhaseRow]:
    """The phase list: ``1 Power-up checks``, its sub-steps ``1.1 …``, its measurements
    ``v5 4.75–5.25 V``; a reusable phase ``2 Power off (shared)``."""
    rows = []
    for n, phase in enumerate(described.get("phases") or (), 1):
        here = phase.get("file") == TEST_FILE
        tip = "" if here else f"In {phase.get('file')}, line {phase.get('line')}."

        def at(line: int) -> int:
            return line if here else 0

        shared = " (shared)" if is_shared(phase) else ""
        rows.append(PhaseRow(0, f"{n} {phase['name']}{shared}", at(phase["line"]), tip))
        for k, sub in enumerate(phase.get("substeps") or (), 1):
            rows.append(PhaseRow(1, f"{n}.{k} {sub['text']}", at(sub["line"]), tip))
        for measurement in phase.get("measurements") or ():
            text = " ".join(t for t in (measurement["name"], limits(measurement)) if t)
            rows.append(PhaseRow(1, text, at(phase["line"]), tip))
    return rows


def _equipment(described: dict) -> str:
    """``Dmm (DMM1), Psu (PSU1)``: each instrument and the address constant named after it."""
    names = [c["name"].split("_")[0] for c in described.get("constants") or ()]
    out = []
    for kind in described.get("instruments") or ():
        mine = [name for name in names if name.upper().startswith(kind.upper())]
        out.append(f"{kind} ({', '.join(mine)})" if mine else kind)
    return ", ".join(out)


def preview_html(described: dict) -> str:
    """The export as the report will show it, read from the code: each phase numbered with its
    description, its sub-steps and its Expected lines; then the equipment."""
    parts = []
    for n, phase in enumerate(described.get("phases") or (), 1):
        head = f"<b>{n}. {escape(phase['name'])}</b>"
        if is_shared(phase):
            head += f' <span style="color:gray">(shared phase, {escape(phase["file"])})</span>'
        parts.append(f'<p style="margin:{0 if n == 1 else 12}px 0 0 0">{head}</p>')
        if phase.get("description"):
            parts.append(f'<p style="margin:0">{escape(phase["description"])}</p>')
        subs = [
            f"{n}.{k} {escape(s['text'])}" for k, s in enumerate(phase.get("substeps") or (), 1)
        ]
        checks = [m for m in phase.get("measurements") or () if m.get("validators")]
        expected = [
            "Expected: "
            + escape(m["name"])
            + (f" ({escape(m['description'])})" if m.get("description") else "")
            + f" {escape(_expected(m))}"
            for m in checks
        ]
        for lines in (subs, expected):
            if lines:
                parts.append(f'<p style="margin:0 0 0 18px">{"<br>".join(lines)}</p>')
    equipment = _equipment(described)
    if equipment:
        parts.append(f'<hr><p style="margin:0">Equipment: {escape(equipment)}</p>')
    return "".join(parts)


def check_lines(found: CodeCheck) -> list[tuple[str, str, str]]:
    """The Check's results line, ``(tone, text, tooltip)`` each: ``✓ Syntax OK``, ``✓ Loads in
    the bundle's environment``, ``✓ 2 steps · 1 measurement · 2 instruments``; ``✗ why`` in
    place of what failed; ``⚠ N warnings`` (the tooltip lists them)."""
    if found.describe is None:  # a syntax error, or no engine: nothing else was read
        return [("danger", f"✗ {problem}", "") for problem in found.problems]
    lines = [("success", "✓ Syntax OK", "")]
    lines += [("danger", f"✗ {problem}", "") for problem in found.problems] or [
        ("success", "✓ Loads in the bundle's environment", "")
    ]
    lines.append(("success", f"✓ {found.summary}", ""))
    if found.warnings:
        n = len(found.warnings)
        lines.append(("warning", f"⚠ {n} warning{'s' * (n != 1)}", "\n".join(found.warnings)))
    return lines
