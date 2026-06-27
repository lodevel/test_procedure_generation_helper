"""Tests for the DCDC finder worklist parser (wizard/list_parse.py)."""
from __future__ import annotations

from workflow_editor.authoring.wizard.list_parse import IcRow, parse_finder_list


# A realistic finder reply: intro prose, a header, a numbered list mixing LDO and
# DC-DC, ascii vs unicode arrows/dashes, backticks, an anonymous test-point rail,
# and a trailing note — exactly the shape the SKILL.md spec produces.
REALISTIC_REPLY = """\
I walked the whole BOM and classified every U*/IC* row by function.
Here is the per-IC worklist (one line per power IC):

1. U86 — TPS62933 (DC-DC) → 3V3
2. U3 — LP5907 (LDO) → 1V8
3. `U12 - LM2596 (DC-DC) -> 5V0`
4. IC4 — TPS7A47 (LDO) → +AUX0_16V
5. U21 — LTC3895 (DC-DC) → DISCH_16V

Note: U7 is the MCU and U9 is an op-amp, so both are excluded (no set-point).
"""


def test_realistic_multi_ic_reply_parses_all_rows():
    rows = parse_finder_list(REALISTIC_REPLY)
    assert rows == [
        IcRow(refdes="U86", part="TPS62933", kind="DC-DC", rail="3V3"),
        IcRow(refdes="U3", part="LP5907", kind="LDO", rail="1V8"),
        IcRow(refdes="U12", part="LM2596", kind="DC-DC", rail="5V0"),
        IcRow(refdes="IC4", part="TPS7A47", kind="LDO", rail="+AUX0_16V"),
        IcRow(refdes="U21", part="LTC3895", kind="DC-DC", rail="DISCH_16V"),
    ]


def test_anonymous_rail_label_preserved():
    # A finder that couldn't name the net labels the rail by a test point / id.
    rows = parse_finder_list("7) U30 — MAX1763 (DC-DC) → net_0042")
    assert rows == [IcRow(refdes="U30", part="MAX1763", kind="DC-DC", rail="net_0042")]


def test_ascii_arrow_and_hyphen_separator():
    rows = parse_finder_list("U5 - AP2112 (LDO) -> 3V3")
    assert rows == [IcRow(refdes="U5", part="AP2112", kind="LDO", rail="3V3")]


def test_unicode_em_dash_and_arrow_no_numbering():
    rows = parse_finder_list("U99 — TPS54331 (DC-DC) → 12V")
    assert rows == [IcRow(refdes="U99", part="TPS54331", kind="DC-DC", rail="12V")]


def test_kind_normalised_to_canonical():
    rows = parse_finder_list(
        "U1 - X (dc-dc) -> A\n"
        "U2 - Y (DCDC) -> B\n"
        "U3 - Z (DC/DC) -> C\n"
        "U4 - W (ldo) -> D\n"
    )
    assert [r.kind for r in rows] == ["DC-DC", "DC-DC", "DC-DC", "LDO"]


def test_backtick_wrapped_line():
    rows = parse_finder_list("`U8 — NCP1117 (LDO) → 3V3`")
    assert rows == [IcRow(refdes="U8", part="NCP1117", kind="LDO", rail="3V3")]


def test_junk_and_prose_lines_ignored():
    text = """\
Here are the power ICs on the board:
- U7 is the MCU (excluded)
This line mentions U86 but is not a worklist row.
========================
Total: 0 regulators found so far
"""
    assert parse_finder_list(text) == []


def test_excluded_class_in_parens_not_matched():
    # A non LDO/DC-DC parenthetical (e.g. an exclusion basis) must not match.
    assert parse_finder_list("U7 — STM32 (MCU) → n/a") == []


def test_missing_arrow_not_matched():
    assert parse_finder_list("U7 — TPS62933 (DC-DC) 3V3") == []


def test_refdes_in_surrounding_prose_picks_worklist_refdes():
    # Earlier refdes mention in the same line must not be captured as the row.
    rows = parse_finder_list("See U7 for context; U86 — TPS62933 (DC-DC) → 3V3")
    assert rows == [IcRow(refdes="U86", part="TPS62933", kind="DC-DC", rail="3V3")]


def test_empty_input_returns_empty_list():
    assert parse_finder_list("") == []
    assert parse_finder_list(None) == []  # type: ignore[arg-type]
    assert parse_finder_list("   \n\n\t") == []


def test_dotted_subinstance_refdes_and_spaced_parts():
    """Real KC30 finder output: multi-output modules carry a ``.N`` sub-instance
    refdes (U11.1, U34.3), part numbers contain spaces (``TDN 1-2411WISM``) and a
    ``#`` (``LT8609AJDDM#TRPBF``), and a rail may be an anonymous net (NetC231.1_2).
    The earlier regex stopped refdes at ``\\w*`` and dropped every dotted row —
    losing the AUX modules + the TDN. All must parse."""
    reply = (
        "1. U5 — RBBA3000-50 (DC-DC) → +CAP_30V\n"
        "2. U11.1 — LMZM33604RLXR (DC-DC) → +AUX0_16V\n"
        "3. U11.2 — LMZM33604RLXR (DC-DC) → +AUX1_16V\n"
        "8. U34.1 — TDN 1-2411WISM (DC-DC) → NetC231.1_2\n"
        "13. U86 — LT8609AJDDM#TRPBF (DC-DC) → +MAIN_5V0\n"
    )
    rows = parse_finder_list(reply)
    assert [r.refdes for r in rows] == ["U5", "U11.1", "U11.2", "U34.1", "U86"]
    assert rows[3] == IcRow("U34.1", "TDN 1-2411WISM", "DC-DC", "NetC231.1_2")
    assert rows[4].part == "LT8609AJDDM#TRPBF"
