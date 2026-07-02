"""Unit tests for the in-process ``get_section_ownership`` / ``reconstruct_text``
bridges in ``workflow_editor.llm.pack_parsers``.

Only the ``project_root=None`` (in-process import) path is exercised here; the
subprocess path needs a project venv, which is out of scope for these tests.

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_pack_parsers_bridges.py --noconftest -q
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers as pp  # noqa: E402
from workflow_editor.llm import _pack_parsers_subprocess as worker  # noqa: E402


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
    """In-process section ownership map (``project_root=None`` → wheel default)."""

    def test_returns_expected_six_key_map(self) -> None:
        ownership = pp.get_section_ownership()
        self.assertEqual(
            ownership,
            {
                "test_id": "parser",
                "title": "parser",
                "description": "parser",
                "meta": "parser",
                "equipment": "llm",
                "steps": "llm",
                "expected": "llm",
            },
        )


class GetSectionOwnershipSideCarTests(unittest.TestCase):
    """The bundle side-car ``<project_root>/bundle/rules/section_ownership.json``
    is the authoritative default when present; the wheel is only the fallback."""

    @staticmethod
    def _write_sidecar(project_root: Path, content: str) -> None:
        rules_dir = project_root / "bundle" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "section_ownership.json").write_text(content, encoding="utf-8")

    def test_custom_sidecar_is_read_and_authoritative(self) -> None:
        custom = {
            "test_id": "parser",
            "description": "parser",
            "meta": "parser",
            "equipment": "parser",
            "steps": "llm",
            "expected": "parser",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_sidecar(root, json.dumps(custom))
            result = pp.get_section_ownership(project_root=root)
        self.assertEqual(result, custom)

    def test_empty_sidecar_returns_empty_dict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_sidecar(root, "{}")
            result = pp.get_section_ownership(project_root=root)
        self.assertEqual(result, {})

    def test_no_sidecar_falls_through_to_wheel_path(self) -> None:
        # No side-car + a project root with no venv → the wheel subprocess
        # fallback runs and (lacking a project venv here) raises
        # ParserUnavailable. This proves we fall THROUGH (not return None/{}).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)  # no bundle/rules/section_ownership.json
            with self.assertRaises(pp.ParserUnavailable):
                pp.get_section_ownership(project_root=root)


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


PROC_MANUAL = """# T
Desc.

## Meta
format_version: 2.0.1
board: B
rules_pack: rules_packager_base@2.0.2
## Equipment
DMM_GENERIC : dmm

## Steps
1. Operator: read voltage on TP X with DMM_GENERIC as {1}.
2. Operator: read voltage on TP Y with DMM_GENERIC as {2}.

## Expected
{1} < 5 V
{2} > 3 V
"""


def _write_procedure_file(case: unittest.TestCase, procedure_json: dict) -> Path:
    """Write ``procedure_json`` to a tmp ``procedure.json`` (the bridges are
    path-based; the file outlives the test via addCleanup)."""
    td = tempfile.TemporaryDirectory()
    case.addCleanup(td.cleanup)
    path = Path(td.name) / "procedure.json"
    path.write_text(json.dumps(procedure_json), encoding="utf-8")
    return path


class BuildManualRunInProcTests(unittest.TestCase):
    """In-process guided-manual run plan (``project_root=None`` → wheel default).

    Uses base ``dmm`` / operator-read steps so no pack registry is needed.
    """

    def setUp(self) -> None:
        procedure_json, _ = pp.parse_text(PROC_MANUAL)
        self.procedure_path = _write_procedure_file(self, procedure_json)

    def test_supported(self) -> None:
        self.assertIs(pp.supports_build_manual_run(), True)

    def test_static_plan_pending_with_expected_joined(self) -> None:
        run = pp.build_manual_run(self.procedure_path)
        self.assertEqual(run.test_name, "T")
        self.assertIs(run.aborted, False)
        bindings = [s for s in run.steps if s.is_binding]
        self.assertEqual([s.ref for s in bindings], [1, 2])
        # no operator input yet → verdicts pending, expected criterion joined
        self.assertEqual([s.verdict for s in bindings], ["", ""])
        self.assertTrue(bindings[0].expected_text.startswith("{1} <"))

    def test_live_verdicts_from_measurements(self) -> None:
        # int ref keys must survive into the wheel (in-proc: passed straight through)
        run = pp.build_manual_run(self.procedure_path, {1: 4.0, 2: 2.0})
        by_ref = {s.ref: s.verdict for s in run.steps if s.is_binding}
        self.assertEqual(by_ref, {1: "PASS", 2: "FAIL"})

    def test_unreadable_path_raises_parser_unavailable(self) -> None:
        with self.assertRaises(pp.ParserUnavailable):
            pp.build_manual_run(self.procedure_path.parent / "missing.json")

    def test_steps_are_frozen_manual_step_views(self) -> None:
        run = pp.build_manual_run(self.procedure_path)
        self.assertTrue(all(isinstance(s, pp.ManualStep) for s in run.steps))
        self.assertIsInstance(run, pp.ManualRunResult)
        # attribute access (frozen dataclass), not dict access
        self.assertTrue(run.steps[0].node_path.startswith("steps["))
        with self.assertRaises(Exception):
            run.steps[0].verdict = "PASS"  # frozen


class BuildManualRunSubprocessMarshallingTests(unittest.TestCase):
    """Cross the JSON boundary explicitly (no real subprocess): the worker op +
    ``ManualRunResult.from_dict`` must preserve int measurement-ref keys and
    reconstruct the same verdicts. Guards the int-key serialization fix."""

    def setUp(self) -> None:
        procedure_json, _ = pp.parse_text(PROC_MANUAL)
        self.procedure_path = _write_procedure_file(self, procedure_json)

    def _round_trip(self, measurements: dict) -> "pp.ManualRunResult":
        spec = {
            "op": "build_manual_run",
            "procedure_path": str(self.procedure_path),
            "measurements": measurements,
            "controls": None,
        }
        spec = json.loads(json.dumps(spec))          # int keys -> "1","2" here
        result = json.loads(json.dumps(worker._op_build_manual_run(spec)))
        return pp.ManualRunResult.from_dict(result)

    def test_int_keys_survive_and_verdicts_match(self) -> None:
        run = self._round_trip({1: 4.0, 2: 2.0})
        by_ref = {s.ref: s.verdict for s in run.steps if s.is_binding}
        self.assertEqual(by_ref, {1: "PASS", 2: "FAIL"})
        self.assertTrue(all(isinstance(s, pp.ManualStep) for s in run.steps))

    def test_int_keyed_helper(self) -> None:
        self.assertEqual(worker._int_keyed({"1": 4.0, "2": 2.0}), {1: 4.0, 2: 2.0})
        self.assertEqual(worker._int_keyed(None), {})
        self.assertEqual(worker._int_keyed({"x": 1}), {"x": 1})  # non-int left as-is

    def test_unreadable_path_returns_error_result(self) -> None:
        out = worker._op_build_manual_run({
            "op": "build_manual_run",
            "procedure_path": str(self.procedure_path.parent / "missing.json"),
        })
        self.assertFalse(out["ok"])
        self.assertIn("cannot read procedure file", out["error"])


class ManualStepSchemaParityTests(unittest.TestCase):
    """``ManualStep`` mirrors the wheel's ``StepDescriptor`` field-for-field.
    Catches drift: if the wheel adds/renames a field, this fails so the GUI view
    is updated deliberately rather than silently dropping it in ``from_dict``."""

    def test_field_names_match_wheel_stepdescriptor(self) -> None:
        from dataclasses import fields
        from rules_packager_base.rules.v2_0_2.parser.manual_run import StepDescriptor
        self.assertEqual(
            {f.name for f in fields(pp.ManualStep)},
            {f.name for f in fields(StepDescriptor)},
        )


if __name__ == "__main__":
    unittest.main()
