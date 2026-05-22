"""LLM-feedback line-coordinate translation tests for
``workflow_editor.llm.validator_dispatch``.

The pipeline reconstructs a full procedure (operator-owned title/description/
``## Meta`` prepended) BEFORE validating, so validator findings carry line
numbers in reconstructed-document coordinates. But the LLM only authored the
body fragment (``## Equipment`` / ``## Steps`` / ``## Expected``). The text
handler (``_validate_proposed_text``) translates each finding's ``.line`` back
to FRAGMENT coordinates in place — the ONE place that knows the coordinate
space — while leaving ``.location`` (full-doc, operator-facing) untouched.
``format_validator_feedback`` then renders the already-translated ``.line``
directly; there is no outcome-level offset and json-handler findings (their own
coords) are never shifted.

These tests cover:
  - ``_fragment_line_shift`` anchoring (with/without a leading blank line, the
    anchor-absent fallback, a CRLF fragment, and a duplicate-anchor identity).
  - ``format_validator_feedback`` rendering an already-translated line, the
    pointer-only (line=None) passthrough, and an untranslated (json-style) line.
  - ``_validate_proposed_text`` translating issue ``.line`` to fragment coords,
    and the absence of any ``line_offset`` attribute on ``ValidationOutcome``.

Runs without PySide6:
    <venv>/python -m pytest tests/test_validator_feedback_fragment_lines.py --noconftest -q
"""
from __future__ import annotations

import unittest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import validator_dispatch  # noqa: E402
from workflow_editor.llm.validator_dispatch import (  # noqa: E402
    ValidationIssueView,
    ValidationOutcome,
    _fragment_line_shift,
    format_validator_feedback,
)
from workflow_editor.llm.backend_base import LLMResponse, LLMProposal  # noqa: E402


# A reconstructed full document whose body (anchor `## Equipment`) starts at
# line 12 (1-based), i.e. index 11 (0-based) — an 11-line identity prefix.
RECON_TEXT = "\n".join([
    "# REAL_TITLE",          # 1
    "Real description.",     # 2
    "",                      # 3
    "## Meta",               # 4
    "format_version: 2.0.1",  # 5
    "board: BOARD_A",        # 6
    "requirement: REQ-42",   # 7
    "rules_pack: old@1.0.0",  # 8
    "labscpi_pack: old@1.0.0",  # 9
    "",                      # 10
    "",                      # 11
    "## Equipment",          # 12  <- anchor
    "PSU1 : psu",            # 13
    "",                      # 14
    "## Steps",              # 15
    "1. Set PSU1 CH1 voltage = 5.0 V.",  # 16
    "",                      # 17
    "## Expected",           # 18
])

# The body fragment the LLM authored — `## Equipment` is line 1.
FRAGMENT_TEXT = "\n".join([
    "## Equipment",          # 1  <- anchor
    "PSU1 : psu",            # 2
    "",                      # 3
    "## Steps",              # 4
    "1. Set PSU1 CH1 voltage = 5.0 V.",  # 5
    "",                      # 6
    "## Expected",           # 7
])


class FragmentLineShiftTests(unittest.TestCase):
    def test_shift_is_identity_prefix_length(self) -> None:
        # recon anchor at index 11, fragment anchor at index 0 → shift 11.
        self.assertEqual(_fragment_line_shift(RECON_TEXT, FRAGMENT_TEXT), 11)

    def test_leading_blank_in_fragment_adjusts_shift(self) -> None:
        # A leading blank line pushes the fragment anchor to index 1, so the
        # shift (recon_idx - frag_idx) drops by one: 11 - 1 = 10.
        frag_with_blank = "\n" + FRAGMENT_TEXT
        self.assertEqual(_fragment_line_shift(RECON_TEXT, frag_with_blank), 10)

    def test_anchor_absent_returns_zero(self) -> None:
        # No shared anchor line → no shift assumed.
        self.assertEqual(
            _fragment_line_shift(RECON_TEXT, "## SomethingElse\nbody\n"), 0
        )

    def test_all_blank_fragment_returns_zero(self) -> None:
        # No non-blank fragment line → anchor is None → 0.
        self.assertEqual(_fragment_line_shift(RECON_TEXT, "\n\n  \n"), 0)

    def test_crlf_fragment_still_matches_lf_recon(self) -> None:
        # Defect #1: the wheel LF-normalizes recon.text, but the raw fragment may
        # be CRLF. ``splitlines()`` drops terminators on both sides so the CRLF
        # anchor (`"## Equipment\r"`) still matches the LF recon line — shift is
        # the real 11, not a silent 0.
        crlf_fragment = FRAGMENT_TEXT.replace("\n", "\r\n")
        self.assertIn("## Equipment\r\n", crlf_fragment)  # sanity: it is CRLF
        self.assertEqual(_fragment_line_shift(RECON_TEXT, crlf_fragment), 11)

    def test_duplicate_anchor_in_identity_uses_body_occurrence(self) -> None:
        # Defect #3: the anchor line also appears in the prepended identity. The
        # body occurrence is the *later* one, so the shift must anchor on it.
        recon_with_dup = "\n".join([
            "# REAL_TITLE",       # 1
            "## Equipment",       # 2  <- decoy occurrence inside identity
            "(notes)",            # 3
            "",                   # 4
            "## Meta",            # 5
            "format_version: 2.0.1",  # 6
            "",                   # 7
            "## Equipment",       # 8  <- real body occurrence (index 7)
            "PSU1 : psu",         # 9
            "## Steps",           # 10
        ])
        # Body anchor at index 7, fragment anchor at index 0 → shift 7. The
        # earliest-occurrence bug would have returned 1 (the decoy).
        self.assertEqual(_fragment_line_shift(recon_with_dup, FRAGMENT_TEXT), 7)


class FormatValidatorFeedbackTranslationTests(unittest.TestCase):
    def test_translated_line_rendered_directly(self) -> None:
        # The text handler already translated the line to fragment coords (9);
        # the renderer must emit it verbatim, no further offset.
        outcome = ValidationOutcome(
            ok=False,
            issues=[ValidationIssueView(
                code="GRAM_LINE_UNRECOGNIZED",
                message="Unrecognized line in Steps: '3. @IF ...'",
                severity="error",
                location="line=20",  # full-doc, operator-facing — untouched
                line=9,              # already fragment coords
                fixable_by="either",
            )],
        )
        feedback = format_validator_feedback(outcome, attempt=1, max_attempts=3)
        self.assertIn("at line 9", feedback)
        self.assertNotIn("at line 20", feedback)
        self.assertIn("Unrecognized line in Steps", feedback)

    def test_pointer_only_issue_passthrough(self) -> None:
        # Schema-style finding: no structured line, only a JSON pointer. The
        # pointer is coordinate-free so it passes through unchanged.
        outcome = ValidationOutcome(
            ok=False,
            issues=[ValidationIssueView(
                code="JS_SCHEMA_VIOLATION",
                message="steps[3] missing required key 'op'",
                severity="error",
                location="pointer=/steps/3",
                line=None,
                fixable_by="either",
            )],
        )
        feedback = format_validator_feedback(outcome, attempt=1, max_attempts=3)
        self.assertIn("pointer=/steps/3", feedback)
        # No structured line → no "at line N" numeric form.
        self.assertNotIn("at line", feedback)

    def test_json_style_issue_renders_its_own_line(self) -> None:
        # Multi-slot guard: a json-handler-style issue carries its OWN line (its
        # coords, offset 0). Translation is per-issue at the text handler, so json
        # findings are never shifted — the renderer must emit their line as-is.
        outcome = ValidationOutcome(
            ok=False,
            issues=[ValidationIssueView(
                code="JS_TOPOLOGY",
                message="dangling reference",
                severity="error",
                location="line=4",
                line=4,  # untranslated json-side line
                fixable_by="either",
            )],
        )
        feedback = format_validator_feedback(outcome, attempt=1, max_attempts=3)
        self.assertIn("at line 4", feedback)


# --------------------------------------------------------------------------- #
# Integration: drive _validate_proposed_text and assert per-issue translation. #
# Mirrors tests/test_validator_dispatch_reconstruct.py.                        #
# --------------------------------------------------------------------------- #

# A valid BODY fragment (the LLM authors only the body sections).
FRAGMENT = """## Equipment
PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]

## Steps
1. Set PSU1 CH1 voltage = 5.0 V.

## Expected
"""

# The operator's prior — distinctive title + Meta so reconstruction prepends a
# known-length identity block ahead of the body fragment.
PRIOR = """# REAL_TITLE
Real description.

## Meta
format_version: 2.0.1
board: BOARD_A
requirement: REQ-42
rules_pack: old@1.0.0
labscpi_pack: old@1.0.0
"""


class _CapturingValidateFn:
    """Stub for the dispatcher's ``validate_fn``. Records the reconstructed full
    document it is handed and returns a report carrying one body finding whose
    line is in reconstructed-doc coordinates (so we can assert translation)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.body_anchor = "## Equipment"

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        recon_text = kwargs["text"]
        recon_lines = recon_text.split("\n")
        # Emit a finding pinned to the body anchor's reconstructed-doc line.
        body_line_0based = recon_lines.index(self.body_anchor)
        recon_line_1based = body_line_0based + 1

        class _Issue:
            code = "GRAM_LINE_UNRECOGNIZED"
            message = "anomaly on the Equipment header"
            severity = "error"
            fix_hint = ""
            fixable_by = "either"
            location = {"line": recon_line_1based}

        class _Report:
            ok = False
            errors = [_Issue()]

        return _Report()


class ValidateProposedTextTranslationIntegrationTests(unittest.TestCase):
    def test_issue_line_translated_to_fragment_coords(self) -> None:
        response = LLMResponse(success=True)
        response.procedure_text = LLMProposal(mode="replace", content=FRAGMENT)
        current = {"text": PRIOR, "json": "", "code": ""}
        validate_fn = _CapturingValidateFn()

        outcome = validator_dispatch._validate_proposed_text(
            response, current, None, validate_fn,
        )
        self.assertIsNotNone(outcome)
        self.assertEqual(len(validate_fn.calls), 1)

        # The body anchor sits at line 1 of the fragment, so after translation the
        # finding's .line must be 1 — regardless of the identity-prefix length.
        recon_text = validate_fn.calls[0]["text"]
        frag_lines = FRAGMENT.split("\n")
        anchor = next(l for l in frag_lines if l.strip())
        expected_frag_line = frag_lines.index(anchor) + 1  # 1-based fragment line

        self.assertEqual(len(outcome.issues), 1)
        self.assertEqual(outcome.issues[0].line, expected_frag_line)
        self.assertEqual(expected_frag_line, 1)

        # The operator-facing full-doc location is left untouched (still recon).
        self.assertIn("line=", outcome.issues[0].location)

    def test_outcome_has_no_line_offset_attribute(self) -> None:
        # The outcome-level offset was removed; per-issue translation replaces it.
        self.assertFalse(hasattr(ValidationOutcome(), "line_offset"))


if __name__ == "__main__":
    unittest.main()
