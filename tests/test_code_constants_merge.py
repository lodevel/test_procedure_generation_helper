"""Unit tests for ``workflow_editor.llm.code_constants_merge``.

Covers the failure modes the architecture reviewer flagged:
  - Type-annotated assignments (``NAME: type = LITERAL``) on either side.
  - Multi-line value spans collapsing cleanly to one line.
  - AST-based extraction including ``ast.AnnAssign`` nodes.
  - Equipment-id derivation from a procedure dict.
  - Idempotence (running twice with no existing pins is a no-op).
"""

from __future__ import annotations

import unittest

from workflow_editor.llm.code_constants_merge import (
    equipment_ids_from_procedure,
    extract_pinned_constants,
    merge_pinned_constants,
    preserve_bench_constants,
)


class ExtractPinnedConstantsTests(unittest.TestCase):

    def test_plain_assignment(self) -> None:
        code = "PSU1_VISA = 'TCPIP0::psu.lab.local::INSTR'\n"
        out = extract_pinned_constants(code, ["PSU1"])
        self.assertEqual(out, {"PSU1_VISA": "TCPIP0::psu.lab.local::INSTR"})

    def test_type_annotated_assignment(self) -> None:
        code = "PSU1_VISA: str = 'TCPIP0::psu.lab.local::INSTR'\n"
        out = extract_pinned_constants(code, ["PSU1"])
        self.assertEqual(out, {"PSU1_VISA": "TCPIP0::psu.lab.local::INSTR"})

    def test_multiple_constants(self) -> None:
        code = (
            "PSU1_VISA = 'X'\n"
            "PSU1_CHANNEL = 2\n"
            "PSU1_REMOTE = True\n"
            "FNCORE_PORT = 'COM22'\n"
            "FNCORE_BAUD = 115200\n"
        )
        out = extract_pinned_constants(code, ["PSU1", "FNCORE"])
        self.assertEqual(out, {
            "PSU1_VISA": "X", "PSU1_CHANNEL": 2, "PSU1_REMOTE": True,
            "FNCORE_PORT": "COM22", "FNCORE_BAUD": 115200,
        })

    def test_skips_unknown_equipment(self) -> None:
        code = "PSU2_VISA = 'X'\n"
        out = extract_pinned_constants(code, ["PSU1"])
        self.assertEqual(out, {})

    def test_skips_non_literal_values(self) -> None:
        code = (
            "import os\n"
            "PSU1_VISA = os.environ['PSU1_VISA']\n"
            "PSU1_CHANNEL = 2\n"
        )
        out = extract_pinned_constants(code, ["PSU1"])
        # Only the literal-valued one survives.
        self.assertEqual(out, {"PSU1_CHANNEL": 2})

    def test_ann_assign_without_value(self) -> None:
        # Type-only declaration (PEP 526) — no value to extract.
        code = "PSU1_VISA: str\n"
        out = extract_pinned_constants(code, ["PSU1"])
        self.assertEqual(out, {})

    def test_skips_tuple_target(self) -> None:
        code = "PSU1_VISA, PSU1_CHANNEL = 'X', 2\n"
        out = extract_pinned_constants(code, ["PSU1"])
        # Tuple-targets are operator-unusual; we conservatively skip.
        self.assertEqual(out, {})

    def test_empty_inputs(self) -> None:
        self.assertEqual(extract_pinned_constants("", ["PSU1"]), {})
        self.assertEqual(extract_pinned_constants("PSU1_VISA = 'X'", []), {})

    def test_syntax_error_returns_empty(self) -> None:
        # Unparseable code shouldn't raise; the merge falls through.
        self.assertEqual(
            extract_pinned_constants("PSU1_VISA = 'X' # unclosed string\nbroken syntax (",
                                     ["PSU1"]),
            {},
        )


class MergePinnedConstantsTests(unittest.TestCase):

    def test_replaces_plain_assignment(self) -> None:
        new = "PSU1_VISA = 'ASRL1::INSTR'\n"
        merged, replaced = merge_pinned_constants(
            new, {"PSU1_VISA": "TCPIP0::live::INSTR"},
        )
        self.assertEqual(merged, "PSU1_VISA = 'TCPIP0::live::INSTR'\n")
        self.assertEqual(replaced, ["PSU1_VISA"])

    def test_replaces_type_annotated_assignment(self) -> None:
        # Type-annotated in new code — replacement strips the annotation
        # because codegen emits canonical plain assignments.
        new = "PSU1_VISA: str = 'ASRL1::INSTR'\n"
        merged, replaced = merge_pinned_constants(
            new, {"PSU1_VISA": "TCPIP0::live::INSTR"},
        )
        self.assertEqual(merged, "PSU1_VISA = 'TCPIP0::live::INSTR'\n")
        self.assertEqual(replaced, ["PSU1_VISA"])

    def test_replaces_multi_line_value(self) -> None:
        new = (
            "PSU1_VISA = (\n"
            "    'ASRL1::INSTR'\n"
            ")\n"
        )
        merged, replaced = merge_pinned_constants(
            new, {"PSU1_VISA": "TCPIP0::live::INSTR"},
        )
        # Multi-line value collapses to a single canonical line.
        self.assertEqual(merged, "PSU1_VISA = 'TCPIP0::live::INSTR'\n")
        self.assertEqual(replaced, ["PSU1_VISA"])

    def test_preserves_indentation(self) -> None:
        # Module-level constants normally have no indent; this test
        # documents that any indent (e.g. inside an if-TYPE_CHECKING
        # block, hypothetically) survives.
        new = "    PSU1_VISA = 'ASRL1::INSTR'\n"
        # The AST sees this as an indented stmt; merge preserves it.
        merged, replaced = merge_pinned_constants(
            new, {"PSU1_VISA": "TCPIP0::live::INSTR"},
        )
        # Note: top-level indentation in real .py files is invalid; the
        # AST parse would fail. So the function returns input unchanged.
        # This test documents the no-op.
        self.assertEqual(replaced, [])

    def test_preserves_surrounding_code(self) -> None:
        new = (
            "TEST_NAME = 'EPO'\n"
            "\n"
            "PSU1_VISA = 'ASRL1::INSTR'\n"
            "PSU1_CHANNEL = 1\n"
            "\n"
            "def main():\n"
            "    pass\n"
        )
        merged, replaced = merge_pinned_constants(
            new,
            {"PSU1_VISA": "TCPIP0::live::INSTR", "PSU1_CHANNEL": 2},
        )
        self.assertEqual(merged, (
            "TEST_NAME = 'EPO'\n"
            "\n"
            "PSU1_VISA = 'TCPIP0::live::INSTR'\n"
            "PSU1_CHANNEL = 2\n"
            "\n"
            "def main():\n"
            "    pass\n"
        ))
        self.assertEqual(set(replaced), {"PSU1_VISA", "PSU1_CHANNEL"})

    def test_no_pins_means_no_change(self) -> None:
        new = "PSU1_VISA = 'ASRL1::INSTR'\n"
        merged, replaced = merge_pinned_constants(new, {})
        self.assertEqual(merged, new)
        self.assertEqual(replaced, [])


class PreserveBenchConstantsTests(unittest.TestCase):

    def test_full_round_trip(self) -> None:
        existing = (
            "PSU1_VISA = 'TCPIP0::psu.lab.local::INSTR'\n"
            "PSU1_CHANNEL = 2\n"
        )
        new = (
            "PSU1_VISA = 'ASRL1::INSTR'\n"
            "PSU1_CHANNEL = 1\n"
            "PSU1_REMOTE = True\n"  # newly added in regen — keeps default
        )
        merged, replaced = preserve_bench_constants(
            new, existing, ["PSU1"],
        )
        # Operator pins win; new fields keep their defaults.
        self.assertIn("PSU1_VISA = 'TCPIP0::psu.lab.local::INSTR'", merged)
        self.assertIn("PSU1_CHANNEL = 2", merged)
        self.assertIn("PSU1_REMOTE = True", merged)
        self.assertEqual(set(replaced), {"PSU1_VISA", "PSU1_CHANNEL"})

    def test_no_existing_code_passthrough(self) -> None:
        new = "PSU1_VISA = 'ASRL1::INSTR'\n"
        merged, replaced = preserve_bench_constants(new, "", ["PSU1"])
        self.assertEqual(merged, new)
        self.assertEqual(replaced, [])

    def test_no_equipment_ids_passthrough(self) -> None:
        new = "PSU1_VISA = 'ASRL1::INSTR'\n"
        merged, replaced = preserve_bench_constants(new, new, [])
        self.assertEqual(merged, new)
        self.assertEqual(replaced, [])


class EquipmentIdsFromProcedureTests(unittest.TestCase):

    def test_extracts_in_order(self) -> None:
        proc = {"equipment": [
            {"id": "PSU1", "type": "psu"},
            {"id": "SCOPE", "type": "scope", "channels": [1]},
        ]}
        self.assertEqual(equipment_ids_from_procedure(proc), ["PSU1", "SCOPE"])

    def test_skips_malformed_entries(self) -> None:
        proc = {"equipment": [
            {"id": "PSU1", "type": "psu"},
            "not a dict",
            {"type": "psu"},  # missing id
            {"id": "", "type": "psu"},  # empty id
            {"id": 123, "type": "psu"},  # non-string id
        ]}
        self.assertEqual(equipment_ids_from_procedure(proc), ["PSU1"])

    def test_no_equipment_key(self) -> None:
        self.assertEqual(equipment_ids_from_procedure({}), [])

    def test_non_list_equipment(self) -> None:
        self.assertEqual(
            equipment_ids_from_procedure({"equipment": "wrong type"}), []
        )


if __name__ == "__main__":
    unittest.main()
