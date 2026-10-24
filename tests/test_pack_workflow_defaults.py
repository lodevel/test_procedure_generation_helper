"""Tests for the bundle-defaults hand-off and the sparse-task layering.

The legacy manifest-walk (drivers_registry → rules_index →
pack_workflow_defaults.json) is gone. The parent app resolves the
project's active bundle and hands the editor the path to the
bundle's pre-merged ``defaults.json`` via the
``PROCEDO_BUNDLE_DEFAULTS_PATH`` env var. This test module covers:

* ``load_pack_workflow_defaults`` reading from that env var (happy
  path + fallbacks).
* editor < pack < project layering of sparse bundle tasks (the merge
  itself is the host's ``workflow_defaults.merge_workflows``, tested there).
"""

from __future__ import annotations

import json

from procedo.studio.core.task_config import TaskConfigManager
from procedo.studio.core.task_config_defaults import load_pack_workflow_defaults
from procedo.core.studio_launch_env import BUNDLE_DEFAULTS_PATH


# ---------------------------------------------------------------------------
# Bundle defaults hand-off (Phase 4a / 5h)
# ---------------------------------------------------------------------------


def test_bundle_env_var_loads_workflows(monkeypatch, tmp_path):
    """Parent sets PROCEDO_BUNDLE_DEFAULTS_PATH → editor reads the
    workflows block from that file. The legacy manifest walk is
    gone; this is the only discovery path."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({
        "workflows": {
            "text_json": {"validators": [{"id": "bundle.v"}]},
        },
        "extractors": {"equipment": {"module": "m"}},
    }), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(defaults_path))

    result = load_pack_workflow_defaults(None)
    assert result == {"text_json": {"validators": [{"id": "bundle.v"}]}}


def test_bundle_env_var_unset_returns_empty(monkeypatch, tmp_path):
    """No env var → empty dict. The editor's merge with its baked-in
    defaults degrades to editor-defaults-only."""
    monkeypatch.delenv(BUNDLE_DEFAULTS_PATH, raising=False)
    assert load_pack_workflow_defaults(None) == {}


def test_bundle_env_var_missing_file_returns_empty(monkeypatch, tmp_path):
    """A stale env var pointing at a deleted file must not crash."""
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(tmp_path / "gone.json"))
    assert load_pack_workflow_defaults(None) == {}


def test_bundle_env_var_malformed_file_returns_empty(monkeypatch, tmp_path):
    """defaults.json with wrong top-level shape (list instead of
    object) collapses to empty dict."""
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(bad))
    assert load_pack_workflow_defaults(None) == {}


def test_bundle_env_var_no_workflows_block_returns_empty(monkeypatch, tmp_path):
    """defaults.json present but workflows: missing → return empty
    dict. The bundle is the authoritative source; an absent workflows
    block is a real 'no workflow defaults' state."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({"extractors": {}}), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(defaults_path))
    assert load_pack_workflow_defaults(None) == {}


def test_sparse_pack_tasks_inherit_name_from_editor_defaults(monkeypatch, tmp_path):
    """Locks editor < pack < project layering after the prompt migration.

    Post-migration the bundle ``defaults.json:workflows.<tab>.tasks`` carries
    sparse ``{id, prompt_template}`` entries — per-task ``name`` /
    ``button_label`` / ``enabled`` must inherit from the editor's
    baked-in ``DEFAULT_TASK_CONFIGS`` via per-field None-passthrough,
    not crash ``TaskConfig.__init__`` (which requires those fields).
    Pre-fix this raised ``TypeError`` because ``_load_config`` merged
    only pack + project with no editor base layer."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({
        "workflows": {
            "text_json": {
                "tasks": [
                    {"id": "derive_json_from_text",
                     "prompt_template": "BUNDLE-OWNED PROMPT"},
                ],
            },
        },
    }), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(defaults_path))

    project = tmp_path / "proj"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.json").write_text("{}", encoding="utf-8")

    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project,
    )
    tasks = mgr._task_configs.get("text_json", [])
    derive = next((t for t in tasks if t.id == "derive_json_from_text"), None)
    assert derive is not None, f"derive_json_from_text missing in {[t.id for t in tasks]}"
    assert derive.prompt_template == "BUNDLE-OWNED PROMPT"
    assert derive.name, "name must inherit from editor baked-in default"
    assert derive.button_label, "button_label must inherit from editor baked-in default"
    assert derive.enabled is True


def test_editor_default_workflows_ship_no_prompts():
    """Locks the post-migration architecture: prompts are bundle-owned;
    the editor's ``default_workflows.json`` is field fallback for
    ``name`` / ``button_label`` / ``enabled`` only. If someone
    re-introduces a prompt here, bundle vs editor origin attribution
    will lie (the GUI shows a row as 'editor default' when in fact
    the user is reading text the bundle would have replaced)."""
    from procedo.studio.core.task_config_defaults import _DEFAULT_WORKFLOWS_RAW

    for tab_id, tab_cfg in _DEFAULT_WORKFLOWS_RAW.items():
        if not isinstance(tab_cfg, dict):
            continue
        for task in tab_cfg.get("tasks", []):
            assert task.get("prompt_template") is None, (
                f"editor's {tab_id}/{task.get('id')} carries a "
                "prompt_template; post-migration only the bundle "
                "is supposed to ship prompts"
            )


def test_sparse_pack_does_not_leak_inherited_fields_on_save(monkeypatch, tmp_path):
    """A user who touches NOTHING must not get pack/editor-inherited
    ``name`` / ``button_label`` / ``enabled`` stamped into their
    ``config.json``. The stamp filter (`_task_dicts_equal` against
    `_pack_task_dicts_by_tab`) must see the SAME shape that
    `TaskConfig.to_dict()` produces — so the index has to be built
    from the editor⊕pack baseline, not from the raw sparse pack."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({
        "workflows": {
            "text_json": {
                "tasks": [
                    {"id": "derive_json_from_text",
                     "prompt_template": "BUNDLE-OWNED PROMPT"},
                ],
            },
        },
    }), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(defaults_path))

    project = tmp_path / "proj"
    (project / "config").mkdir(parents=True)
    cfg_path = project / "config" / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")

    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project,
    )

    # The user touches the task without changing any field: refresh the
    # snapshot for derive_json_from_text exactly as the GUI does on every
    # tab close / save.
    mgr._stamp_task_in_snapshot("text_json", "derive_json_from_text")
    mgr.save_config()

    # Project config.json must NOT carry derive_json_from_text — the task
    # is pack-identical (after editor fallback fill-in) and untouched.
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    text_json_snap = on_disk.get("workflows", {}).get("text_json", {})
    snap_tasks = text_json_snap.get("tasks", [])
    leaked = [t for t in snap_tasks if t.get("id") == "derive_json_from_text"]
    assert not leaked, (
        "Untouched pack task leaked into project config — inherited "
        f"name/button_label would now be frozen. Leaked entry: {leaked}"
    )
