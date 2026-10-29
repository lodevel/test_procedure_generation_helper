"""The equipment types a procedure text declares, found by a heading scan that
mirrors the wheel's lenient heading forms (the section universe comes from
the bundle's ownership map)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from procedo.core import section_ownership

from . import pack_parsers

_EQUIP_LINE_RE = None  # lazily compiled
_HEADING_LINE_RE = None  # lazily compiled


def _heading_name(line: str, known_sections: frozenset[str]) -> Optional[str]:
    """Section name if *line* is a ``##`` section heading, else None.

    Local mirror of the wheel parser's heading FORM semantics (the editor must
    not import the wheel at module level): ``##`` + OPTIONAL space/tab + name —
    ``## Equipment`` and ``##Equipment`` both name ``Equipment``; ``###...``
    and a bare ``##`` are content, never headings. The NO-SPACE form is a
    heading only for a name in *known_sections* (lowercased), so prose like
    ``##note`` stays content. Unlike the wheel, the known-name check is
    case-insensitive (the ownership-map keys are lowercase; labels are
    display-only)."""
    global _HEADING_LINE_RE
    if _HEADING_LINE_RE is None:
        _HEADING_LINE_RE = re.compile(r"^##(?!#)[ \t]*(\S.*?)[ \t]*$")
    m = _HEADING_LINE_RE.match(line)
    if m is None:
        return None
    name = m.group(1)
    if line[2:3] in (" ", "\t"):  # spaced form: heading for any name
        return name
    return name if name.lower() in known_sections else None


def section_scan_names(
    project_root: Optional[Path] = None,
) -> tuple[str, frozenset[str]]:
    """Resolve ``(equipment_section_name, known_section_names)`` for the
    heading scan in :func:`equipment_types_in`; both lowercased.

    The section universe is bundle-driven — the ownership-map keys from
    :func:`.pack_parsers.get_section_ownership` when loadable — falling back to the
    canonical v2.0.x names. The equipment section is the universe entry named
    ``equipment`` when declared, else the same literal: the ONE place to
    change if a bundle ever renames the section."""
    names: tuple[str, ...] = ()
    try:
        names = tuple(
            str(n).strip().lower() for n in pack_parsers.get_section_ownership(project_root)
        )
    except Exception:  # noqa: BLE001 — no wheel/bundle reachable: fallback
        pass
    known = frozenset(n for n in names if n) or section_ownership.CANONICAL_SECTIONS
    # test_id has no '## ' heading form (it is the '# <id>' line) — it must
    # never gate the NO-SPACE heading match, or a literal '##test_id' body
    # line would terminate a section (wheel treats it as content).
    known = frozenset(n for n in known if n != "test_id")
    equipment = next((n for n in known if n == "equipment"), "equipment")
    return equipment, known


def equipment_types_in(
    procedure_text: str,
    project_root: Optional[Path] = None,
) -> list[str]:
    """Extract the declared equipment types from a procedure's ``## Equipment``
    block (lines ``<ID> : <type> [params]``). Order-preserving, de-duplicated.
    Used to decide which packs an action needs.

    Heading matching is lenient, mirroring the wheel (:func:`_heading_name`):
    ``## Equipment`` and ``##Equipment`` both open the block, case-insensitive;
    ``###...`` is content. The section name comes from the bundle's declared
    universe when available (:func:`section_scan_names`)."""
    if not (procedure_text or "").strip():
        return []  # an empty editor uses no equipment: the bundle is not asked
    global _EQUIP_LINE_RE
    if _EQUIP_LINE_RE is None:
        _EQUIP_LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]*\s*:\s*(\S+)")
    target, known = section_scan_names(project_root)
    types: list[str] = []
    in_block = False
    for raw in (procedure_text or "").splitlines():
        line = raw.strip()
        heading = _heading_name(line, known)
        if heading is not None:
            in_block = heading.strip().lower() == target
            continue
        if in_block:
            m = _EQUIP_LINE_RE.match(line)
            if m and m.group(1) not in types:
                types.append(m.group(1))
    return types
