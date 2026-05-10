"""Phase-2/3 snapshot pattern: ensure pack defaults never leak into
``<project>/config/config.json`` on save (Codex H2) and that the
single-writer injection seam (Codex H1.D / Q7) routes payloads through
``register_workflows_writer`` when set.

The snapshot pattern: at load time, ``_project_workflow_snapshot`` is a
``deepcopy`` of the project's verbatim ``workflows`` section (pack
defaults are NOT merged in). All mutation APIs stamp the snapshot so it
stays current. ``save_config()`` writes ONLY the snapshot — pack-
inherited tasks / validators / chat_configs never round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from workflow_editor.core.task_config import (
    ChatConfig,
    TaskConfig,
    TaskConfigManager,
)


# ---------------------------------------------------------------------------
# Test fixtures — synthetic pack with workflow defaults
# ---------------------------------------------------------------------------


def _shared_fallback(tmp_path: Path) -> Path:
    """Empty fallback file. Tests run in project mode; the fallback is
    only touched if the project path resolves to None — which doesn't
    happen here."""
    p = tmp_path / "fallback" / "tab_contexts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    return p


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True, exist_ok=True)
    return root


def _write_project_config(root: Path, payload: dict) -> Path:
    cfg = root / "config" / "config.json"
    cfg.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return cfg


def _read_project_config(root: Path) -> dict:
    return json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))


@pytest.fixture
def stub_pack_workflows(monkeypatch):
    """Force ``_load_pack_workflow_defaults`` to return a synthetic
    payload — bypasses the manifest-path discovery entirely so the test
    doesn't depend on which packs the project declares."""
    payload: dict[str, Any] = {
        "text_json": {
            "tasks": [
                {
                    "id": "pack_only_task",
                    "name": "Pack-only Task",
                    "button_label": "Pack Label",
                    "prompt_template": None,
                    "enabled": True,
                    "selected_rules": ["pack.md"],
                    "max_validator_attempts": None,
                },
                {
                    "id": "review_text",
                    "name": "Review (pack)",
                    "button_label": "Review",
                    "prompt_template": None,
                    "enabled": True,
                    "selected_rules": None,
                    "max_validator_attempts": None,
                },
            ],
            "validators": [
                {"id": "pack.validator_a", "enabled": True},
                {"id": "pack.validator_b", "enabled": True},
            ],
            "chat_config": {"enabled": True, "system_prompt": "PACK chat"},
        },
    }

    def _patched(self):  # noqa: ANN001 — patching a bound method
        return payload

    monkeypatch.setattr(
        TaskConfigManager, "_load_pack_workflow_defaults", _patched, raising=True
    )
    return payload


# ---------------------------------------------------------------------------
# Codex H2: pack defaults NEVER leak into project config.json on save
# ---------------------------------------------------------------------------


def test_no_workflows_no_mutation_save_writes_empty_block(tmp_path, stub_pack_workflows):
    """Project starts with no ``workflows`` key. Load surfaces pack data
    in caches, but with no mutation the on-disk file's ``workflows``
    block stays empty after save — pack data did NOT leak."""
    root = _project_root(tmp_path)
    _write_project_config(root, {"manifest": {"name": "p"}})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    # Sanity: pack data IS visible in the read API.
    assert any(t.id == "pack_only_task" for t in mgr.get_all_tasks_for_tab("text_json"))

    assert mgr.save_config()

    on_disk = _read_project_config(root)
    assert on_disk.get("workflows", {}) == {}, (
        "no-mutation save must not persist pack-inherited tasks"
    )
    assert on_disk["manifest"] == {"name": "p"}, "outer keys preserved"


def test_project_override_save_keeps_pack_data_out(tmp_path, stub_pack_workflows):
    """Project authors ONE task override in text_json. After save, the
    project's workflows.text_json.tasks still has exactly one task
    (the override) — pack's other tasks did NOT migrate into the file."""
    root = _project_root(tmp_path)
    _write_project_config(root, {
        "workflows": {
            "text_json": {
                "tasks": [
                    {"id": "derive_json_from_text", "name": "Derive",
                     "button_label": "User Custom", "enabled": True,
                     "prompt_template": None, "selected_rules": None,
                     "max_validator_attempts": None},
                ],
            },
        },
    })
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    text_json = on_disk["workflows"]["text_json"]
    task_ids = [t["id"] for t in text_json.get("tasks", [])]
    assert task_ids == ["derive_json_from_text"], (
        f"expected only the project-authored task; got {task_ids}"
    )
    assert "validators" not in text_json, (
        "pack validators must not be mirrored into project config"
    )
    assert "chat_config" not in text_json, (
        "pack chat_config must not be mirrored into project config"
    )


def test_mutate_pack_only_task_stamps_just_that_one(tmp_path, stub_pack_workflows):
    """Project has no workflows. User edits a pack-only task. After save,
    the project's workflows.text_json.tasks has exactly ONE entry — the
    one the user stamped — not the pack's full task list."""
    root = _project_root(tmp_path)
    _write_project_config(root, {"manifest": {"name": "p"}})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    assert mgr.update_task_config("text_json", "pack_only_task",
                                  button_label="User Edited")
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    tasks = on_disk["workflows"]["text_json"]["tasks"]
    assert len(tasks) == 1, f"only the stamped task should persist; got {tasks}"
    assert tasks[0]["id"] == "pack_only_task"
    assert tasks[0]["button_label"] == "User Edited"
    assert on_disk["workflows"]["text_json"].get("validators") is None, (
        "pack validators must not be persisted"
    )


def test_validators_block_in_project_survives_pack_overlap(tmp_path, stub_pack_workflows):
    """Project carries its own ``validators`` block. Pack also provides
    one (with different IDs). After save, the project's block remains
    verbatim; pack's block is NOT mirrored."""
    root = _project_root(tmp_path)
    project_validators = [{"id": "project.x", "enabled": True}]
    _write_project_config(root, {
        "workflows": {
            "text_json": {
                "validators": project_validators,
            },
        },
    })
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    # Touch SOMETHING so save_config() actually runs the snapshot path.
    mgr.update_task_config("text_json", "pack_only_task",
                           button_label="touch")
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    text_json = on_disk["workflows"]["text_json"]
    assert text_json["validators"] == project_validators, (
        "project's validator list must survive verbatim"
    )
    # Make sure no pack validator id sneaked in.
    serialised = json.dumps(text_json["validators"])
    assert "pack.validator_a" not in serialised
    assert "pack.validator_b" not in serialised


def test_chat_config_pack_only_does_not_leak(tmp_path, stub_pack_workflows):
    """Pack ships a chat_config; project doesn't. Save without mutation:
    the project config's text_json block stays empty (no chat_config)."""
    root = _project_root(tmp_path)
    _write_project_config(root, {"manifest": {"name": "p"}})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    assert on_disk.get("workflows", {}) == {}, (
        "pack chat_config must not be persisted as project chat_config"
    )


def test_set_chat_config_stamps_only_that_tab(tmp_path, stub_pack_workflows):
    """User sets a chat_config on text_only. Only text_only ends up in
    the saved project config; text_json (which has pack defaults but no
    user mutation) is NOT persisted."""
    root = _project_root(tmp_path)
    _write_project_config(root, {})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    mgr.set_chat_config("text_only", ChatConfig(enabled=True, system_prompt="user!"))
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    wf = on_disk["workflows"]
    assert set(wf.keys()) == {"text_only"}, (
        f"only the mutated tab should persist; got {set(wf.keys())}"
    )
    assert wf["text_only"]["chat_config"] == {"enabled": True, "system_prompt": "user!"}


def test_reload_clears_snapshot(tmp_path, stub_pack_workflows):
    """``reload(new_root)`` clears the snapshot; mutation against the
    new project doesn't leak data from the previous one."""
    root_a = _project_root(tmp_path / "a")
    _write_project_config(root_a, {"workflows": {"text_json": {
        "tasks": [{"id": "a_task", "name": "A", "button_label": "A",
                   "enabled": True, "prompt_template": None,
                   "selected_rules": None, "max_validator_attempts": None}]
    }}})

    root_b = _project_root(tmp_path / "b")
    _write_project_config(root_b, {})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root_a)
    mgr.reload(root_b)
    assert mgr.save_config()

    on_disk_b = _read_project_config(root_b)
    assert on_disk_b.get("workflows", {}) == {}, (
        "previous project's snapshot must not bleed into the new project"
    )


# ---------------------------------------------------------------------------
# Codex H1.D / Q7: register_workflows_writer routes the payload
# ---------------------------------------------------------------------------


def test_workflows_writer_receives_snapshot_payload(tmp_path, stub_pack_workflows):
    """When a writer is registered, save_config() calls it with the
    PROJECT-ONLY snapshot (NOT the merged caches) and does NOT touch
    the on-disk file directly."""
    root = _project_root(tmp_path)
    _write_project_config(root, {
        "workflows": {
            "text_json": {
                "tasks": [{"id": "derive_json_from_text", "name": "D",
                           "button_label": "P", "enabled": True,
                           "prompt_template": None, "selected_rules": None,
                           "max_validator_attempts": None}],
            },
        },
    })
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)

    captured: list[dict] = []
    def _writer(payload: dict) -> bool:
        captured.append(payload)
        return True
    mgr.register_workflows_writer(_writer)

    mgr.update_task_config("text_json", "derive_json_from_text",
                           button_label="Edited")
    assert mgr.save_config()

    assert len(captured) == 1
    payload = captured[0]
    assert "text_json" in payload
    assert payload["text_json"]["tasks"][0]["button_label"] == "Edited"
    # Pack-only data must NOT be in the payload.
    assert "validators" not in payload["text_json"]
    assert "chat_config" not in payload["text_json"]
    pack_only_ids = [t.get("id") for t in payload["text_json"]["tasks"]
                     if t.get("id") == "pack_only_task"]
    assert pack_only_ids == [], "pack-only tasks must not be in writer payload"

    # And the on-disk file must NOT have been touched by save_config().
    on_disk = _read_project_config(root)
    assert on_disk["workflows"]["text_json"]["tasks"][0]["button_label"] == "P", (
        "writer-routed save must NOT bypass and write directly"
    )


def test_workflows_writer_failure_logged_not_raised(tmp_path, stub_pack_workflows, caplog):
    root = _project_root(tmp_path)
    _write_project_config(root, {})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    mgr.register_workflows_writer(lambda _payload: False)

    mgr.set_chat_config("text_only", ChatConfig(enabled=False, system_prompt=None))
    with caplog.at_level("ERROR"):
        # save_config catches and reports True (we count "writer ran" as success).
        ok = mgr.save_config()

    # save_config returns True because no exception bubbled; the writer's
    # own failure is logged at ERROR level so operators can diagnose.
    assert ok
    assert any("writer reported failure" in rec.message for rec in caplog.records)


def test_writer_unregister_reverts_to_direct_write(tmp_path, stub_pack_workflows):
    root = _project_root(tmp_path)
    _write_project_config(root, {})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    mgr.register_workflows_writer(lambda _payload: True)
    mgr.register_workflows_writer(None)  # unregister

    mgr.set_chat_config("text_only", ChatConfig(enabled=True, system_prompt="x"))
    assert mgr.save_config()
    on_disk = _read_project_config(root)
    assert on_disk["workflows"]["text_only"]["chat_config"] == {
        "enabled": True, "system_prompt": "x"
    }, "with writer cleared, save must write directly to config.json"


# ---------------------------------------------------------------------------
# Lifecycle: reset_to_defaults + delete_task drop snapshot entries
# ---------------------------------------------------------------------------


def test_reset_to_defaults_drops_tab_from_snapshot(tmp_path, stub_pack_workflows):
    """``reset_to_defaults(tab)`` removes the tab from the snapshot so
    save reverts to pack / baked-in defaults rather than persisting them."""
    root = _project_root(tmp_path)
    _write_project_config(root, {
        "workflows": {
            "text_json": {
                "tasks": [{"id": "derive_json_from_text", "name": "D",
                           "button_label": "Custom", "enabled": True,
                           "prompt_template": None, "selected_rules": None,
                           "max_validator_attempts": None}],
            },
        },
    })
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    assert mgr.reset_to_defaults("text_json")
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    assert "text_json" not in on_disk.get("workflows", {}), (
        "reset_to_defaults should remove the tab from project config"
    )


# ---------------------------------------------------------------------------
# Codex Q1: set_all_tasks_for_tab filters pack-identical entries
# ---------------------------------------------------------------------------


def test_set_all_tasks_filters_pack_identical(tmp_path, stub_pack_workflows):
    """Mimics SettingsDialog's save path: read the merged task list,
    pass it back to ``set_all_tasks_for_tab`` unchanged. Pack-identical
    tasks must NOT be stamped into the snapshot — only the project-
    authored / user-modified ones should persist."""
    root = _project_root(tmp_path)
    _write_project_config(root, {})  # No project workflows.

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)

    # Read the merged task list as the dialog would.
    merged_tasks = mgr.get_all_tasks_for_tab("text_json")
    assert len(merged_tasks) >= 2

    # Round-trip unchanged.
    mgr.set_all_tasks_for_tab("text_json", merged_tasks)
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    assert on_disk.get("workflows", {}) == {}, (
        "round-tripping merged tasks through set_all_tasks_for_tab "
        "must NOT persist pack-identical tasks"
    )


def test_set_all_tasks_only_persists_user_modified(tmp_path, stub_pack_workflows):
    """Same flow as above but the user edits ONE task's button_label
    before passing the list back. Only the modified task should land in
    the snapshot — pack-identical entries are filtered out."""
    root = _project_root(tmp_path)
    _write_project_config(root, {})

    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    merged_tasks = mgr.get_all_tasks_for_tab("text_json")

    # User edits pack_only_task's button_label in the table.
    for t in merged_tasks:
        if t.id == "pack_only_task":
            t.button_label = "User Touched"

    mgr.set_all_tasks_for_tab("text_json", merged_tasks)
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    tasks = on_disk["workflows"]["text_json"]["tasks"]
    assert [t["id"] for t in tasks] == ["pack_only_task"], (
        f"only the touched task should persist; got {tasks}"
    )
    assert tasks[0]["button_label"] == "User Touched"


def test_set_all_tasks_drops_deleted_from_snapshot(tmp_path, stub_pack_workflows):
    """When the caller omits a task that was previously in the snapshot,
    it gets dropped — i.e. ``set_all_tasks_for_tab`` is treated as a
    replace, not a merge."""
    root = _project_root(tmp_path)
    _write_project_config(root, {
        "workflows": {
            "text_json": {
                "tasks": [
                    {"id": "user_a", "name": "A", "button_label": "A",
                     "enabled": True, "prompt_template": None,
                     "selected_rules": None, "max_validator_attempts": None},
                    {"id": "user_b", "name": "B", "button_label": "B",
                     "enabled": True, "prompt_template": None,
                     "selected_rules": None, "max_validator_attempts": None},
                ],
            },
        },
    })
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)

    # Drop user_a, keep user_b (and the pack tasks the cache surfaces).
    merged = mgr.get_all_tasks_for_tab("text_json")
    kept = [t for t in merged if t.id != "user_a"]
    mgr.set_all_tasks_for_tab("text_json", kept)
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    ids = [t["id"] for t in on_disk["workflows"]["text_json"]["tasks"]]
    assert "user_a" not in ids, "deleted task should no longer be in snapshot"
    assert "user_b" in ids, "non-deleted user task survives"
    assert "pack_only_task" not in ids, "pack-identical tasks still filtered"


# ---------------------------------------------------------------------------
# Codex Q5: SettingsDialog-style save path preserves selected_rules and
# max_validator_attempts (TaskConfig.from_dict is tolerant of missing
# keys, but the dialog must populate them to round-trip correctly).
# ---------------------------------------------------------------------------


def test_set_all_tasks_preserves_per_task_extra_fields(tmp_path, stub_pack_workflows):
    """``set_all_tasks_for_tab`` stamps the full ``TaskConfig.to_dict()``
    of each task into the snapshot, so callers that construct
    ``TaskConfig`` instances with ``selected_rules`` and
    ``max_validator_attempts`` set get those fields persisted intact.

    This pins the dialog-side fix (Codex Q5): if the SettingsDialog
    save flow stops carrying these fields, it'll round-trip them as
    None and silently erase data. This test asserts the *manager-side*
    contract — given correctly-constructed tasks, the values reach
    disk.
    """
    root = _project_root(tmp_path)
    _write_project_config(root, {})
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)

    task = TaskConfig(
        id="user_custom",
        name="Custom",
        button_label="Run",
        prompt_template="hello {{x}}",
        enabled=True,
        max_validator_attempts=3,
        selected_rules=["rule1.md", "rule2.md"],
    )
    mgr.set_all_tasks_for_tab("text_json", [task])
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    persisted = on_disk["workflows"]["text_json"]["tasks"][0]
    assert persisted["max_validator_attempts"] == 3
    assert persisted["selected_rules"] == ["rule1.md", "rule2.md"]
    assert persisted["prompt_template"] == "hello {{x}}"


def test_delete_task_removes_from_snapshot(tmp_path, stub_pack_workflows):
    root = _project_root(tmp_path)
    _write_project_config(root, {
        "workflows": {
            "text_json": {
                "tasks": [
                    {"id": "a", "name": "A", "button_label": "A", "enabled": True,
                     "prompt_template": None, "selected_rules": None,
                     "max_validator_attempts": None},
                    {"id": "b", "name": "B", "button_label": "B", "enabled": True,
                     "prompt_template": None, "selected_rules": None,
                     "max_validator_attempts": None},
                ],
            },
        },
    })
    mgr = TaskConfigManager(_shared_fallback(tmp_path), project_root=root)
    assert mgr.delete_task("text_json", "a")
    assert mgr.save_config()

    on_disk = _read_project_config(root)
    tasks = on_disk["workflows"]["text_json"]["tasks"]
    assert [t["id"] for t in tasks] == ["b"]
