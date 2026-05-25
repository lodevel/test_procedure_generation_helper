"""Unit tests for the procedure_text section emit-list in
``workflow_editor.llm.output_contracts``.

Pure-logic; runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_output_contracts_section_emit.py --noconftest -q
"""
from __future__ import annotations

import unittest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from workflow_editor.llm.output_contracts import (  # noqa: E402
    JSON_CODE_CONTRACT,
    get_contract_for_tab,
    render_section_emit_list,
)
from workflow_editor.llm.section_ownership import (  # noqa: E402
    CANONICAL_SECTIONS,
    CANONICAL_SECTION_ORDER,
    DEFAULT_OWNERSHIP,
    SECTION_HEADINGS,
    resolve,
)


# ---------------------------------------------------------------------------
# render_section_emit_list()
# ---------------------------------------------------------------------------


class RenderSectionEmitListDefaultTests(unittest.TestCase):
    """Default ownership: equipment/steps/expected owned, rest preserved."""

    def setUp(self) -> None:
        self.rendered = render_section_emit_list(resolve(DEFAULT_OWNERSHIP))

    def test_contains_owned_headings(self) -> None:
        self.assertIn("## Equipment", self.rendered)
        self.assertIn("## Steps", self.rendered)
        self.assertIn("## Expected", self.rendered)

    def test_owned_order_equipment_steps_expected(self) -> None:
        eq = self.rendered.index("## Equipment")
        st = self.rendered.index("## Steps")
        ex = self.rendered.index("## Expected")
        self.assertLess(eq, st)
        self.assertLess(st, ex)

    def test_do_not_emit_references_parser_sections(self) -> None:
        # The parser-owned identity sections must be in the do-not-emit block.
        self.assertIn("# <TEST_ID> (title line)", self.rendered)
        self.assertIn("description paragraph (under the title)", self.rendered)
        self.assertIn("## Meta", self.rendered)

    def test_has_author_only_lead_in(self) -> None:
        self.assertIn("Author ONLY these sections", self.rendered)


# ---------------------------------------------------------------------------
# get_contract_for_tab() — text-producing tabs get the emit-list
# ---------------------------------------------------------------------------


class GetContractEmitListTests(unittest.TestCase):
    """Text tabs append the emit-list; json_code stays untouched."""

    def test_text_json_has_emit_list(self) -> None:
        self.assertIn(
            "Author ONLY these sections", get_contract_for_tab("text_json")
        )

    def test_text_only_has_emit_list(self) -> None:
        self.assertIn(
            "Author ONLY these sections", get_contract_for_tab("text_only")
        )

    def test_json_code_has_no_emit_list(self) -> None:
        contract = get_contract_for_tab("json_code")
        self.assertNotIn("Author ONLY these sections", contract)

    def test_json_code_equals_untouched_contract(self) -> None:
        self.assertEqual(get_contract_for_tab("json_code"), JSON_CODE_CONTRACT)

    def test_unknown_tab_raises(self) -> None:
        with self.assertRaises(ValueError):
            get_contract_for_tab("bogus_tab")


# ---------------------------------------------------------------------------
# get_contract_for_tab() — per-call ownership override threads through
# ---------------------------------------------------------------------------


class GetContractOverrideTests(unittest.TestCase):
    """A resolved ownership override changes which sections are authored."""

    def setUp(self) -> None:
        self.contract = get_contract_for_tab(
            "text_json", ownership=resolve(DEFAULT_OWNERSHIP, {"steps"})
        )
        # Split into the authored block vs the do-not-emit block.
        marker = "Do NOT emit these"
        idx = self.contract.index(marker)
        self.author_block = self.contract[:idx]
        self.do_not_emit_block = self.contract[idx:]

    def test_author_block_has_only_steps(self) -> None:
        self.assertIn("## Steps", self.author_block)
        self.assertNotIn("## Equipment", self.author_block)
        self.assertNotIn("## Expected", self.author_block)

    def test_equipment_and_expected_now_in_do_not_emit(self) -> None:
        self.assertIn("## Equipment", self.do_not_emit_block)
        self.assertIn("## Expected", self.do_not_emit_block)


class CanonicalSectionConstantsTests(unittest.TestCase):
    """Guard against drift between the three section constants."""

    def test_order_set_equals_canonical_sections(self) -> None:
        self.assertEqual(set(CANONICAL_SECTION_ORDER), set(CANONICAL_SECTIONS))

    def test_order_has_no_duplicates(self) -> None:
        self.assertEqual(len(CANONICAL_SECTION_ORDER), len(set(CANONICAL_SECTION_ORDER)))

    def test_headings_keys_equal_canonical_sections(self) -> None:
        self.assertEqual(set(SECTION_HEADINGS), set(CANONICAL_SECTIONS))


class RenderSectionEmitListEmptyOwnedTests(unittest.TestCase):
    """All sections operator-owned (reachable once per-task overrides land)."""

    def setUp(self) -> None:
        # task_override=set() is not None → authoritative empty LLM-owned set.
        self.rendered = render_section_emit_list(resolve(DEFAULT_OWNERSHIP, set()))

    def test_no_author_only_lead_in(self) -> None:
        self.assertNotIn("Author ONLY these sections", self.rendered)

    def test_states_fully_operator_owned(self) -> None:
        self.assertIn("All sections are operator-owned", self.rendered)

    def test_no_start_output_line(self) -> None:
        self.assertNotIn("Start your output", self.rendered)

    def test_parser_sections_listed(self) -> None:
        self.assertIn("## Equipment", self.rendered)
        self.assertIn("## Meta", self.rendered)


if __name__ == "__main__":
    unittest.main()
