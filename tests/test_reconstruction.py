"""Unit tests for ``workflow_editor.llm.reconstruction.reconstruct_for_pipeline``.

This is the single seam where ownership resolution meets the ``pack_parsers``
reconstruct bridge. Only the in-process (``project_root=None``) path is
exercised; the subprocess path needs a project venv, out of scope here.

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_reconstruction.py --noconftest -q
"""
from __future__ import annotations

import unittest

import pytest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers  # noqa: E402
from workflow_editor.llm.reconstruction import (  # noqa: E402
    reconstruct_for_pipeline,
    reconstructed_or_error,
)

from tests import _env

pytestmark = pytest.mark.skipif(
    not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON
)  # noqa: E402

_skip_no_labscpi = pytest.mark.skipif(
    not _env.labscpi_psu_reconstruct_available(), reason=_env.LABSCPI_SKIP_REASON
)


# ---------------------------------------------------------------------------
# Grammar-valid fixtures (mirror tests/test_pack_parsers_bridges.py)
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
requirement: REQ-42
rules_pack: old@1.0.0
labscpi_pack: old@1.0.0
"""


class ReconstructAgainstPriorTests(unittest.TestCase):
    """A valid body fragment + prior with ``## Meta`` reconstructs cleanly,
    taking operator-owned identity from the prior."""

    def setUp(self) -> None:
        self.report = reconstruct_for_pipeline(FRAGMENT, PRIOR, project_root=None)

    @_skip_no_labscpi
    def test_success(self) -> None:
        self.assertIs(self.report.success, True)
        self.assertIs(self.report.ok, True)

    def test_text_starts_with_prior_title(self) -> None:
        self.assertIsInstance(self.report.text, str)
        self.assertTrue(self.report.text.startswith("# PRIOR_TEST"))

    @_skip_no_labscpi
    def test_json_id_matches_prior(self) -> None:
        self.assertEqual(self.report.json["id"], "PRIOR_TEST")

    def test_prior_requirement_preserved(self) -> None:
        self.assertIn("requirement: REQ-42", self.report.text)

    def test_prior_board_preserved(self) -> None:
        self.assertIn("board: BOARD_A", self.report.text)

    @_skip_no_labscpi
    def test_pack_version_lines_present(self) -> None:
        # The parser fills Meta wholesale: pack-version lines come from the
        # authoritative pack, NOT from the prior's stale ``old@1.0.0``.
        self.assertIn("rules_pack:", self.report.text)
        self.assertIn("labscpi_pack:", self.report.text)
        self.assertNotIn("old@1.0.0", self.report.text)


class ReconstructCreateTests(unittest.TestCase):
    """A None prior (fresh create) yields placeholder identity — reconstruction
    ALWAYS runs; there is no empty-prior pass-through."""

    def setUp(self) -> None:
        self.report = reconstruct_for_pipeline(FRAGMENT, None, project_root=None)

    @_skip_no_labscpi
    def test_success(self) -> None:
        self.assertIs(self.report.success, True)

    @_skip_no_labscpi
    def test_json_id_is_placeholder(self) -> None:
        self.assertEqual(self.report.json["id"], "PLACEHOLDER")

    def test_text_starts_with_placeholder_title(self) -> None:
        self.assertTrue(self.report.text.startswith("# PLACEHOLDER"))


class ReconstructTaskOverrideTests(unittest.TestCase):
    """A ``task_override`` makes the named set the authoritative LLM-owned
    sections; everything else becomes parser-owned. With ``{"steps"}`` the
    fragment's ``## Equipment`` is now parser-owned, so the wheel rejects it
    with ``RECON_UNSUPPORTED_OWNER`` — proving the override threads through
    ``resolve`` into ``reconstruct_text``."""

    def setUp(self) -> None:
        self.report = reconstruct_for_pipeline(
            FRAGMENT, PRIOR, task_override={"steps"}, project_root=None
        )

    def test_not_successful(self) -> None:
        self.assertIs(self.report.success, False)
        self.assertIs(self.report.ok, False)

    def test_unsupported_owner_finding_present(self) -> None:
        codes = [f.code for f in self.report.findings]
        self.assertIn("RECON_UNSUPPORTED_OWNER", codes)


class ReconstructOverrideEquivalenceTests(unittest.TestCase):
    """``task_override=None`` (wheel default) and an explicit override that
    names the same default LLM-owned set produce identical reconstruction for
    a normal body fragment."""

    @_skip_no_labscpi
    def test_none_matches_explicit_default(self) -> None:
        default = reconstruct_for_pipeline(
            FRAGMENT, PRIOR, task_override=None, project_root=None
        )
        explicit = reconstruct_for_pipeline(
            FRAGMENT,
            PRIOR,
            task_override={"equipment", "steps", "expected"},
            project_root=None,
        )
        self.assertIs(default.success, True)
        self.assertIs(explicit.success, True)
        self.assertEqual(default.text, explicit.text)


class ReconstructNoOverridePassesResolvedSetTests(unittest.TestCase):
    """The ``task_override is None`` fast-path is gone: reconstruction now always
    resolves ownership (side-car / wheel default) and threads an explicit
    ``owned_sections`` set into ``reconstruct_text`` — never ``None``."""

    def setUp(self) -> None:
        self._orig = pack_parsers.reconstruct_text
        self.calls: list[dict] = []

        def _spy(fragment, prior=None, owned_sections=None, project_root=None):
            self.calls.append({
                "owned_sections": owned_sections,
                "project_root": project_root,
            })
            return self._orig(
                fragment, prior,
                owned_sections=owned_sections,
                project_root=project_root,
            )

        pack_parsers.reconstruct_text = _spy

    def tearDown(self) -> None:
        pack_parsers.reconstruct_text = self._orig

    def test_no_override_calls_with_a_set_not_none(self) -> None:
        reconstruct_for_pipeline(FRAGMENT, PRIOR, project_root=None)
        self.assertEqual(len(self.calls), 1)
        owned = self.calls[0]["owned_sections"]
        self.assertIsInstance(owned, set)
        self.assertIsNotNone(owned)
        # The DEFAULT bundle map → equipment/steps/expected are LLM-owned.
        self.assertEqual(owned, {"equipment", "steps", "expected"})


class ReconstructedOrErrorTests(unittest.TestCase):
    """The apply-path guard helper. Returns ``(strict_text, best_effort_text,
    error)``: ``(text, text, None)`` on success; ``(None, half_built_text,
    findings)`` on an invalid proposal — strict_text is None so it is never
    auto-applied, but the half-built doc is surfaced so the apply path can show
    a reviewable diff (with a warning banner) instead of dropping it; and
    ``(None, None, reason)`` when there is genuinely nothing to show (e.g. an
    unguarded ``ParserUnavailable`` raised into a Qt slot)."""

    @_skip_no_labscpi
    def test_success_returns_text_and_none(self) -> None:
        strict, best, err = reconstructed_or_error(FRAGMENT, PRIOR, project_root=None)
        self.assertIsNone(err)
        self.assertIsInstance(strict, str)
        self.assertTrue(strict.startswith("# PRIOR_TEST"))
        self.assertEqual(best, strict)  # best_effort == strict on success

    def test_failure_surfaces_best_effort_and_message(self) -> None:
        # task_override={"steps"} makes ## Equipment parser-owned; the
        # fragment still carries it, so reconstruction fails. strict_text is
        # None (never auto-applied), but the half-built best_effort_text IS
        # surfaced so the operator can review/fix it, and the findings land in
        # the error string.
        strict, best, err = reconstructed_or_error(
            FRAGMENT, PRIOR, task_override={"steps"}, project_root=None
        )
        self.assertIsNone(strict)
        self.assertIsNotNone(best)  # half-built doc surfaced (the fix)
        self.assertIsInstance(err, str)
        self.assertTrue(err)  # non-empty
        self.assertIn("RECON_UNSUPPORTED_OWNER", err)


if __name__ == "__main__":
    unittest.main()
