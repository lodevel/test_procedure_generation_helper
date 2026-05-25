"""Unit tests for ``workflow_editor.llm.reconstruction.pipeline_ownership``.

This is the single resolver both the prompt emit-list and reconstruction derive
from: the bundle's declared ``section_ownership()`` map (via
``pack_parsers.get_section_ownership``), with an optional per-task override.
Asserting the no-override result equals the wheel map proves the prompt now
derives from the bundle accessor, NOT the static ``DEFAULT_OWNERSHIP``.

Only the in-process (``project_root=None``) path is exercised; the subprocess
path needs a project venv, out of scope here.

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_pipeline_ownership.py --noconftest -q
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers  # noqa: E402
from workflow_editor.llm.reconstruction import pipeline_ownership  # noqa: E402
from workflow_editor.llm.section_ownership import (  # noqa: E402
    CANONICAL_SECTIONS,
    DEFAULT_OWNERSHIP,
    SectionOwnership,
    resolve,
)


class PipelineOwnershipNoOverrideTests(unittest.TestCase):
    """With no override, the resolved ownership matches the wheel's declared
    ``section_ownership()`` map (equipment/steps/expected = LLM-owned)."""

    def setUp(self) -> None:
        self.own = pipeline_ownership(project_root=None)

    def test_returns_section_ownership(self) -> None:
        self.assertIsInstance(self.own, SectionOwnership)

    def test_llm_sections_match_wheel_map(self) -> None:
        expected = resolve(pack_parsers.get_section_ownership(None))
        self.assertEqual(self.own.llm_sections, expected.llm_sections)

    def test_default_llm_sections_are_equipment_steps_expected(self) -> None:
        self.assertEqual(
            self.own.llm_sections, frozenset({"equipment", "steps", "expected"})
        )

    def test_partition_covers_all_canonical_sections(self) -> None:
        self.assertEqual(
            self.own.llm_sections | self.own.parser_sections, CANONICAL_SECTIONS
        )


class PipelineOwnershipTaskOverrideTests(unittest.TestCase):
    """A task override is authoritative: only the named sections stay LLM-owned;
    everything else (including bundle-default LLM sections) becomes parser-owned."""

    def setUp(self) -> None:
        self.own = pipeline_ownership(project_root=None, task_override={"steps"})

    def test_only_steps_is_llm_owned(self) -> None:
        self.assertEqual(self.own.llm_sections, frozenset({"steps"}))

    def test_former_default_llm_sections_now_parser_owned(self) -> None:
        self.assertIn("equipment", self.own.parser_sections)
        self.assertIn("expected", self.own.parser_sections)


class PipelineOwnershipTracksAccessorTests(unittest.TestCase):
    """Derives from get_section_ownership, NOT the static DEFAULT: a non-default
    bundle map is reflected verbatim (would fail if pipeline_ownership hardcoded
    DEFAULT_OWNERSHIP)."""

    def setUp(self) -> None:
        self._orig = pack_parsers.get_section_ownership
        # A map deliberately different from DEFAULT (only steps LLM-owned).
        pack_parsers.get_section_ownership = lambda project_root=None: {
            "test_id": "parser", "description": "parser", "meta": "parser",
            "equipment": "parser", "steps": "llm", "expected": "parser",
        }

    def tearDown(self) -> None:
        pack_parsers.get_section_ownership = self._orig

    def test_tracks_custom_bundle_map(self) -> None:
        own = pipeline_ownership(project_root=None)
        self.assertEqual(own.llm_sections, frozenset({"steps"}))


class PipelineOwnershipSideCarTests(unittest.TestCase):
    """With a project root carrying a CUSTOM bundle side-car, the resolved
    ownership reflects the side-car map (proves the side-car flows all the way
    through ``get_section_ownership`` → ``resolve``)."""

    def test_resolves_custom_sidecar_map(self) -> None:
        custom = {
            "test_id": "parser", "description": "parser", "meta": "parser",
            "equipment": "parser", "steps": "llm", "expected": "parser",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rules_dir = root / "bundle" / "rules"
            rules_dir.mkdir(parents=True)
            (rules_dir / "section_ownership.json").write_text(
                json.dumps(custom), encoding="utf-8"
            )
            own = pipeline_ownership(project_root=root)
        self.assertEqual(own.llm_sections, frozenset({"steps"}))


class PipelineOwnershipFallbackTests(unittest.TestCase):
    """When the parser is unavailable, fall back to DEFAULT_OWNERSHIP rather
    than raising — the prompt-build path must stay non-fatal."""

    def setUp(self) -> None:
        self._orig = pack_parsers.get_section_ownership

        def _raise(project_root=None):
            raise pack_parsers.ParserUnavailable("wheel missing")

        pack_parsers.get_section_ownership = _raise

    def tearDown(self) -> None:
        pack_parsers.get_section_ownership = self._orig

    def test_falls_back_to_default_without_raising(self) -> None:
        own = pipeline_ownership(project_root=None)
        self.assertEqual(
            own.llm_sections, resolve(DEFAULT_OWNERSHIP).llm_sections
        )

    def test_override_still_applies_on_fallback(self) -> None:
        own = pipeline_ownership(project_root=None, task_override={"steps"})
        self.assertEqual(own.llm_sections, frozenset({"steps"}))


if __name__ == "__main__":
    unittest.main()
