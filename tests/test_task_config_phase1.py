"""Phase-1 round-trip + project-mode tests for TaskConfigManager.

Covers the contracts established in Phase 1 of the workflows-to-project-config
refactor (see PLAN_workflows_to_project_config.md):

- Unknown ``workflows.<tab>`` keys (e.g. a future ``validators`` block)
  survive load+save verbatim (Codex Q3).
- Project mode reads ``<project>/config/config.json:workflows`` and falls
  back to the shared repo file, then to ``DEFAULT_TASK_CONFIGS``.
- A per-task ``button_label`` override on top of the fallback chain shows
  up for that task while the rest stay at the fallback's values.
- ``reload(project_root)`` clears the cache before re-reading (no leak
  from the previous project's state).
- The migration ``<project>/config/tab_contexts.json`` →
  ``config.json:workflows`` is exhaustive and deletes the orphan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_editor.core.task_config import (
    DEFAULT_TASK_CONFIGS,
    TaskConfigManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shared_fallback(tmp_path: Path) -> Path:
    """A minimal tab_contexts-shaped fallback file used as the read-only
    fallback in project-mode tests."""
    p = tmp_path / "shared_fallback.json"
    p.write_text(json.dumps({
        "text_json": {
            "tasks": [
                {
                    "id": "derive_json_from_text",
                    "name": "Derive JSON from Text",
                    "button_label": "Text → JSON",
                    "prompt_template": None,
                    "enabled": True,
                    "max_validator_attempts": None,
                },
                {
                    "id": "review_json",
                    "name": "Review JSON",
                    "button_label": "Review JSON",
                    "prompt_template": None,
                    "enabled": True,
                    "max_validator_attempts": None,
                },
            ],
            "chat_config": {"enabled": True, "system_prompt": None},
        },
    }), encoding="utf-8")
    return p


def _project_root(tmp_path: Path) -> Path:
    """Create a minimal project skeleton (just the config/ dir)."""
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# Q3 — unknown-key preservation (THE Phase-1 contract test)
# ---------------------------------------------------------------------------


def test_unknown_workflows_keys_survive_round_trip(tmp_path):
    """A future ``validators`` block (unknown to Phase 1) must round-trip
    through load+save with byte-equal content. Without preservation,
    Phase-2 work shipped to disk by hand would be silently dropped on the
    next save."""
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)

    # Project carries the future validators block AND a fully-formed tasks list.
    project_cfg = root / "config" / "config.json"
    project_cfg.write_text(json.dumps({
        "manifest": {"name": "Round-trip test"},
        "workflows": {
            "text_json": {
                "tasks": [
                    {
                        "id": "derive_json_from_text",
                        "name": "Derive JSON from Text",
                        "button_label": "Override",
                        "prompt_template": None,
                        "enabled": True,
                        "max_validator_attempts": None,
                    },
                ],
                "chat_config": {"enabled": True, "system_prompt": "custom"},
                "validators": [
                    {"id": "rules_packager_base.validate_procedure", "enabled": True},
                    {"id": "core.check_python_syntax", "enabled": False},
                ],
                "future_extension": {"opaque": ["data", 42]},
            },
        },
    }, indent=2), encoding="utf-8")

    mgr = TaskConfigManager(fallback, project_root=root)

    # Touch a known field so save_config has something to actually persist.
    mgr.update_task_config("text_json", "derive_json_from_text",
                           button_label="Override Edited")
    assert mgr.save_config()

    # Read back the on-disk config and assert preservation.
    after = json.loads(project_cfg.read_text(encoding="utf-8"))
    assert after["manifest"] == {"name": "Round-trip test"}, "outer keys preserved"

    wf = after["workflows"]["text_json"]
    assert wf["validators"] == [
        {"id": "rules_packager_base.validate_procedure", "enabled": True},
        {"id": "core.check_python_syntax", "enabled": False},
    ], "validators block survived"
    assert wf["future_extension"] == {"opaque": ["data", 42]}, "future block survived"
    assert wf["chat_config"] == {"enabled": True, "system_prompt": "custom"}
    assert wf["tasks"][0]["button_label"] == "Override Edited", "edit persisted"


# ---------------------------------------------------------------------------
# Project-mode load chain: project → fallback → defaults
# ---------------------------------------------------------------------------


def test_project_with_no_workflows_uses_fallback(tmp_path):
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)
    (root / "config" / "config.json").write_text(
        json.dumps({"manifest": {"name": "no workflows"}}),
        encoding="utf-8",
    )
    mgr = TaskConfigManager(fallback, project_root=root)

    tasks = mgr.get_all_tasks_for_tab("text_json")
    labels = [t.button_label for t in tasks]
    assert labels == ["Text → JSON", "Review JSON"], "fallback labels used"


def test_project_override_per_task_button_label(tmp_path):
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)
    (root / "config" / "config.json").write_text(json.dumps({
        "workflows": {
            "text_json": {
                "tasks": [
                    {
                        "id": "derive_json_from_text",
                        "name": "Derive JSON from Text",
                        "button_label": "CUSTOM Text→JSON",
                        "prompt_template": None,
                        "enabled": True,
                    },
                ],
            },
        },
    }), encoding="utf-8")

    mgr = TaskConfigManager(fallback, project_root=root)
    task = mgr.get_task_config("text_json", "derive_json_from_text")
    assert task.button_label == "CUSTOM Text→JSON", "override wins"

    # All other defaults still come through via _fill_missing_defaults.
    other_tabs = [tid for tid in DEFAULT_TASK_CONFIGS if tid != "text_json"]
    for tid in other_tabs:
        assert mgr.get_all_tasks_for_tab(tid), f"tab {tid} populated from defaults"


def test_no_files_anywhere_uses_baked_defaults(tmp_path):
    fallback = tmp_path / "does_not_exist.json"
    root = _project_root(tmp_path)
    mgr = TaskConfigManager(fallback, project_root=root)
    for tab_id, defaults in DEFAULT_TASK_CONFIGS.items():
        tasks = mgr.get_all_tasks_for_tab(tab_id)
        assert len(tasks) == len(defaults)


# ---------------------------------------------------------------------------
# reload()
# ---------------------------------------------------------------------------


def test_reload_clears_cache_and_switches_project(tmp_path):
    fallback = _shared_fallback(tmp_path)

    proj_a = tmp_path / "proj_a"
    (proj_a / "config").mkdir(parents=True)
    (proj_a / "config" / "config.json").write_text(json.dumps({
        "workflows": {
            "text_json": {
                "tasks": [
                    {
                        "id": "derive_json_from_text",
                        "name": "Derive JSON from Text",
                        "button_label": "Label A",
                    },
                ],
            },
        },
    }), encoding="utf-8")

    proj_b = tmp_path / "proj_b"
    (proj_b / "config").mkdir(parents=True)
    (proj_b / "config" / "config.json").write_text(json.dumps({
        "workflows": {
            "text_json": {
                "tasks": [
                    {
                        "id": "derive_json_from_text",
                        "name": "Derive JSON from Text",
                        "button_label": "Label B",
                    },
                ],
            },
        },
    }), encoding="utf-8")

    mgr = TaskConfigManager(fallback, project_root=proj_a)
    assert mgr.get_task_config("text_json", "derive_json_from_text").button_label == "Label A"

    mgr.reload(proj_b)
    assert mgr.get_task_config("text_json", "derive_json_from_text").button_label == "Label B"
    assert mgr.project_root == proj_b

    # Switching to a project with no workflows key falls back to the shared file.
    proj_c = tmp_path / "proj_c"
    (proj_c / "config").mkdir(parents=True)
    (proj_c / "config" / "config.json").write_text("{}", encoding="utf-8")
    mgr.reload(proj_c)
    assert mgr.get_task_config("text_json", "derive_json_from_text").button_label == "Text → JSON"


def test_reload_fires_callbacks(tmp_path):
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)
    mgr = TaskConfigManager(fallback, project_root=root)

    fires = []
    mgr.register_reload_callback(lambda: fires.append("a"))
    mgr.register_reload_callback(lambda: fires.append("b"))

    mgr.reload(root)
    assert fires == ["a", "b"]


# ---------------------------------------------------------------------------
# Migration: <project>/config/tab_contexts.json → config.json:workflows
# ---------------------------------------------------------------------------


def test_migrates_project_tab_contexts(tmp_path):
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)
    legacy = root / "config" / "tab_contexts.json"

    legacy.write_text(json.dumps({
        "text_json": {
            "selected_rules": ["rule_a.md", "rule_b.md"],
            "tasks": [
                {
                    "id": "derive_json_from_text",
                    "name": "Derive JSON from Text",
                    "button_label": "Legacy Label",
                },
            ],
            "future_block": {"keep": "me"},
        },
    }), encoding="utf-8")

    # Pre-existing manifest must survive.
    (root / "config" / "config.json").write_text(
        json.dumps({"manifest": {"name": "with manifest"}}),
        encoding="utf-8",
    )

    mgr = TaskConfigManager(fallback, project_root=root)

    # Legacy file deleted.
    assert not legacy.exists(), "legacy tab_contexts.json should be removed"

    # config.json now holds workflows section + manifest preserved.
    on_disk = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["manifest"] == {"name": "with manifest"}
    wf = on_disk["workflows"]["text_json"]
    # Tab-level selected_rules dropped (lifted onto task).
    assert "selected_rules" not in wf
    assert wf["tasks"][0]["selected_rules"] == ["rule_a.md", "rule_b.md"]
    assert wf["tasks"][0]["button_label"] == "Legacy Label"
    assert wf["future_block"] == {"keep": "me"}, "unknown keys preserved"

    # In-memory state matches.
    task = mgr.get_task_config("text_json", "derive_json_from_text")
    assert task.button_label == "Legacy Label"
    assert task.selected_rules == ["rule_a.md", "rule_b.md"]


def test_migration_aborts_when_project_config_corrupt(tmp_path):
    """If config.json is unreadable, the migration must not clobber it."""
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)

    legacy = root / "config" / "tab_contexts.json"
    legacy.write_text(json.dumps({"text_json": {"tasks": []}}), encoding="utf-8")

    corrupt = root / "config" / "config.json"
    corrupt.write_text("{ not valid json", encoding="utf-8")

    TaskConfigManager(fallback, project_root=root)

    # Both files left in place; corrupt config.json untouched.
    assert legacy.exists(), "legacy preserved on abort"
    assert corrupt.read_text(encoding="utf-8") == "{ not valid json"


def test_migration_idempotent_when_workflows_already_present(tmp_path):
    """If config.json already has a workflows section, the migration must
    not overwrite it from a stale tab_contexts.json."""
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)

    legacy = root / "config" / "tab_contexts.json"
    legacy.write_text(json.dumps({
        "text_json": {"tasks": [{"id": "stale", "name": "stale", "button_label": "stale"}]},
    }), encoding="utf-8")

    project_cfg = root / "config" / "config.json"
    project_cfg.write_text(json.dumps({
        "workflows": {
            "text_json": {"tasks": [
                {"id": "derive_json_from_text", "name": "n", "button_label": "Existing"},
            ]},
        },
    }), encoding="utf-8")

    mgr = TaskConfigManager(fallback, project_root=root)

    # Workflows section untouched (the value still says "Existing").
    on_disk = json.loads(project_cfg.read_text(encoding="utf-8"))
    assert on_disk["workflows"]["text_json"]["tasks"][0]["button_label"] == "Existing"
    # Stale legacy file removed.
    assert not legacy.exists()
    # In-memory matches existing, not stale.
    assert mgr.get_task_config(
        "text_json", "derive_json_from_text"
    ).button_label == "Existing"


# ---------------------------------------------------------------------------
# selected_rules per-task lift
# ---------------------------------------------------------------------------


def test_selected_rules_lifted_onto_each_task(tmp_path):
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)
    (root / "config" / "config.json").write_text(json.dumps({
        "workflows": {
            "text_json": {
                "selected_rules": ["x.md", "y.md"],
                "tasks": [
                    {"id": "derive_json_from_text", "name": "n", "button_label": "L1"},
                    {"id": "review_json", "name": "n", "button_label": "L2",
                     "selected_rules": ["override.md"]},
                ],
            },
        },
    }), encoding="utf-8")

    mgr = TaskConfigManager(fallback, project_root=root)
    assert mgr.get_selected_rules_for_task("text_json", "derive_json_from_text") == ["x.md", "y.md"]
    # Task-level override wins.
    assert mgr.get_selected_rules_for_task("text_json", "review_json") == ["override.md"]


def test_set_selected_rules_for_tab_propagates(tmp_path):
    fallback = _shared_fallback(tmp_path)
    root = _project_root(tmp_path)
    mgr = TaskConfigManager(fallback, project_root=root)

    mgr.set_selected_rules_for_tab("text_json", ["a.md", "b.md"])
    for task in mgr.get_all_tasks_for_tab("text_json"):
        assert task.selected_rules == ["a.md", "b.md"]

    # Persisting and re-loading produces the same per-task values.
    mgr.save_config()
    mgr2 = TaskConfigManager(fallback, project_root=root)
    for task in mgr2.get_all_tasks_for_tab("text_json"):
        assert task.selected_rules == ["a.md", "b.md"]
