"""The component-ID (part number) block is SEPARATE from the connectivity
netlist, so a wiring-only skill isn't charged the property tokens. Part numbers
surface only for ICs (U*/IC* refdes); passives are skipped (the token bulk).
"""
from workflow_editor.authoring import format_component_ids, format_netlist


def test_component_ids_lists_ic_properties():
    board = {"components": [
        {"refdes": "U1", "properties": {"NAME": "TPS62840", "VALUE": "10uF"}},
    ], "nets": []}
    text = format_component_ids(board)
    assert "Component part numbers (ICs):" in text
    assert "- U1: NAME='TPS62840', VALUE='10uF'" in text


def test_component_ids_drops_empty_values():
    board = {"components": [
        {"refdes": "U2", "properties": {"PART": "LM317", "Lambda": "", "Note": "  "}},
    ], "nets": []}
    text = format_component_ids(board)
    assert "PART='LM317'" in text
    assert "Lambda" not in text and "Note" not in text


def test_component_ids_skips_passives_keeps_ics():
    board = {"components": [
        {"refdes": "R10", "properties": {"Value": "10k"}},
        {"refdes": "C5", "properties": {"Value": "100nF"}},
        {"refdes": "USB1", "properties": {"Part": "connector"}},   # U-prefix but not an IC
        {"refdes": "U7", "properties": {"Part": "NCP730"}},
        {"refdes": "IC3", "properties": {"Part": "ADXYZ"}},
    ], "nets": []}
    text = format_component_ids(board)
    assert "U7" in text and "NCP730" in text
    assert "IC3" in text and "ADXYZ" in text       # IC* prefix matches
    assert "R10" not in text and "C5" not in text and "USB1" not in text


def test_component_ids_empty_when_no_ic_props():
    board = {"components": [{"refdes": "R1", "properties": {"Value": "1k"}}], "nets": []}
    assert format_component_ids(board) == ""
    # also empty when the ICs have no non-empty props
    board2 = {"components": [{"refdes": "U1", "properties": {"X": ""}}], "nets": []}
    assert format_component_ids(board2) == ""


def test_netlist_carries_no_properties():
    # Connectivity netlist must NOT carry properties anymore (they moved out).
    board = {"components": [
        {"refdes": "U1", "side": "top", "pins": [{"name": "1", "net": "VIN"}],
         "properties": {"NAME": "TPS62840"}}], "nets": []}
    text = format_netlist(board)
    assert "- U1 [top]: 1=VIN" in text
    assert "TPS62840" not in text and "{props" not in text and "NAME=" not in text
