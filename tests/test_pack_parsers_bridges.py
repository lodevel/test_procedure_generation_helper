"""Unit tests for the in-process ``get_section_ownership`` / ``reconstruct_text``
bridges in ``procedo.studio.pack_ops.pack_parsers``.

Only the ``project_root=None`` (in-process import) path is exercised here; the
subprocess path needs a project venv, which is out of scope for these tests.

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_pack_parsers_bridges.py --noconftest -q
"""
from __future__ import annotations

import json
import tempfile
import unittest

import pytest
from pathlib import Path

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402
from procedo.studio.pack_ops import engine_session  # noqa: E402
from procedo.studio.pack_ops import engine_replies  # noqa: E402
from procedo.engine_client import _engine_ops as engine_ops  # noqa: E402

from tests import _env

pytestmark = pytest.mark.skipif(
    not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON
)

_skip_no_labscpi = pytest.mark.skipif(
    not _env.labscpi_psu_reconstruct_available(), reason=_env.LABSCPI_SKIP_REASON
)


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
            with self.assertRaises(engine_session.ParserUnavailable):
                pp.get_section_ownership(project_root=root)


class ReconstructTextSuccessTests(unittest.TestCase):
    """A valid non-controller fragment + prior reconstructs cleanly."""

    def setUp(self) -> None:
        self.report = pp.reconstruct_text(FRAGMENT, PRIOR)

    @_skip_no_labscpi
    def test_success(self) -> None:
        self.assertIs(self.report.success, True)
        self.assertIs(self.report.ok, True)

    @_skip_no_labscpi
    def test_no_findings(self) -> None:
        self.assertEqual(self.report.findings, [])
        self.assertEqual(self.report.errors, [])

    def test_text_is_str_starting_with_prior_title(self) -> None:
        self.assertIsInstance(self.report.text, str)
        self.assertTrue(self.report.text.startswith("# PRIOR_TEST"))

    @_skip_no_labscpi
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


class BuildManualRunOpTests(unittest.TestCase):
    """The ``build_manual_run`` engine op on the document it is sent, read back
    through ``ManualRunResult.from_dict`` (what the guided dialog does), with the
    spec crossing the JSON boundary as it does over the daemon pipe.

    Uses base ``dmm`` / operator-read steps so no pack registry is needed.
    """

    def setUp(self) -> None:
        self.procedure_json, _ = pp.parse_text(PROC_MANUAL)

    def _run(self, measurements: dict | None = None) -> "engine_replies.ManualRunResult":
        spec = json.loads(json.dumps({
            "op": "build_manual_run",
            "procedure_json": self.procedure_json,
            "measurements": measurements,
            "controls": None,
        }))
        return engine_replies.ManualRunResult.from_dict(json.loads(json.dumps(engine_ops.run(spec))))

    def test_static_plan_pending_with_expected_joined(self) -> None:
        run = self._run()
        self.assertEqual(run.test_name, "T")
        self.assertIs(run.aborted, False)
        bindings = [s for s in run.steps if s.is_binding]
        self.assertEqual([s.ref for s in bindings], [1, 2])
        # no operator input yet → verdicts pending, expected criterion joined
        self.assertEqual([s.verdict for s in bindings], ["", ""])
        self.assertTrue(bindings[0].expected_text.startswith("{1} <"))

    def test_row_keys_cross_and_verdicts_match(self) -> None:
        run = self._run({"steps[0]": 4.0, "steps[1]": 2.0})
        by_ref = {s.ref: s.verdict for s in run.steps if s.is_binding}
        self.assertEqual(by_ref, {1: "PASS", 2: "FAIL"})

    def test_steps_are_frozen_manual_step_views(self) -> None:
        run = self._run()
        self.assertTrue(all(isinstance(s, engine_replies.ManualStep) for s in run.steps))
        self.assertIsInstance(run, engine_replies.ManualRunResult)
        # attribute access (frozen dataclass), not dict access
        self.assertTrue(run.steps[0].node_path.startswith("steps["))
        with self.assertRaises(Exception):
            run.steps[0].verdict = "PASS"  # frozen

    def test_run_inputs_read_operator_verdict_keys_back_as_readings(self) -> None:
        verdicts = {"1": "PASS", "Vout": "FAIL", "Vrail[3]": "SKIP", "-1": "PASS"}
        self.assertEqual(engine_ops.run_inputs({"operator_verdicts": verdicts})["operator_verdicts"],
                         {1: "PASS", "Vout": "FAIL", "Vrail[3]": "SKIP", "-1": "PASS"})
        self.assertEqual(engine_ops.run_inputs({})["operator_verdicts"], {})

    def test_a_document_that_is_not_an_object_is_an_error_result(self) -> None:
        for doc in (None, [1, 2]):
            out = engine_ops.run({"op": "build_manual_run", "procedure_json": doc})
            self.assertEqual((out["ok"], out["kind"]), (False, "Other"))
            self.assertIn("not a JSON object", out["error"])


class ManualStepSchemaParityTests(unittest.TestCase):
    """``ManualStep`` mirrors the wheel's ``StepDescriptor`` field-for-field, plus
    the Wait and Values-panel fields the build_manual_run op adds. Catches drift: if the wheel
    adds/renames a field, this fails so the GUI view is updated deliberately
    rather than silently dropping it in ``from_dict``."""

    def test_field_names_match_wheel_stepdescriptor(self) -> None:
        from dataclasses import fields
        from rules_packager_base.rules.v2_0_2.parser.manual_run import StepDescriptor
        self.assertEqual(
            {f.name for f in fields(engine_replies.ManualStep)},
            {f.name for f in fields(StepDescriptor)} | {"wait_s", "wait_refused", "value_text", "value_note", "names"},
        )


class ManualRunEngineReadsTests(unittest.TestCase):
    """What the app never computes itself: a Wait's delay, a typed reading, a
    criterion's readings — the engine's, in its ops."""

    def _wait(self, duration, unit="s", op="wait") -> dict:
        from rules_packager_base.rules.v2_0_2.parser.manual_run import StepDescriptor
        return engine_ops._step_payload(StepDescriptor(
            node_path="steps[0]", n=1, op=op, text="", resolved_op={"duration": duration, "duration_unit": unit}))

    def test_a_wait_carries_its_seconds_or_why_they_are_refused(self) -> None:
        self.assertEqual(self._wait(100, "ms")["wait_s"], 0.1)
        self.assertEqual(self._wait("-0.5")["wait_s"], 0)      # below 0 waits none, as test.py
        refused = self._wait("MAX")
        self.assertEqual((refused.get("wait_s"), refused["wait_refused"]), (None, "not a number: 'MAX'"))
        self.assertNotIn("wait_s", self._wait(5, op="note"))
        step = engine_replies.ManualStep.from_dict(self._wait(2))
        self.assertEqual((step.wait_s, step.wait_refused), (2, ""))

    def test_a_typed_reading_is_read_or_refused_by_the_engine(self) -> None:
        def read(kind, text, **op):
            return engine_ops.run({"op": "manual_reading", "text": text,
                                   "step": {"kind": kind, "expected_text": "", "resolved_op": op}})
        self.assertEqual(read("read", "200 mV"), {"ok": True, "value": 0.2, "refusal": ""})
        self.assertEqual(read("text", " 4B ")["value"], " 4B ")
        self.assertEqual(read("bytes", "FB 00", count=2, signed=True)["value"], -1280)
        self.assertIn("isn't a number", read("read", "5 apples")["refusal"])

    def test_a_reading_is_a_run_op(self) -> None:
        self.assertIsNone(engine_ops.refusal("run", {"cmd": "op", "op": "manual_reading"}))

    def test_a_criterions_readings_are_the_engines_entry_refs(self) -> None:
        crit = {"type": "logic", "op": "or", "parts": [
            {"type": "lt", "ref": 1, "limit": 1}, {"type": "range", "ref": {"range": [3, 4]}, "lower": 0, "upper": 5},
            {"type": "lt", "ref": 1, "limit": 2}]}
        out = engine_ops.run({"op": "criterion_refs", "criteria": {"0:a": crit}})
        self.assertEqual(out, {"ok": True, "refs": {"0:a": ["1", "3", "4"]}})


if __name__ == "__main__":
    unittest.main()


def test_an_in_process_pack_load_that_fails_says_why_on_every_call(monkeypatch, tmp_path):
    """The no-project path loads the launcher's bundle once; its failure is said as the engine's ops say it."""
    from procedo.studio.launch_env import BUNDLE_DEFAULTS_PATH

    (tmp_path / "defaults.json").write_text(json.dumps({"pack_dispatch": {
        "namespace_to_module": {"zz": "no_such_pack_parser"}}}), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(tmp_path / "defaults.json"))
    monkeypatch.setattr(pp, "_REGISTRY_LOAD", None)
    for _ in range(2):
        with pytest.raises(engine_session.ParserUnavailable, match="^The bundle's packs could not be loaded: "):
            pp._inproc_import_wheel()
