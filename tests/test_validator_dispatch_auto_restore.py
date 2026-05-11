"""Phase 7.1 (2026-04-30) — operator-only field auto-restore tests.

Covers the deterministic auto-restore in
``workflow_editor.llm.validator_dispatch._auto_restore_operator_only_fields``
that runs before the artifact handlers in ``validate_response``. The
auto-restore detects when the LLM populated or modified `meta.requirement`
against the baseline and restores the baseline value verbatim, emitting an
informational warning (``META_REQUIREMENT_AUTO_RESTORED``) so the operator
sees the correction.

Test cases:
1. baseline empty + response populated → restored to empty + warning
2. baseline value `"X"` + response value `"Y"` → restored to `"X"` + warning
3. baseline empty + response empty → no-op, no warning
4. baseline value matches response value → no-op, no warning
5. response carries no procedure_text AND no procedure_json → no-op
6. (codex) `current_artifacts["text"]` is `None` → no-op, no raise
7. (codex) multi-slot consistency: text AND json both restored to baseline
"""

from __future__ import annotations

import json
import unittest

from workflow_editor.llm.backend_base import LLMResponse, LLMProposal
from workflow_editor.llm.validator_dispatch import (
    _auto_restore_operator_only_fields,
    _extract_meta_requirement_from_text,
    format_validator_feedback,
    ValidationIssueView,
    ValidationOutcome,
)


def _v2_text(requirement: str | None) -> str:
    """Build a minimal v2-shaped procedure_text.md.

    `requirement=None` → omit the line entirely (canonical "absent" form).
    `requirement=""` → emit `requirement:` (empty slot).
    `requirement="X"` → emit `requirement: X`.
    """
    req_line = "requirement:" if requirement is None else f"requirement: {requirement}" if requirement else "requirement:"
    return (
        "# DEMO\n"
        "Demo procedure.\n"
        "\n"
        "## Meta\n"
        "format_version: 2.0.0\n"
        "board: TEST\n"
        "rules_pack: rules_packager_base@2.0.0\n"
        "labscpi_pack: labscpi@2.0.0\n"
        f"{req_line}\n"
        "\n"
        "## Equipment\n"
        "PSU1 : psu\n"
        "\n"
        "## Steps\n"
        "\n"
        "## Expected\n"
    )


def _make_response(
    *,
    text_value: str | None = None,
    json_value: str | None = None,
) -> LLMResponse:
    """Build an LLMResponse with optional text/json proposals carrying
    the given `meta.requirement` value (None = no proposal at all)."""
    response = LLMResponse(success=True)
    if text_value is not None:
        response.procedure_text = LLMProposal(
            mode="replace",
            content=_v2_text(text_value),
        )
    if json_value is not None:
        proc_json = {
            "format_version": "2.0.0",
            "id": "DEMO",
            "description": "Demo procedure.",
            "meta": {
                "format_version": "2.0.0",
                "board": "TEST",
                "rules_pack": "rules_packager_base@2.0.0",
                "labscpi_pack": "labscpi@2.0.0",
            },
            "equipment": [{"id": "PSU1", "type": "psu"}],
            "lifecycle": {},
            "parameters": {},
            "steps": [],
            "expected": [],
        }
        if json_value:
            proc_json["meta"]["requirement"] = json_value
        response.procedure_json = LLMProposal(
            mode="replace",
            content=json.dumps(proc_json, indent=2),
        )
    return response


class AutoRestoreHappyPathTests(unittest.TestCase):
    """The five core scenarios from the Phase 7.1 plan."""

    def test_baseline_empty_response_invented_restored(self) -> None:
        # Case (a): baseline empty + response populated → restore to empty.
        baseline_text = _v2_text(None)
        response = _make_response(
            text_value="SCOPE configured at 1 V/div"  # invented
        )
        issues = _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        # One warning issue surfaces.
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "META_REQUIREMENT_AUTO_RESTORED")
        self.assertEqual(issues[0].severity, "warning")
        # Response text mutated: requirement line is now empty.
        # Use the same regex-extract that the auto-restore uses, to be
        # independent of which rules_packager_base wheel is installed
        # (the wheel may predate Phase 7 evening's empty-`requirement:`
        # parser relaxation).
        self.assertEqual(
            _extract_meta_requirement_from_text(response.procedure_text.content),
            "",
        )

    def test_empty_baseline_strips_requirement_line(self) -> None:
        """Regression: when baseline is empty/absent and the LLM
        invented a value, auto-restore must DELETE the requirement
        line entirely, not emit ``requirement:`` (no space) or
        ``requirement: `` (empty value).

        Per Canonical_Text_Procedure_v2.md §"Canonical key order":
        "Omit optional keys entirely when absent (do not emit empty
        values)". Two failure modes the fix prevents:

        - ``requirement:`` (no space) → parser GRAM_META_SEPARATOR
          rejects → self-inflicted retry loop where auto-restore
          introduces the error and asks the LLM to fix our output.
        - ``requirement: `` (empty value) → spec-violating, plus
          ``preserve_human_only_fields`` at apply time strips lines
          the original didn't have anyway.
        """
        baseline_text = _v2_text(None)
        response = _make_response(text_value="LLM-invented value")
        _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        content = response.procedure_text.content
        # No requirement line of any shape should remain.
        self.assertNotIn("requirement:", content, (
            f"auto-restore left a requirement line in the proposal; "
            f"per spec it must be stripped entirely. Full text:\n{content}"
        ))
        # No blank line introduced where the stripped line used to be.
        self.assertNotIn("\n\nfncore_pack", content,
                         "stripping requirement left a stray blank line")

    def test_baseline_value_response_modified_restored_to_baseline(self) -> None:
        # Case (b): baseline `"X"` + response `"Y"` → restore to `"X"`.
        baseline_text = _v2_text("IEC-62133 §4.3")
        response = _make_response(
            text_value="IEC-62133 §4.3 — annotated by LLM"
        )
        issues = _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "META_REQUIREMENT_AUTO_RESTORED")
        self.assertEqual(
            _extract_meta_requirement_from_text(response.procedure_text.content),
            "IEC-62133 §4.3",
        )

    def test_both_empty_noop(self) -> None:
        # Case (c): baseline empty + response empty → no change, no warning.
        baseline_text = _v2_text(None)
        response = _make_response(text_value=None)  # absent → empty
        # Need a text proposal so the auto-restore has something to look at.
        # An ABSENT proposal hits a different branch (no proposal).
        # Build a response with an empty-requirement text proposal.
        from workflow_editor.llm.backend_base import LLMProposal
        response.procedure_text = LLMProposal(
            mode="replace", content=_v2_text(None),
        )
        issues = _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        self.assertEqual(issues, [])

    def test_both_match_noop(self) -> None:
        # Case (d): baseline value == response value → no change, no warning.
        baseline_text = _v2_text("IEC-62133 §4.3")
        response = _make_response(text_value="IEC-62133 §4.3")
        issues = _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        self.assertEqual(issues, [])

    def test_no_proposals_noop(self) -> None:
        # Case (e): response has no procedure_text and no procedure_json.
        # Auto-restore has nothing to mutate.
        baseline_text = _v2_text(None)
        response = LLMResponse(success=True)  # no proposals
        issues = _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        self.assertEqual(issues, [])


class AutoRestoreEdgeCaseTests(unittest.TestCase):
    """Codex-flagged edge cases: empty baseline + multi-slot consistency."""

    def test_baseline_text_is_none_noop(self) -> None:
        # Codex: current_artifacts["text"] is type-permitted None.
        # The auto-restore must skip cleanly without raising.
        response = _make_response(text_value="anything invented")
        issues = _auto_restore_operator_only_fields(
            response, {"text": None, "json": "", "code": ""}  # type: ignore[arg-type]
        )
        self.assertEqual(issues, [])
        # And the response was NOT mutated.
        self.assertEqual(
            _extract_meta_requirement_from_text(response.procedure_text.content),
            "anything invented",
        )

    def test_baseline_text_is_empty_string_noop(self) -> None:
        # Codex: GUI normalizes missing content to "".
        response = _make_response(text_value="something invented")
        issues = _auto_restore_operator_only_fields(
            response, {"text": "", "json": "", "code": ""}
        )
        self.assertEqual(issues, [])

    def test_multi_slot_consistency_both_restored(self) -> None:
        # Codex: when LLM proposes BOTH text and JSON with mutated
        # meta.requirement, both must be restored to the same baseline
        # value. After restore, the text-side `requirement:` line is
        # empty AND the json-side `meta.requirement` is absent.
        baseline_text = _v2_text(None)
        response = _make_response(
            text_value="invented in text",
            json_value="invented in json — possibly different",
        )
        issues = _auto_restore_operator_only_fields(
            response, {"text": baseline_text, "json": "", "code": ""}
        )
        # Implementation emits one warning per field (currently just
        # `requirement`), regardless of how many slots changed. Either
        # 1 or 2 issues is acceptable here.
        self.assertGreaterEqual(len(issues), 1)

        # Text side: `requirement:` line is empty.
        self.assertEqual(
            _extract_meta_requirement_from_text(response.procedure_text.content),
            "",
        )
        # JSON side: `meta.requirement` is absent (canonical "not supplied").
        json_proc = json.loads(response.procedure_json.content)
        self.assertNotIn("requirement", json_proc.get("meta", {}))


class FormatValidatorFeedbackFiltersTests(unittest.TestCase):
    """The LLM-facing retry feedback message must filter informational
    issues the LLM cannot act on. Two categories:

    * ``fixable_by == "operator"`` — design-time operator-only fields.
    * ``severity == "warning"`` — the system already handled this
      (e.g. META_REQUIREMENT_AUTO_RESTORED fires AFTER auto-restore
      mutated the proposal). Telling the LLM to "fix" it wastes a
      retry turn and risks re-introducing the very thing auto-restore
      stripped.
    """

    def test_warnings_filtered_from_llm_feedback(self) -> None:
        outcome = ValidationOutcome(
            ok=False,
            skipped=False,
            issues=[
                # Informational — the system fixed this already.
                ValidationIssueView(
                    code="META_REQUIREMENT_AUTO_RESTORED",
                    message="Auto-restored requirement to baseline",
                    severity="warning",
                ),
                # Real error the LLM CAN fix.
                ValidationIssueView(
                    code="GRAM_STEP_INVALID",
                    message="Step verb unrecognized: 'frobnicate'",
                    severity="error",
                ),
            ],
        )
        text = format_validator_feedback(outcome, attempt=1, max_attempts=3)
        # Real error is shown.
        self.assertIn("Step verb unrecognized", text)
        # Auto-restore warning is NOT — saved as one error, not two.
        self.assertNotIn("Auto-restored requirement to baseline", text)
        self.assertIn("1 error(s)", text)
        self.assertNotIn("2 error(s)", text)

    def test_all_warnings_yields_empty_feedback(self) -> None:
        # If every issue is a warning, the LLM has nothing actionable —
        # caller should short-circuit (no retry). format_validator_feedback
        # returns "" defensively in that case.
        outcome = ValidationOutcome(
            ok=False,
            skipped=False,
            issues=[
                ValidationIssueView(
                    code="META_REQUIREMENT_AUTO_RESTORED",
                    message="restored",
                    severity="warning",
                ),
            ],
        )
        self.assertEqual(
            format_validator_feedback(outcome, attempt=1, max_attempts=3),
            "",
        )


if __name__ == "__main__":
    unittest.main()
