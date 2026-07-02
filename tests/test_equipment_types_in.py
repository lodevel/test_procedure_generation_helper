"""Unit tests for ``workflow_editor.llm.pack_parsers.equipment_types_in``.

Heading recognition must mirror the wheel's lenient form semantics:
``##`` + OPTIONAL space + name (``## Equipment`` == ``##Equipment``),
``###...`` is content, unknown NO-SPACE names (``##note``) are content.
The equipment section name comes from the bundle's declared universe
(ownership-map keys) when available, else the literal ``equipment``.

Runs without PySide6 and without the wheel installed (the section-universe
lookup degrades to the canonical fallback):
    PYTHONPATH=. python3 -m pytest tests/test_equipment_types_in.py --noconftest -q
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers as pp  # noqa: E402

EQUIP_LINES = """PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]
ELOAD1 : eload channels=[{1, max_current=5.0 A}]
"""


def _text(heading: str, tail: str = "\n## Steps\n1. Do nothing.\n") -> str:
    return f"# TEST_X\nSome description.\n\n{heading}\n{EQUIP_LINES}{tail}"


class EquipmentTypesInHeadingForms(unittest.TestCase):
    """Lenient heading matching (defect a: no-space form was missed)."""

    def test_spaced_heading_matches(self):
        self.assertEqual(pp.equipment_types_in(_text("## Equipment")),
                         ["psu", "eload"])

    def test_no_space_heading_matches(self):
        self.assertEqual(pp.equipment_types_in(_text("##Equipment")),
                         ["psu", "eload"])

    def test_h3_heading_is_content_not_matched(self):
        self.assertEqual(pp.equipment_types_in(_text("###Equipment")), [])
        self.assertEqual(pp.equipment_types_in(_text("### Equipment")), [])

    def test_mixed_case_heading_matches(self):
        self.assertEqual(pp.equipment_types_in(_text("## EQUIPMENT")),
                         ["psu", "eload"])
        self.assertEqual(pp.equipment_types_in(_text("##equipment")),
                         ["psu", "eload"])

    def test_no_equipment_section_yields_empty(self):
        text = "# TEST_X\n\n## Steps\n1. Do nothing.\n\n## Expected\n"
        self.assertEqual(pp.equipment_types_in(text), [])
        self.assertEqual(pp.equipment_types_in(""), [])
        self.assertEqual(pp.equipment_types_in(None), [])

    def test_extra_spacing_tolerated(self):
        self.assertEqual(pp.equipment_types_in(_text("##  Equipment")),
                         ["psu", "eload"])
        self.assertEqual(pp.equipment_types_in(_text("## Equipment  ")),
                         ["psu", "eload"])


class EquipmentTypesInBlockBoundaries(unittest.TestCase):
    """The block ends at the next heading — in either form."""

    def test_no_space_known_section_ends_block(self):
        text = _text("## Equipment", tail="##Steps\nPSU9 : siggen\n")
        self.assertEqual(pp.equipment_types_in(text), ["psu", "eload"])

    def test_unknown_no_space_name_is_content_block_continues(self):
        # ``##note`` mirrors the wheel: NOT a heading, the block continues.
        text = _text("## Equipment",
                     tail="##note\nSIG1 : siggen\n\n## Steps\n1. X.\n")
        self.assertEqual(pp.equipment_types_in(text),
                         ["psu", "eload", "siggen"])

    def test_spaced_unknown_section_ends_block(self):
        text = _text("## Equipment",
                     tail="## Anything\nSIG1 : siggen\n")
        self.assertEqual(pp.equipment_types_in(text), ["psu", "eload"])

    def test_types_deduplicated_order_preserved(self):
        text = _text("##Equipment",
                     tail="PSU2 : psu\nSIG1 : siggen\n\n## Steps\n")
        self.assertEqual(pp.equipment_types_in(text),
                         ["psu", "eload", "siggen"])


class EquipmentTypesInUniverseResolution(unittest.TestCase):
    """Section name/universe comes from the bundle's declared ownership map."""

    def test_universe_from_ownership_map_keys(self):
        with mock.patch.object(
            pp, "get_section_ownership",
            return_value={"meta": "parser", "equipment": "llm", "steps": "llm"},
        ) as gso:
            self.assertEqual(pp.equipment_types_in(_text("##Equipment")),
                             ["psu", "eload"])
        gso.assert_called_with(None)

    def test_ownership_lookup_failure_falls_back_to_canonical(self):
        with mock.patch.object(
            pp, "get_section_ownership",
            side_effect=pp.ParserUnavailable("no wheel"),
        ):
            self.assertEqual(pp.equipment_types_in(_text("##Equipment")),
                             ["psu", "eload"])

    def test_section_outside_declared_universe_stays_content_in_no_space_form(self):
        # ``##Steps`` is NOT in this bundle's universe -> content, block continues.
        with mock.patch.object(
            pp, "get_section_ownership",
            return_value={"equipment": "llm", "expected": "llm"},
        ):
            text = _text("## Equipment", tail="##Steps\nSIG1 : siggen\n")
            self.assertEqual(pp.equipment_types_in(text),
                             ["psu", "eload", "siggen"])

    def test_project_root_threaded_to_ownership_lookup(self):
        from pathlib import Path
        root = Path("/some/project")
        with mock.patch.object(
            pp, "get_section_ownership",
            return_value={"equipment": "llm"},
        ) as gso:
            pp.equipment_types_in(_text("## Equipment"), root)
        gso.assert_called_with(root)


if __name__ == "__main__":
    unittest.main()


def test_no_space_test_id_is_content_not_a_heading():
    """gpt-5.5 review regression: 'test_id' has no '## ' heading form, so a
    literal '##test_id' body line must NOT terminate the Equipment block."""
    text = (
        "## Equipment\n"
        "PSU1 : psu\n"
        "##test_id\n"
        "LOAD1 : eload\n"
    )
    from workflow_editor.llm.pack_parsers import equipment_types_in
    assert equipment_types_in(text) == ["psu", "eload"]
