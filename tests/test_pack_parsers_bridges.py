"""Unit tests for the in-process ``get_section_ownership`` / ``reconstruct_text``
bridges in ``workflow_editor.llm.pack_parsers``.

Only the ``project_root=None`` (in-process import) path is exercised here; the
subprocess path needs a project venv, which is out of scope for these tests.

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_pack_parsers_bridges.py --noconftest -q
"""
from __future__ import annotations

import unittest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Grammar-valid fixtures
# ---------------------------------------------------------------------------

FRAGMENT = """## Equipment
PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]

## Steps
1. Set PSU1 CH1 voltage = 5.0 V.

## Expected
"""

PRIOR = """# PRIOR_TEST
Prior description.

## Meta
format_version: 2.0.1
board: BOARD_A
rules_pack: old@1.0.0
labscpi_pack: old@1.0.0
"""

# Same fragment, but with an invalid body line under ``## Expected``.
FRAGMENT_BAD_EXPECTED = """## Equipment
PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]

## Steps
1. Set PSU1 CH1 voltage = 5.0 V.

## Expected
not valid expected syntax
"""


class GetSectionOwnershipTests(unittest.TestCase):
    """In-process section ownership map."""

    def test_returns_expected_six_key_map(self) -> None:
        ownership = pp.get_section_ownership()
        self.assertEqual(
            ownership,
            {
                "test_id": "parser",
                "description": "parser",
                "meta": "parser",
                "equipment": "llm",
                "steps": "llm",
                "expected": "llm",
            },
        )


class ReconstructTextSuccessTests(unittest.TestCase):
    """A valid non-controller fragment + prior reconstructs cleanly."""

    def setUp(self) -> None:
        self.report = pp.reconstruct_text(FRAGMENT, PRIOR)

    def test_success(self) -> None:
        self.assertIs(self.report.success, True)
        self.assertIs(self.report.ok, True)

    def test_no_findings(self) -> None:
        self.assertEqual(self.report.findings, [])
        self.assertEqual(self.report.errors, [])

    def test_text_is_str_starting_with_prior_title(self) -> None:
        self.assertIsInstance(self.report.text, str)
        self.assertTrue(self.report.text.startswith("# PRIOR_TEST"))

    def test_json_id_matches_prior(self) -> None:
        self.assertEqual(self.report.json["id"], "PRIOR_TEST")


class ReconstructTextFailureTests(unittest.TestCase):
    """An invalid Expected body produces error findings."""

    def setUp(self) -> None:
        # ``expected`` must be LLM-owned for the bad body line to be parsed
        # (otherwise the wheel drops it and fails on the ownership guard,
        # never reaching body validation).
        self.report = pp.reconstruct_text(
            FRAGMENT_BAD_EXPECTED,
            PRIOR,
            owned_sections={"equipment", "steps", "expected"},
        )

    def test_not_successful(self) -> None:
        self.assertIs(self.report.success, False)
        self.assertIs(self.report.ok, False)

    def test_has_findings(self) -> None:
        self.assertGreaterEqual(len(self.report.findings), 1)

    def test_errors_non_empty(self) -> None:
        self.assertGreaterEqual(len(self.report.errors), 1)

    def test_finding_code_present(self) -> None:
        codes = [f.code for f in self.report.findings]
        self.assertTrue(any(c for c in codes), f"no non-empty code in {codes!r}")

    def test_findings_are_normalized_objects(self) -> None:
        for f in self.report.findings:
            self.assertTrue(hasattr(f, "code"))
            self.assertTrue(hasattr(f, "message"))
            self.assertTrue(hasattr(f, "severity"))


if __name__ == "__main__":
    unittest.main()
