"""Tests for the bundle-defaults hand-off + merge helper.

The legacy manifest-walk (drivers_registry → rules_index →
pack_workflow_defaults.json) is gone. The parent app resolves the
project's active bundle and hands the editor the path to the
bundle's pre-merged ``defaults.json`` via the
``TPG_BUNDLE_DEFAULTS_PATH`` env var. This test module covers:

* ``_load_pack_workflow_defaults`` reading from that env var (happy
  path + fallbacks).
* ``_merge_workflows`` per-tab overlay semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_editor.core.task_config import (
    TaskConfigManager,
    _merge_workflows,
)


# ---------------------------------------------------------------------------
# Bundle defaults hand-off (Phase 4a / 5h)
# ---------------------------------------------------------------------------


def test_bundle_env_var_loads_workflows(monkeypatch, tmp_path):
    """Parent sets TPG_BUNDLE_DEFAULTS_PATH → editor reads the
    workflows block from that file. The legacy manifest walk is
    gone; this is the only discovery path."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({
        "workflows": {
            "text_json": {"validators": [{"id": "bundle.v"}]},
        },
        "extractors": {"equipment": {"module": "m"}},
    }), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(defaults_path))

    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    result = mgr._load_pack_workflow_defaults()
    assert result == {"text_json": {"validators": [{"id": "bundle.v"}]}}


def test_bundle_env_var_unset_returns_empty(monkeypatch, tmp_path):
    """No env var → empty dict. The editor's merge with its baked-in
    defaults degrades to editor-defaults-only."""
    monkeypatch.delenv("TPG_BUNDLE_DEFAULTS_PATH", raising=False)
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}


def test_bundle_env_var_missing_file_returns_empty(monkeypatch, tmp_path):
    """A stale env var pointing at a deleted file must not crash."""
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(tmp_path / "gone.json"))
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}


def test_bundle_env_var_malformed_file_returns_empty(monkeypatch, tmp_path):
    """defaults.json with wrong top-level shape (list instead of
    object) collapses to empty dict."""
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(bad))
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}


def test_bundle_env_var_no_workflows_block_returns_empty(monkeypatch, tmp_path):
    """defaults.json present but workflows: missing → return empty
    dict. The bundle is the authoritative source; an absent workflows
    block is a real 'no workflow defaults' state."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({"extractors": {}}), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(defaults_path))
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}


# ---------------------------------------------------------------------------
# _merge_workflows
# ---------------------------------------------------------------------------


def test_merge_workflows_overlay_overrides_per_key():
    base = {"text_json": {"a": 1, "b": 2}}
    overlay = {"text_json": {"a": 9}}
    merged = _merge_workflows(base, overlay)
    assert merged == {"text_json": {"a": 9, "b": 2}}


def test_merge_workflows_new_tab_from_overlay():
    base = {"text_json": {"a": 1}}
    overlay = {"json_code": {"x": 7}}
    merged = _merge_workflows(base, overlay)
    assert merged == {"text_json": {"a": 1}, "json_code": {"x": 7}}


def test_merge_workflows_empty_base():
    assert _merge_workflows({}, {"text_json": {"a": 1}}) == {"text_json": {"a": 1}}


def test_merge_workflows_empty_overlay():
    assert _merge_workflows({"text_json": {"a": 1}}, {}) == {"text_json": {"a": 1}}


def test_merge_workflows_non_dict_tab_value_tolerated():
    """Defensive: a malformed tab payload (non-dict) shouldn't crash the
    merge — it's treated as an empty dict so the other side wins."""
    base = {"text_json": "not-a-dict"}
    overlay = {"text_json": {"a": 1}}
    assert _merge_workflows(base, overlay) == {"text_json": {"a": 1}}
