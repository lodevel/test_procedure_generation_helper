"""Parse the DCDC *finder* skill's numbered worklist into structured rows.

The finder's final deliverable is a numbered list, one line per power IC, in the
canonical shape (authoring_wizards/dcdc_finder/SKILL.md:105)::

    U# — <part> (LDO|DC-DC) → <rail>

This module turns that LLM reply (prose, list numbering, backticks and all) into
``list[IcRow]`` — the worklist a downstream wizard step turns into checkboxes and
feeds, one IC at a time, to the authoring half.

PURE: no Qt, no I/O. The reply text is matched line-by-line; non-matching lines
(headers, prose, blank) are silently skipped, so an empty/garbage reply yields
``[]`` rather than raising.

Tolerances (the LLM is not byte-exact):
- arrow ``→`` OR ascii ``->`` / ``-->`` / ``=>`` between part-class and rail;
- separator em-dash ``—`` OR en-dash ``–`` OR ascii hyphen ``-`` between refdes
  and part;
- ``LDO`` / ``DC-DC`` (also ``DCDC`` / ``DC/DC``), case-insensitive;
- leading list numbering (``1.`` / ``2)``), bullet markers (``-`` / ``*`` / ``•``),
  surrounding backticks and other prose around the line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["IcRow", "parse_finder_list", "parse_classifier_list", "parse_rail_reply",
           "parse_rail_probe"]


@dataclass
class IcRow:
    """One power IC from the finder worklist.

    ``kind`` is normalised to exactly ``"LDO"`` or ``"DC-DC"``. ``refdes``,
    ``part`` and ``rail`` are preserved verbatim (only surrounding whitespace and
    backticks trimmed) — a rail may be an anonymous label (a test point, a net
    id, or the producing IC) when the finder couldn't name the net.
    """

    refdes: str
    part: str
    kind: str
    rail: str


# Structural core of a worklist line, found anywhere on the line (re.search), so
# leading numbering / bullets / backticks / prose are skipped without enumerating
# them. The `(LDO|DC-DC)` class and the arrow are the load-bearing anchors that
# tell a real worklist row from prose — a line lacking either never matches.
_LINE_RE = re.compile(
    r"(?P<refdes>(?:U|IC)\d+\w*(?:\.\w+)?)"  # refdes: U86, IC3, U1A, U11.1, U34.3
    r"\s*[—–\-]+\s*"             # separator: em / en dash or hyphen
    r"(?P<part>.+?)"                       # part (non-greedy up to the class)
    r"\s*\(\s*(?P<kind>LDO|DC[-/]?DC)\s*\)"  # class: (LDO) / (DC-DC) / (DCDC)
    r"\s*(?:→|⟶|➔|➜|-{1,}>|=+>)\s*"  # arrow: → / -> / =>
    r"(?P<rail>.+)",                       # rail (to end of line)
    re.IGNORECASE,
)


def _norm_kind(raw: str) -> str:
    """Collapse a matched class token to canonical ``"LDO"`` / ``"DC-DC"``."""
    return "LDO" if raw.strip().lower() == "ldo" else "DC-DC"


def _clean(token: str) -> str:
    """Trim whitespace and surrounding backticks from a captured field."""
    return token.strip().strip("`").strip()


def parse_finder_list(text: str) -> list[IcRow]:
    """Extract the finder worklist rows from a finder skill reply.

    Scans ``text`` line by line; each line matching the worklist shape becomes
    one :class:`IcRow`. Non-matching lines (prose, headers, blanks, junk) are
    skipped. Returns ``[]`` for empty / ``None`` / no-match input — never raises.
    """
    if not text:
        return []
    rows: list[IcRow] = []
    for line in text.splitlines():
        m = _LINE_RE.search(line)
        if not m:
            continue
        part = _clean(m.group("part"))
        rail = _clean(m.group("rail"))
        if not part or not rail:
            continue
        rows.append(
            IcRow(
                refdes=_clean(m.group("refdes")),
                part=part,
                kind=_norm_kind(m.group("kind")),
                rail=rail,
            )
        )
    return rows


# --- V2: classifier output (NO rail) + per-IC turn-1 rail reply -------------- #

# The V2 classifier emits `U# — <part> (LDO|DC-DC)` with NO rail (rail-reading is a
# separate per-IC turn). Same anchors as the worklist line MINUS the arrow + rail; any
# trailing text (a stray arrow/rail/prose) after the class is ignored, so this also
# tolerates a finder-style line by dropping its rail.
_CLASSIFIER_LINE_RE = re.compile(
    r"(?P<refdes>(?:U|IC)\d+\w*(?:\.\w+)?)"   # refdes: U86, IC3, U11.1
    r"[\s`]*[—–\-]+[\s`]*"                    # separator (tolerate `backticks` around refdes/dash)
    r"(?P<part>.+?)"                          # part (non-greedy up to the class)
    r"\s*\(\s*(?P<kind>LDO|DC[-/]?DC)\s*\)",  # class: (LDO) / (DC-DC) / (DCDC)
    re.IGNORECASE,
)

# A per-IC turn-1 reply: `<refdes> → <rail>` (or just `→ <rail>`). Capture the rail
# token right after the arrow, stopping at whitespace / `,` / `;` so a trailing
# explanation ("+CAP_30V ; pin 11 is +Vout") keeps only the net.
_RAIL_REPLY_RE = re.compile(
    r"(?:→|⟶|➔|➜|-{1,}>|=+>)\s*(?P<rail>[^\s,;)]+)",
)

# An OPTIONAL probe-point HINT a turn-1 reply may append when the netname is generic:
# `<refdes> → <netname> (probe: <TP>)`. Display-only — the rail VALUE stays the netname.
_RAIL_PROBE_RE = re.compile(
    r"\(\s*probe\s*[:=]\s*(?P<probe>[^)]+?)\s*\)", re.IGNORECASE)


def parse_classifier_list(text: str) -> list[IcRow]:
    """Extract the V2 classifier rows (refdes + part + kind, NO rail) from a
    classifier skill reply. Each row's ``rail`` is ``""`` — it is filled later by the
    per-IC turn-1 rail-read. Returns ``[]`` for empty / no-match input; never raises."""
    if not text:
        return []
    rows: list[IcRow] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _CLASSIFIER_LINE_RE.search(line)
        if not m:
            continue
        part = _clean(m.group("part"))
        if not part:
            continue
        refdes = _clean(m.group("refdes"))
        if refdes in seen:           # one row per refdes (the LLM may echo a line)
            continue
        seen.add(refdes)
        rows.append(IcRow(refdes=refdes, part=part,
                          kind=_norm_kind(m.group("kind")), rail=""))
    return rows


def parse_rail_reply(text: str) -> str:
    """Extract the rail net from a per-IC turn-1 reply (`<refdes> → <rail>`).

    Returns the FIRST rail token after an arrow, cleaned; ``""`` when none is found
    (the caller then keeps the row in its un-railed state). A multi-output module that
    replies several `→ <rail>` lines yields just the first here — multi-output is a
    later refinement, flagged in the plan."""
    if not text:
        return ""
    m = _RAIL_REPLY_RE.search(text)
    return _clean(m.group("rail")) if m else ""


def parse_rail_probe(text: str) -> str:
    """Extract the OPTIONAL probe-point hint a turn-1 reply appends when the rail's
    netname is generic — ``<refdes> → <netname> (probe: <TP>)``. Returns the TP label,
    cleaned; ``""`` when none. Display-only — the rail VALUE is still ``parse_rail_reply``."""
    if not text:
        return ""
    m = _RAIL_PROBE_RE.search(text)
    return _clean(m.group("probe")) if m else ""
