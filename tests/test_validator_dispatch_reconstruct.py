"""Validate-path reconstruction tests for
``workflow_editor.llm.validator_dispatch._validate_proposed_text``.

Regression for the dict-vs-attr bug at validator_dispatch.py:478. ``current``
is a ``CurrentArtifacts`` dict (``{"text":..., "json":..., "code":...}``),
NOT an object — so ``getattr(current, "text", None)`` ALWAYS returned None.
That fed ``prior_text=None`` into reconstruction, which yields PLACEHOLDER
identity (``# PLACEHOLDER``) instead of the operator's prior. The validator
then validated a placeholder document while apply used the real editor text,
and name-fidelity received ``original_text=None``.

The fix is ``original_text = current.get("text")``.

These tests drive ``_validate_proposed_text`` directly with a stub
``validate_fn`` that captures the ``text`` (the reconstructed full document)
and ``original_text`` it is handed. Asserting the reconstructed identity is
``REAL_TITLE`` (from the prior) MUST FAIL if line 478 reverts to ``getattr``
(reconstruction would run against None → ``# PLACEHOLDER``).

Runs without PySide6:
    <venv>/python -m pytest tests/test_validator_dispatch_reconstruct.py --noconftest -q
"""
from __future__ import annotations

import unittest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import validator_dispatch  # noqa: E402
from workflow_editor.llm.backend_base import LLMResponse, LLMProposal  # noqa: E402


# A valid BODY fragment (the LLM authors only the body sections).
FRAGMENT = """## Equipment
PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]

## Steps
1. Set PSU1 CH1 voltage = 5.0 V.

## Expected
"""

# The operator's prior — distinctive title + Meta so we can tell whether
# reconstruction ran against it (REAL_TITLE) or against None (PLACEHOLDER).
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
    """Stub for the dispatcher's ``validate_fn``. Records the kwargs the
    handler hands it (notably ``text`` = the reconstructed full document and
    ``original_text`` = the operator's prior) and returns a clean report."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        # Minimal duck-typed ValidationReport: ok=True, no errors.
        class _Report:
            ok = True
            errors: list = []
        return _Report()


class ValidateProposedTextReconstructsAgainstPriorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.response = LLMResponse(success=True)
        self.response.procedure_text = LLMProposal(mode="replace", content=FRAGMENT)
        # The real CurrentArtifacts shape: a dict, not an object.
        self.current = {"text": PRIOR, "json": "", "code": ""}
        self.validate_fn = _CapturingValidateFn()

    def test_reconstructs_against_prior_not_placeholder(self) -> None:
        # project_root=None → in-process reconstruction + name-fidelity gate
        # defaults to enabled (no config.json), original_text threaded through.
        outcome = validator_dispatch._validate_proposed_text(
            self.response, self.current, None, self.validate_fn,
        )
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome.ok, msg=f"unexpected issues: {outcome.issues}")

        # The validator must have been called with the reconstructed full
        # document — identity taken from the PRIOR, not a placeholder.
        self.assertEqual(len(self.validate_fn.calls), 1)
        reconstructed_text = self.validate_fn.calls[0]["text"]
        self.assertTrue(
            reconstructed_text.startswith("# REAL_TITLE"),
            msg=(
                "reconstruction did not use the prior — got:\n"
                f"{reconstructed_text.splitlines()[:1]}\n"
                "If this is '# PLACEHOLDER', line 478 is reading the dict "
                "with getattr (always None) instead of current.get('text')."
            ),
        )
        self.assertIn("requirement: REQ-42", reconstructed_text)
        self.assertIn("board: BOARD_A", reconstructed_text)

    def test_original_text_threaded_for_name_fidelity(self) -> None:
        # Name-fidelity needs the operator's pre-LLM text as original_text.
        # The bug fed it None; the fix passes the prior through.
        validator_dispatch._validate_proposed_text(
            self.response, self.current, None, self.validate_fn,
        )
        self.assertEqual(len(self.validate_fn.calls), 1)
        self.assertEqual(self.validate_fn.calls[0]["original_text"], PRIOR)


if __name__ == "__main__":
    unittest.main()
