"""Tests for the dcdc-wizard done-signal block detector.

Covers find_dcdc_test_block / is_done:
  * a full generate_dcdc_test block is DETECTED and returned (starting at the
    '## Equipment' heading);
  * prose missing one of the three headings -> None;
  * the three headings OUT OF ORDER -> None;
  * a clarifying question reply -> None.
"""
import os
import sys

# Make the editor's workflow_editor package importable when run from the tests/
# dir (mirrors the other tests' sys.path bootstrap).
_HERE = os.path.dirname(os.path.abspath(__file__))
_EDITOR_ROOT = os.path.dirname(_HERE)
if _EDITOR_ROOT not in sys.path:
    sys.path.insert(0, _EDITOR_ROOT)

from workflow_editor.authoring.dcdc_test_generator import (  # noqa: E402
    DcDcTestParams,
    EnableParams,
    PowerGoodParams,
    PsuParams,
    generate_dcdc_test,
)
from workflow_editor.authoring.wizard.done_signal import (  # noqa: E402
    find_dcdc_test_block,
    is_done,
)


def _real_block() -> str:
    """A genuine generate_dcdc_test block (always-on enable, PG present)."""
    params = DcDcTestParams(
        rail_name="+MAIN_5V0",
        ic_refdes="U86",
        ic_part="LT8609A",
        vout_nominal_v=5.0,
        rail_test_point="MAIN_5V0",
        psu=PsuParams(input_voltage_v=28.0, input_current_a=10.0,
                      entry_pos="P4", entry_neg="P2"),
        enable=EnableParams(present=True, always_on=True),
        power_good=PowerGoodParams(present=True, test_point="PG_5V0"),
    )
    return generate_dcdc_test(params)


# ---------------------------------------------------------------------------
# A full block is detected + returned.
# ---------------------------------------------------------------------------

def test_full_block_detected_and_returned():
    block = _real_block()
    found = find_dcdc_test_block(block)
    assert found is not None
    # Returned verbatim from the '## Equipment' heading onward.
    assert found.startswith("## Equipment")
    assert "## Steps" in found
    assert "## Expected" in found
    assert found == block.rstrip()
    assert is_done(block) is True


def test_full_block_detected_with_surrounding_prose():
    block = _real_block()
    wrapped = (
        "Here is the bring-up test I generated for U86:\n\n"
        + block
        + "\n\nLet me know if the entry connector is wrong.\n"
    )
    found = find_dcdc_test_block(wrapped)
    assert found is not None
    assert found.startswith("## Equipment")
    # The leading prose is dropped; trailing prose rides along but the block is
    # still recognised (pragmatic detector — see module docstring).
    assert "## Steps" in found
    assert "## Expected" in found
    assert is_done(wrapped) is True


# ---------------------------------------------------------------------------
# Prose without all three headings -> None.
# ---------------------------------------------------------------------------

def test_missing_expected_heading_is_none():
    text = (
        "## Equipment\n"
        "PSU1 : psu channels=[{1, max_voltage=28.0 V, max_current=10.0 A}]\n\n"
        "## Steps\n"
        "1. Set PSU1 CH1 voltage = 28.0 V.\n"
    )
    assert find_dcdc_test_block(text) is None
    assert is_done(text) is False


def test_only_equipment_heading_is_none():
    text = "## Equipment\nPSU1 : psu channels=[{1}]\n"
    assert find_dcdc_test_block(text) is None
    assert is_done(text) is False


# ---------------------------------------------------------------------------
# Out-of-order headings -> None.
# ---------------------------------------------------------------------------

def test_out_of_order_headings_is_none():
    # All three headings present, but Expected precedes Steps.
    text = (
        "## Equipment\n"
        "PSU1 : psu channels=[{1}]\n\n"
        "## Expected\n"
        "{1} = 5.0 V +/- 3.0 %\n\n"
        "## Steps\n"
        "1. Set PSU1 CH1 voltage = 28.0 V.\n"
    )
    assert find_dcdc_test_block(text) is None
    assert is_done(text) is False


# ---------------------------------------------------------------------------
# A question reply -> None.
# ---------------------------------------------------------------------------

def test_question_reply_is_none():
    text = (
        "Before I can author the test for U86 (TPS62933 -> 3V3), which connector "
        "node feeds board power, and is there a controllable enable? "
        "Should I probe the rail at TP MAIN_3V3?"
    )
    assert find_dcdc_test_block(text) is None
    assert is_done(text) is False


def test_empty_text_is_none():
    assert find_dcdc_test_block("") is None
    assert find_dcdc_test_block(None) is None  # type: ignore[arg-type]
    assert is_done("") is False


# ---------------------------------------------------------------------------
# Headings must be standalone lines, not inline mentions.
# ---------------------------------------------------------------------------

def test_inline_heading_mentions_are_not_a_block():
    text = (
        "I'll fill in the ## Equipment and ## Steps and ## Expected sections "
        "once you confirm the rail."
    )
    assert find_dcdc_test_block(text) is None
    assert is_done(text) is False


if __name__ == "__main__":  # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
