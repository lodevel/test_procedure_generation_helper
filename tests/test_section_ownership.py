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
    supports_section_ownership,
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
            frozenset({"test_id", "title", "description", "meta"}),
        )

    def test_union_equals_canonical(self) -> None:
        self.assertEqual(
            self.ownership.llm_sections | self.ownership.parser_sections,
            CANONICAL_SECTIONS,
        )

    def test_section_order_is_canonical_six(self) -> None:
        # Backward-compat: the default map's universe == the canonical sections
        # in the declared (insertion) order (now includes the optional
        # front-matter ``title`` section).
        self.assertEqual(
            self.ownership.section_order,
            ("test_id", "title", "description", "meta", "equipment", "steps", "expected"),
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

    def test_partial_map_universe_is_just_its_keys(self) -> None:
        # Universe now comes from the map's keys, NOT the canonical six:
        # a partial map declares a 2-section universe.
        o = resolve({"steps": OWNER_LLM, "expected": OWNER_LLM})
        self.assertEqual(o.llm_sections, frozenset({"steps", "expected"}))
        self.assertEqual(o.parser_sections, frozenset())
        self.assertEqual(o.section_order, ("steps", "expected"))


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

    def test_default_override_steps_backward_compat(self) -> None:
        # Spec backward-compat case: universe = the canonical sections,
        # llm = {steps}, parser = all the others.
        o = resolve(DEFAULT_OWNERSHIP, {"steps"})
        self.assertEqual(o.llm_sections, frozenset({"steps"}))
        self.assertEqual(o.parser_sections, CANONICAL_SECTIONS - {"steps"})
        self.assertEqual(
            o.section_order,
            ("test_id", "title", "description", "meta", "equipment", "steps", "expected"),
        )


# ---------------------------------------------------------------------------
# Custom-universe tests — the universe comes from the map keys, not canonical
# ---------------------------------------------------------------------------


class ResolveCustomUniverseTests(unittest.TestCase):
    """A bundle with a different/renamed ruleset drives the whole partition."""

    CUSTOM = {"intro": OWNER_PARSER, "body": OWNER_LLM, "outro": OWNER_LLM}

    def test_section_order_is_custom_keys(self) -> None:
        o = resolve(self.CUSTOM)
        self.assertEqual(o.section_order, ("intro", "body", "outro"))

    def test_llm_sections_from_custom_map(self) -> None:
        o = resolve(self.CUSTOM)
        self.assertEqual(o.llm_sections, frozenset({"body", "outro"}))

    def test_partition_covers_only_custom_universe(self) -> None:
        o = resolve(self.CUSTOM)
        self.assertEqual(
            o.llm_sections | o.parser_sections, frozenset({"intro", "body", "outro"})
        )
        # NOT the canonical six.
        self.assertEqual(o.parser_sections, frozenset({"intro"}))

    def test_override_partitions_over_custom_universe(self) -> None:
        o = resolve(self.CUSTOM, task_override={"body"})
        self.assertEqual(o.section_order, ("intro", "body", "outro"))
        self.assertEqual(o.llm_sections, frozenset({"body"}))
        self.assertEqual(o.parser_sections, frozenset({"intro", "outro"}))

    def test_override_section_absent_from_universe_dropped(self) -> None:
        # "steps" is canonical but NOT in the custom universe → dropped.
        o = resolve(self.CUSTOM, task_override={"body", "steps"})
        self.assertEqual(o.llm_sections, frozenset({"body"}))
        self.assertNotIn("steps", o.llm_sections)
        self.assertNotIn("steps", o.parser_sections)


class ResolveEdgeCaseTests(unittest.TestCase):
    """Unknown/dirty keys in the map are silently dropped."""

    def test_extra_key_extends_universe(self) -> None:
        # A bundle may declare sections beyond the canonical six; the universe
        # is the map's keys, so an extra key becomes part of the partition.
        m = dict(DEFAULT_OWNERSHIP)
        m["totally_unknown"] = OWNER_LLM  # type: ignore[index]
        o = resolve(m)
        self.assertEqual(
            o.llm_sections | o.parser_sections,
            CANONICAL_SECTIONS | {"totally_unknown"},
        )
        self.assertIn("totally_unknown", o.llm_sections)

    def test_unrecognised_owner_value_defaults_to_parser(self) -> None:
        o = resolve({"steps": "robot"})  # "robot" is not a known owner
        self.assertIn("steps", o.parser_sections)

    def test_empty_map_empty_universe(self) -> None:
        # An empty map declares an empty universe (no sections at all).
        o = resolve({})
        self.assertEqual(o.parser_sections, frozenset())
        self.assertEqual(o.llm_sections, frozenset())
        self.assertEqual(o.section_order, ())

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

    def test_object_shape_entry_extracts_owner_string(self) -> None:
        """Commit B: ``{"meta": {"owner": "parser", "required": true,
        "required_keys": ["format_version"]}}`` returns the flat
        ``{"meta": "parser"}`` map. The extra object-shape keys are
        consumed by the parser side (section_requirements), not here."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), json.dumps({
                "meta": {
                    "owner": "parser",
                    "required": True,
                    "required_keys": ["format_version", "board"],
                },
                "steps": "llm",  # legacy str shape mixes freely
            }))
            result = load_bundle_ownership(bundle)
        self.assertEqual(result, {"meta": "parser", "steps": "llm"})

    def test_object_shape_missing_owner_returns_none(self) -> None:
        """Object-shape entry without an ``owner`` string is malformed —
        the whole file falls back to None (default) rather than silently
        dropping the section."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), json.dumps({
                "meta": {"required": True},  # no "owner"
            }))
            result = load_bundle_ownership(bundle)
        self.assertIsNone(result)

    def test_object_shape_non_string_owner_returns_none(self) -> None:
        """``owner`` must be a string. ``{"owner": 42}`` is malformed."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_bundle(Path(td), json.dumps({
                "meta": {"owner": 42},
            }))
            result = load_bundle_ownership(bundle)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# supports_section_ownership() tests
# ---------------------------------------------------------------------------


class SupportsSectionOwnershipTests(unittest.TestCase):
    """Capability probe: True iff the bundle declares a map."""

    def test_present_map_returns_true(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rules_dir = Path(td) / "rules"
            rules_dir.mkdir()
            (rules_dir / "section_ownership.json").write_text(
                json.dumps({"steps": "llm"}), encoding="utf-8"
            )
            self.assertTrue(supports_section_ownership(Path(td)))

    def test_missing_map_returns_false(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(supports_section_ownership(Path(td)))


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

    def test_empty_object_file_empty_universe(self) -> None:
        """A valid ``{}`` file is respected — empty universe, NOT default."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rules_dir = Path(td) / "rules"
            rules_dir.mkdir()
            (rules_dir / "section_ownership.json").write_text("{}", encoding="utf-8")
            o = for_bundle(Path(td))
        # With an empty map the universe is empty: no sections on either side.
        self.assertEqual(o.llm_sections, frozenset())
        self.assertEqual(o.parser_sections, frozenset())
        self.assertEqual(o.section_order, ())

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
