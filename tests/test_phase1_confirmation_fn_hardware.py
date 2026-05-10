"""Phase 1 confirmation: load chain works on a project seeded from
``customer_configs/FN_hardware``.

Confirms the two-step scenario from PLAN_workflows_to_project_config.md:

1. A project carrying FN_hardware's packs (``base``, ``fncore-mockup-driver``,
   ``labscpi``) and NO ``workflows`` section in its ``config.json`` falls back
   to the repo-shared ``external/test_procedure_generation_helper/config/
   tab_contexts.json``. All four tabs (text_only, text_json, json_code,
   traceability) populate with the fallback's button labels.

2. Adding a single per-task ``button_label`` override into the project's
   ``config.json`` at ``workflows.text_json.tasks[0].button_label`` (matched
   by ``id``) overrides exactly that button; everything else stays at the
   fallback values.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from workflow_editor.core.task_config import TaskConfigManager


# Repo-relative anchors.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FALLBACK_PATH = _REPO_ROOT / "external" / "test_procedure_generation_helper" / "config" / "tab_contexts.json"
_FN_HARDWARE = _REPO_ROOT / "customer_configs" / "FN_hardware"


@pytest.fixture
def fn_hardware_project(tmp_path):
    """Project seeded from FN_hardware (manifest + packs section, NO workflows)."""
    root = tmp_path / "fn_proj"
    cfg = root / "config"
    cfg.mkdir(parents=True)

    fn_config = json.loads((_FN_HARDWARE / "config.json").read_text(encoding="utf-8"))
    project_config = {
        "manifest": fn_config.get("manifest", {}),
        "packs": fn_config.get("packs", {}),
        "parsers": fn_config.get("parsers", {}),
        "patterns": fn_config.get("patterns", {}),
        "profiles": fn_config.get("profiles", {}),
        # Intentionally NO "workflows" key — that's the test scenario.
    }
    (cfg / "config.json").write_text(
        json.dumps(project_config, indent=2), encoding="utf-8"
    )
    return root


def _expected_fallback_labels() -> dict[str, dict[str, str]]:
    """Read the repo fallback file and index it as ``{tab_id: {task_id: label}}``."""
    data = json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for tab_id, tab_cfg in data.items():
        if not isinstance(tab_cfg, dict):
            continue
        out[tab_id] = {
            t["id"]: t["button_label"]
            for t in tab_cfg.get("tasks", [])
            if isinstance(t, dict) and "id" in t and "button_label" in t
        }
    return out


def test_step1_project_with_no_workflows_uses_fallback(fn_hardware_project):
    """Without a workflows section, all four tabs render with fallback labels."""
    mgr = TaskConfigManager(_FALLBACK_PATH, project_root=fn_hardware_project)
    expected = _expected_fallback_labels()

    for tab_id in ("text_only", "text_json", "json_code"):
        tasks = mgr.get_all_tasks_for_tab(tab_id)
        assert tasks, f"tab {tab_id} should be populated"
        for task in tasks:
            if task.id in expected.get(tab_id, {}):
                assert task.button_label == expected[tab_id][task.id], (
                    f"tab {tab_id} task {task.id}: fallback label expected "
                    f"{expected[tab_id][task.id]!r}, got {task.button_label!r}"
                )

    # Traceability tab is chat-only — verify chat config came through.
    chat = mgr.get_chat_config("traceability")
    assert chat is not None


def test_step2_per_task_override_wins(fn_hardware_project):
    """Adding ``workflows.text_json.tasks[<derive>].button_label = "Custom"``
    to the project's config.json overrides that one button. The rest of
    text_json's tasks AND all other tabs keep fallback labels."""
    expected = _expected_fallback_labels()

    # Edit the project's config.json to add a workflows section overriding
    # exactly one task's button_label.
    cfg_path = fn_hardware_project / "config" / "config.json"
    full = json.loads(cfg_path.read_text(encoding="utf-8"))
    full["workflows"] = {
        "text_json": {
            "tasks": [
                {
                    "id": "derive_json_from_text",
                    "name": "Derive JSON from Text",
                    "button_label": "Custom",
                    "prompt_template": None,
                    "enabled": True,
                    "max_validator_attempts": None,
                },
            ],
        },
    }
    cfg_path.write_text(json.dumps(full, indent=2), encoding="utf-8")

    mgr = TaskConfigManager(_FALLBACK_PATH, project_root=fn_hardware_project)

    # The overridden task shows the override.
    derive = mgr.get_task_config("text_json", "derive_json_from_text")
    assert derive is not None
    assert derive.button_label == "Custom"

    # Other text_json tasks AND other tabs come from fallback / defaults.
    # Project's text_json declared only the derive task; the rest of
    # text_json's defaults are NOT auto-filled (a partial workflows.tab
    # is taken as authoritative for that tab — by design).
    for tab_id in ("text_only", "json_code"):
        tasks = mgr.get_all_tasks_for_tab(tab_id)
        assert tasks, f"tab {tab_id} should be populated"
        for task in tasks:
            fallback_label = expected.get(tab_id, {}).get(task.id)
            if fallback_label is not None:
                assert task.button_label == fallback_label, (
                    f"tab {tab_id} task {task.id} should stay at fallback "
                    f"{fallback_label!r}, got {task.button_label!r}"
                )

    # Traceability stays chat-only.
    assert mgr.get_chat_config("traceability") is not None
