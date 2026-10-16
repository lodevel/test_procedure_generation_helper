"""Aggregate chat metadata stays compact without weakening connectivity."""
from copy import deepcopy
import json

import pytest

from procedo.studio.authoring.netlist_text import (
    format_board_context, format_component_ids, format_netlist,
    format_other_component_ids,
)


def _component(refdes, properties, pin_count=2):
    return {"refdes": refdes, "side": "TOP", "properties": properties,
            "pins": [{"name": str(pin), "net": f"N{pin}"}
                     for pin in range(pin_count)]}


def _board(*components):
    nets = {}
    for comp in components:
        for pin in comp["pins"]:
            nets.setdefault(pin["net"], []).append(
                {"refdes": comp["refdes"], "pin": pin["name"]})
    return {"components": list(components),
            "nets": [{"net": net, "nodes": nodes} for net, nodes in nets.items()]}


def _schema(text):
    return json.loads(next(line.removeprefix("Property fields: ")
                           for line in text.splitlines()
                           if line.startswith("Property fields: ")))


def _properties(text, refdes):
    return next((line for line in text.splitlines() if line.startswith(f"- {refdes}: ")), "")


def test_projection_keeps_exact_connections_values_and_cached_input():
    comp = _component("U_é", {"EDA.ManufacturerPartNumber": "  µC\\'42  ",
                              "EDA.category": "MCU", "VALUE": 0,
                              "acme.TestAccess": False,
                              "Supplier.Name": "DROP_SUPPLIER"})
    data = _board(comp, _component("PAD9", {"Footprint": "TEST_PAD"}, 1))
    before = deepcopy(data)
    text = format_board_context(data)
    assert format_netlist(data) in text
    assert "components=2, pins=3, nets=2, nodes=3" in text
    assert "EDA.ManufacturerPartNumber=" + repr("  µC\\'42  ") in text
    assert "VALUE=0" in text and "acme.TestAccess=False" in text
    assert "Footprint='TEST_PAD'" in text and "DROP_SUPPLIER" not in text
    assert data == before
    assert format_board_context(data) == text


@pytest.mark.parametrize("key", [
    "Manufacturer Part Number", "manufacturer_part_number", "ManufacturerPartNumber",
    "schema:MANUFACTURER-PART-NUMBER", "schema/Part No", "schema.Vendor reference",
    "MPN", "MfgRef", "Manufacturer_Reference", "MfrPartNo", "Part", "componentName",
])
def test_identity_names_allow_case_separators_camel_case_and_namespaces(key):
    text = format_board_context(_board(_component("U1", {key: "CHIP_1"})))
    assert f"{key}='CHIP_1'" in _properties(text, "U1")
    assert _schema(text) == {"selected": [key], "omitted": []}


def test_each_row_uses_best_populated_role_and_fallbacks():
    data = _board(
        _component("U1", {"MPN": "CHIP", "Name": "generic", "Type": "MCU",
                          "Category": "duplicate", "Function": "Control",
                          "Description": "DROP_LONG", "Value": "3V3"}),
        _component("U2", {"MPN": None, "Name": "FALLBACK", "Type": " ",
                          "Description": "Only identity description", "Value": ""}),
        _component("U3", {"MPN": "", "Name": "third", "Function": "Sense",
                          "Description": "DROP_THIRD"}),
    )
    text = format_board_context(data)
    assert _properties(text, "U1") == "- U1: MPN='CHIP', Type='MCU', Function='Control', Value='3V3'"
    assert _properties(text, "U2") == "- U2: Name='FALLBACK', Description='Only identity description'"
    assert _properties(text, "U3") == "- U3: Name='third', Function='Sense'"
    assert "DROP_" not in text and "duplicate" not in text
    assert "components_without_selected_identity=0" in text


def test_distinct_test_access_flags_are_not_collapsed_and_package_is_single_pin_only():
    props = {"EDA.TestPoint": False, "EDA.TestAccess": "yes", "ProbeAccess": "bottom",
             "Footprint": "PAD_FOOTPRINT", "Package": "DUPLICATE_PACKAGE"}
    text = format_board_context(_board(_component("PAD1", props, 1),
                                      _component("J1", props, 2)))
    for refdes in ("PAD1", "J1"):
        line = _properties(text, refdes)
        assert "EDA.TestPoint=False" in line and "EDA.TestAccess='yes'" in line
        assert "ProbeAccess='bottom'" in line
    assert "Footprint='PAD_FOOTPRINT'" in _properties(text, "PAD1")
    assert "Footprint=" not in _properties(text, "J1")
    assert "DUPLICATE_PACKAGE" not in text
    assert "alone is not proof of a test point" in text


def test_unknown_and_noise_fields_are_named_once_but_their_values_are_not_guessed():
    props = {key: "SECRET_TEST_POINT_MCU" for key in (
        "CustomField", "Supplier.Name", "DocumentDescription", "AssemblyType",
        "geometry.type", "ManufacturerName", "Acme.Manufacturer.Name", "Url")}
    text = format_board_context(_board(_component("U1", props), _component("U2", props)))
    assert "SECRET_TEST_POINT_MCU" not in text
    assert _schema(text) == {"selected": [], "omitted": sorted(props)}
    for key in props:
        assert text.count('"' + key + '"') == 1
    assert "components_with_properties=0" in text
    assert "components_without_selected_properties=2" in text
    assert "components_without_selected_identity=2" in text
    assert "- N1: U1.1, U2.1" in text
    assert "missing identities or unknown nets are unknown, not proof of absence" in text


def test_blank_values_do_not_mask_zero_false_or_fallback_description():
    data = _board(_component("R1", {"Value": 0, "MPN": None, "Name": "  ",
                                    "Type": None, "Description": "Resistor",
                                    "TestPoint": False}))
    text = format_board_context(data)
    assert _properties(text, "R1") == "- R1: Value=0, Description='Resistor', TestPoint=False"
    assert _schema(text)["omitted"] == ["MPN", "Name", "Type"]
    assert "components_with_properties=1" in text
    assert "components_without_selected_identity=1" in text


def test_existing_wizard_identity_context_keeps_unselected_fields():
    data = _board(_component("U1", {"CustomField": "CUSTOM_CHIP"}),
                  _component("R1", {"PurchasingNote": "CUSTOM_PASSIVE"}))
    assert "CustomField='CUSTOM_CHIP'" in format_component_ids(data)
    assert "PurchasingNote='CUSTOM_PASSIVE'" in format_other_component_ids(data)
    text = format_board_context(data)
    assert "CUSTOM_CHIP" not in text and "CUSTOM_PASSIVE" not in text


def test_noisy_board_projects_columns_without_dropping_tail_or_connectivity():
    data = _board(*[_component(f"U{index}", {
        "MPN": f"PART_{index}", "Type": "MCU", "Name": "GENERIC",
        "Description": "Repeated documentation " * 30,
        "SupplierNotes": "Purchasing information " * 30,
        "Datasheet": "https://example.invalid/" + "x" * 100,
        "AssemblyNotes": "Assembly information " * 30,
    }) for index in range(250)])
    text = format_board_context(data)
    previous_properties = format_component_ids(data) + format_other_component_ids(data)
    assert len(text.encode("utf-8")) < len(previous_properties.encode("utf-8")) / 10
    assert "- U249: MPN='PART_249', Type='MCU'" in text
    assert format_netlist(data) in text
    assert "components=250, pins=500, nets=2, nodes=500" in text
    assert text.splitlines()[-1].startswith("BOARD_CONTEXT_END:")
