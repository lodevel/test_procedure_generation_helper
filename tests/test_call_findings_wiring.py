"""Studio shows the Call steps' project warnings (the wheel's function_catalog) with the
procedure's other findings: Validate Procedure sends the project, the engine's ``validate``
op reads its ``functions/`` without running them, and names the bundle from its bundle.json.
``functions_check`` answers Bench the same check, project-wide."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests import _env
from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.studio.core.validators_registry import (  # noqa: E402
    ValidatorContext,
    ensure_builtins_registered,
    get,
)
from procedo.engine_client import _engine_ops as engine_ops  # noqa: E402
from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402

pytestmark = pytest.mark.skipif(not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON)

ABSENT = "procedo_no_such_package_s9b"
TEXT = (
    "# T\nDesc.\n\n## Meta\nformat_version: 2.0.2\nboard: B\nrules_pack: rules_packager_base@2.13.1\n\n"
    "## Equipment\nDMM1 : dmm\n\n## Steps\n"
    "1. Operator: read DC voltage on TP1 with DMM1 as {1}.\n"
    "2. Call analysis.rise_time({1}, limit=2) as {30} us.\n\n## Expected\n{30} < 5 us\n"
)
MISSING = f"step 2: functions/analysis.py imports {ABSENT}; bundle KC30 2.10.2 does not provide it"


@pytest.fixture
def project(tmp_path) -> Path:
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "bundle.json").write_text(
        json.dumps({"id": "KC30", "version": "2.10.2"})
    )
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "analysis.py").write_text(
        f'import {ABSENT}\n\ndef rise_time(x):\n    """Returns: s"""\n    return 1.0\n'
    )
    return tmp_path


def test_validate_procedure_shows_the_call_warnings_on_their_line(project, engine_in_process):
    ensure_builtins_registered()
    outcome = get("rules_packager_base.validate_procedure")(
        ValidatorContext(
            artifact_text=TEXT,
            artifact_json=None,
            artifact_code=None,
            project_root=project,
            tab_id="t",
        )
    )
    shown = [issue.to_dock_dict() for issue in outcome.issues]
    assert [(d["code"], d["severity"], d["location"], d["message"]) for d in shown] == [
        ("CALL_IMPORT_MISSING", "warning", "line=14", MISSING),
        (
            "CALL_ARGS",
            "warning",
            "line=14",
            "step 2: analysis.rise_time(x): got an unexpected keyword argument 'limit'",
        ),
        (
            "CALL_UNIT_MISMATCH",
            "warning",
            "line=14",
            "step 2: analysis.rise_time declares it returns s, the step writes {30} in us",
        ),
    ]
    assert all(d["suggested_fix"] for d in shown)
    assert engine_in_process[-1]["project"] == str(project)


def test_without_a_project_the_op_checks_no_functions(project):
    out = engine_ops.run({"op": "validate", "text": TEXT})
    assert out["ok"] and not [f for f in out["findings"] if f["code"].startswith("CALL_")]


def _no_catalog(monkeypatch):
    """The project venv's wheel predates function_catalog (bundle 2.10.2): importing it fails."""
    import rules_packager_base.rules.v2_0_2.parser as parser

    name = "rules_packager_base.rules.v2_0_2.parser.function_catalog"
    monkeypatch.delattr(parser, "function_catalog", raising=False)
    monkeypatch.setitem(sys.modules, name, None)


def test_a_procedure_without_a_call_never_reaches_the_catalog(project, monkeypatch):
    from rules_packager_base.rules.v2_0_2.parser import function_catalog

    reached = []
    monkeypatch.setattr(function_catalog, "call_findings", lambda *a, **k: reached.append(a) or [])
    text = TEXT.replace("2. Call analysis.rise_time({1}, limit=2) as {30} us.\n", "").replace(
        "{30} < 5 us", "{1} < 5 V"
    )
    out = engine_ops.run({"op": "validate", "text": text, "project": str(project)})
    assert out == engine_ops.run({"op": "validate", "text": text}) and reached == []


def test_a_failing_catalog_keeps_every_other_finding(project, monkeypatch):
    """A Call's check that cannot run (an older wheel) adds nothing and costs nothing."""
    text = TEXT.replace("{30} < 5 us", "{30} < 5 us\n{9} < 1 V")  # REF_DANGLING, the parser's
    spec = {
        "op": "validate",
        "text": text,
        "json_obj": engine_ops.run({"op": "parse_text", "text": TEXT})["json"],
    }
    _no_catalog(monkeypatch)
    out = engine_ops.run({**spec, "project": str(project)})
    assert out["ok"] and out == engine_ops.run(spec)
    assert "REF_DANGLING" in [f["code"] for f in out["findings"]]


def test_functions_check_lists_each_missing_package_for_bench(project):
    assert engine_ops.run({"op": "functions_check", "project": str(project)}) == {
        "ok": True,
        "problems": [MISSING.removeprefix("step 2: ") + f": rebuild the bundle with {ABSENT}"],
    }
    (project / "bundle" / "bundle.json").unlink()
    assert engine_ops.run({"op": "functions_check", "project": str(project)})["problems"] == [
        f"functions/analysis.py imports {ABSENT}; the bundle does not provide it: rebuild the bundle with {ABSENT}"
    ]


def test_function_signatures_answer_studio_s_call_tooltips(project, engine_in_process, monkeypatch):
    from procedo.studio.pack_ops import call_hints

    assert call_hints.function_signatures(project) == {
        "analysis.rise_time": "rise_time(x) returns s"
    }
    assert engine_in_process[-1] == {"op": "function_signatures", "project": str(project)}
    monkeypatch.setattr(pp, "_engine_call", lambda *a, **k: {"ok": False, "error": "Unknown op"})
    assert call_hints.function_signatures(project) == {}  # a wheel without the op: no tooltip
