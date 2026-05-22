"""Unit tests for ``workflow_editor.llm.section_ownership``.

All tests are pure-logic; no Qt or file-system access is required except
for :class:`LoadBundleOwnershipTests` which uses ``tmp_path`` via pytest
(the ``unittest.TestCase`` + ``tmp_path`` combination is supported in
pytest ≥ 3.9 via the ``pytestmark`` / direct fixture injection pattern
documented below).

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_section_ownership.py --noconftest -q
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

# ---------------------------------------------------------------------------
# Import subject under test
# ---------------------------------------------------------------------------

from workflow_editor.llm.section_ownership import (  # noqa: E402
    CANONICAL_SECTIONS,
    DEFAULT_OWNERSHIP,
    OWNER_LLM,
    OWNER_PARSER,
    SectionOwnership,
    for_bundle,
    load_bundle_ownership,
    resolve,
)


# ---------------------------------------------------------------------------
# resolve() tests
# ---------------------------------------------------------------------------


class ResolveDefaultMapTests(unittest.TestCase):
    """resolve() with DEFAULT_OWNERSHIP produces the expected split."""

    def setUp(self) -> None:
        self.ownership = resolve(DEFAULT_OWNERSHIP)

    def test_llm_sections(self) -> None:
        self.assertEqual(
            self.ownership.llm_sections,
            frozenset({"equipment", "steps", "expected"}),
        )

    def test_parser_sections(self) -> None:
        self.assertEqual(
            self.ownership.parser_sections,
            frozenset({"test_id", "description", "meta"}),
        )

    def test_union_equals_canonical(self) -> None:
        self.assertEqual(
            self.ownership.llm_sections | self.ownership.parser_sections,
            CANONICAL_SECTIONS,
        )

    def test_owner_of_llm_section(self) -> None:
        self.assertEqual(self.ownership.owner_of("steps"), OWNER_LLM)

    def test_owner_of_parser_section(self) -> None:
        self.assertEqual(self.ownership.owner_of("meta"), OWNER_PARSER)

    def test_owner_of_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.ownership.owner_of("nonexistent")


class ResolveCustomMapTests(unittest.TestCase):
    """resolve() honours a fully-custom ownership map."""

    def test_all_llm(self) -> None:
        m = {s: OWNER_LLM for s in CANONICAL_SECTIONS}
        o = resolve(m)
        self.assertEqual(o.llm_sections, CANONICAL_SECTIONS)
        self.assertEqual(o.parser_sections, frozenset())

    def test_all_parser(self) -> None:
        m = {s: OWNER_PARSER for s in CANONICAL_SECTIONS}
        o = resolve(m)
        self.assertEqual(o.parser_sections, CANONICAL_SECTIONS)
        self.assertEqual(o.llm_sections, frozenset())

    def test_partial_map_defaults_missing_to_parser(self) -> None:
        # Only "steps" and "expected" in the map as llm; rest unmentioned.
        o = resolve({"steps": OWNER_LLM, "expected": OWNER_LLM})
        self.assertIn("steps", o.llm_sections)
        self.assertIn("expected", o.llm_sections)
        # All other canonical sections fall back to parser.
        for s in CANONICAL_SECTIONS - {"steps", "expected"}:
            self.assertIn(s, o.parser_sections)


class ResolveTaskOverrideTests(unittest.TestCase):
    """task_override is authoritative, ignoring the ownership map."""

    def test_override_wins_over_map(self) -> None:
        # Map says equipment=parser, but override says llm.
        o = resolve(DEFAULT_OWNERSHIP, task_override=["equipment"])
        self.assertIn("equipment", o.llm_sections)
        self.assertIn("steps", o.parser_sections)  # steps not in override → parser

    def test_empty_override_all_parser(self) -> None:
        o = resolve(DEFAULT_OWNERSHIP, task_override=[])
        self.assertEqual(o.llm_sections, frozenset())
        self.assertEqual(o.parser_sections, CANONICAL_SECTIONS)

    def test_full_override_all_llm(self) -> None:
        o = resolve(DEFAULT_OWNERSHIP, task_override=list(CANONICAL_SECTIONS))
        self.assertEqual(o.llm_sections, CANONICAL_SECTIONS)
        self.assertEqual(o.parser_sections, frozenset())

    def test_union_still_canonical_with_override(self) -> None:
        o = resolve(DEFAULT_OWNERSHIP, task_override=["steps", "expected"])
        self.assertEqual(o.llm_sections | o.parser_sections, CANONICAL_SECTIONS)


class ResolveEdgeCaseTests(unittest.TestCase):
    """Unknown/dirty keys in the map are silently dropped."""

    def test_unknown_key_ignored(self) -> None:
        m = dict(DEFAULT_OWNERSHIP)
        m["totally_unknown"] = OWNER_LLM  # type: ignore[index]
        o = resolve(m)
        # All canonical sections still resolved; unknown key doesn't pollute.
        self.assertEqual(o.llm_sections | o.parser_sections, CANONICAL_SECTIONS)

    def test_unrecognised_owner_value_defaults_to_parser(self) -> None:
        o = resolve({"steps": "robot"})  # "robot" is not a known owner
        self.assertIn("steps", o.parser_sections)

    def test_empty_map_all_canonical_to_parser(self) -> None:
        o = resolve({})
        self.assertEqual(o.parser_sections, CANONICAL_SECTIONS)
        self.assertEqual(o.llm_sections, frozenset())

    def test_case_insensitivity_key(self) -> None:
        # "STEPS" should normalise to "steps".
        o = resolve({"STEPS": OWNER_LLM})
        self.assertIn("steps", o.llm_sections)

    def test_case_insensitivity_owner(self) -> None:
        o = resolve({"steps": "LLM"})
        self.assertIn("steps", o.llm_sections)

    def test_whitespace_stripped_from_keys(self) -> None:
        o = resolve({" steps ": OWNER_LLM})
        self.assertIn("steps", o.llm_sections)

    def test_override_unknown_section_ignored(self) -> None:
        o = resolve(DEFAULT_OWNERSHIP, task_override=["steps", "not_a_section"])
        self.assertIn("steps", o.llm_sections)
        # "not_a_section" must not appear anywhere.
        self.assertNotIn("not_a_section", o.llm_sections)
        self.assertNotIn("not_a_section", o.parser_sections)

    def test_override_case_insensitivity(self) -> None:
        o = resolve(DEFAULT_OWNERSHIP, task_override=["STEPS", "Expected"])
        self.assertIn("steps", o.llm_sections)
        self.assertIn("expected", o.llm_sections)


# ---------------------------------------------------------------------------
# SectionOwnership dataclass tests
# ---------------------------------------------------------------------------


class SectionOwnershipFrozenTests(unittest.TestCase):
    """SectionOwnership is frozen (immutable)."""

    def test_is_frozen(self) -> None:
        o = resolve(DEFAULT_OWNERSHIP)
        with self.assertRaises((AttributeError, TypeError)):
            o.llm_sections = frozenset()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# load_bundle_ownership() tests
# ---------------------------------------------------------------------------


class LoadBundleOwnershipTests(unittest.TestCase):
    """IO loader: present file, missing file, malformed JSON."""

    def _make_bundle(self, tmp_path: Path, content: str | None = None) -> Path:
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        if content is not None:
            (rules_dir / "section_ownership.json").write_text(content, encoding="utf-8")
        return tmp_path

    def test_present_valid_file(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), json.dumps({
                "test_id": "parser", "steps": "llm",
            }))
            result = load_bundle_ownership(bundle)
        self.assertEqual(result, {"test_id": "parser", "steps": "llm"})

    def test_missing_file_returns_none(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)  # no rules subdir, no file
            result = load_bundle_ownership(bundle)
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), "{ not valid json }")
            result = load_bundle_ownership(bundle)
        self.assertIsNone(result)

    def test_non_object_json_returns_none(self) -> None:
        """Valid JSON but not an object (array) → None."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), "[1, 2, 3]")
            result = load_bundle_ownership(bundle)
        self.assertIsNone(result)

    def test_non_string_value_returns_none(self) -> None:
        """A JSON object with a non-string value is treated as malformed → None."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), json.dumps({"steps": 42}))
            result = load_bundle_ownership(bundle)
        self.assertIsNone(result)

    def test_empty_object_returns_empty_dict(self) -> None:
        """An explicit ``{}`` file is valid — returns ``{}`` not ``None``."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), "{}")
            result = load_bundle_ownership(bundle)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# for_bundle() tests
# ---------------------------------------------------------------------------


class ForBundleTests(unittest.TestCase):
    """for_bundle() missing-map falls back to DEFAULT_OWNERSHIP."""

    def test_missing_map_uses_default(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            o = for_bundle(Path(td))
        default = resolve(DEFAULT_OWNERSHIP)
        self.assertEqual(o.llm_sections, default.llm_sections)
        self.assertEqual(o.parser_sections, default.parser_sections)

    def test_present_map_overrides_default(self) -> None:
        import tempfile
        all_parser_map = {s: OWNER_PARSER for s in CANONICAL_SECTIONS}
        with tempfile.TemporaryDirectory() as td:
            rules_dir = Path(td) / "rules"
            rules_dir.mkdir()
            (rules_dir / "section_ownership.json").write_text(
                json.dumps(all_parser_map), encoding="utf-8"
            )
            o = for_bundle(Path(td))
        self.assertEqual(o.llm_sections, frozenset())

    def test_task_override_passed_through(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            o = for_bundle(Path(td), task_override=["steps"])
        self.assertIn("steps", o.llm_sections)
        self.assertNotIn("steps", o.parser_sections)

    def test_empty_object_file_all_parser_owned(self) -> None:
        """A valid ``{}`` file is respected — no LLM sections, NOT default."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rules_dir = Path(td) / "rules"
            rules_dir.mkdir()
            (rules_dir / "section_ownership.json").write_text("{}", encoding="utf-8")
            o = for_bundle(Path(td))
        # With an empty map every canonical section falls back to parser.
        self.assertEqual(o.llm_sections, frozenset())
        self.assertEqual(o.parser_sections, CANONICAL_SECTIONS)

    def test_non_string_value_falls_back_to_default(self) -> None:
        """A file with a non-string value is malformed → for_bundle yields DEFAULT."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rules_dir = Path(td) / "rules"
            rules_dir.mkdir()
            (rules_dir / "section_ownership.json").write_text(
                json.dumps({"steps": 42}), encoding="utf-8"
            )
            o = for_bundle(Path(td))
        default = resolve(DEFAULT_OWNERSHIP)
        self.assertEqual(o.llm_sections, default.llm_sections)
        self.assertEqual(o.parser_sections, default.parser_sections)


if __name__ == "__main__":
    unittest.main()
