"""Tests for component-property rendering in the LLM-readable netlist text.

Properties (part number / value / description) must surface so the skill chat
can identify ICs, but a component with no properties MUST render byte-identically
to before (existing tests assert exact substrings like
"- U1 [top]: 1=VIN, 4=EN, 5=VOUT").
"""
from workflow_editor.authoring import format_netlist


def test_props_segment_appears_when_properties_present():
    board = {
        "components": [
            {"refdes": "U1", "side": "top", "pins": [
                {"name": "1", "net": "VIN"},
                {"name": "5", "net": "VOUT"},
            ], "properties": {"NAME": "TPS62840", "VALUE": "10uF"}},
        ],
        "nets": [],
    }
    text = format_netlist(board)
    # Pins still render exactly as before, props appended AFTER them.
    assert "- U1 [top]: 1=VIN, 5=VOUT  {props: NAME='TPS62840', VALUE='10uF'}" in text


def test_empty_properties_render_byte_identical():
    """A component with empty/absent properties must be unchanged from today."""
    pins = [
        {"name": "1", "net": "VIN"},
        {"name": "4", "net": "EN"},
        {"name": "5", "net": "VOUT"},
    ]
    expected_line = "- U1 [top]: 1=VIN, 4=EN, 5=VOUT"

    # absent properties key
    board_absent = {"components": [{"refdes": "U1", "side": "top", "pins": pins}], "nets": []}
    assert expected_line in format_netlist(board_absent)
    assert "{props:" not in format_netlist(board_absent)

    # explicit empty dict
    board_empty = {"components": [
        {"refdes": "U1", "side": "top", "pins": pins, "properties": {}}], "nets": []}
    assert expected_line in format_netlist(board_empty)
    assert "{props:" not in format_netlist(board_empty)

    # both must produce the identical full text
    assert format_netlist(board_absent) == format_netlist(board_empty)


def test_props_on_component_without_pins():
    board = {"components": [
        {"refdes": "U9", "properties": {"PART": "LM317"}}], "nets": []}
    text = format_netlist(board)
    assert "- U9  {props: PART='LM317'}" in text


def test_non_mapping_properties_ignored():
    board = {"components": [
        {"refdes": "U1", "pins": [{"name": "1", "net": "V"}], "properties": "junk"}],
        "nets": []}
    text = format_netlist(board)
    assert "- U1: 1=V" in text
    assert "{props:" not in text
