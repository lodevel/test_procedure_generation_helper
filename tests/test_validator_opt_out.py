"""Phase 4.6: per-project deterministic-validator opt-out.

When ``validator_loop.enabled=false`` is set in a project's
config.json, the deterministic validator is treated as intentionally
off:

* ``is_loop_available`` returns False with a "disabled in project
  settings" reason.
* The auto-correct LLM-loop short-circuits (no probe, no retries).
* The "Deterministic validator unavailable" chat warning is
  suppressed (operator opted out — don't nag them).

The flag defaults to True so existing projects keep their current
behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_editor.llm.validator_dispatch import is_loop_available
from workflow_editor.llm.validator_loop_settings import (
    SECTION_NAME,
    is_enabled,
    save_setting,
)


def _project(tmp_path: Path, validator_loop: dict | None = None) -> Path:
    """Build a minimal project root with an optional validator_loop block."""
    root = tmp_path / "proj"
    cfg = root / "config"
    cfg.mkdir(parents=True)
    data: dict = {}
    if validator_loop is not None:
        data[SECTION_NAME] = validator_loop
    (cfg / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return root


def test_is_enabled_defaults_to_true_when_section_absent(tmp_path):
    root = _project(tmp_path)
    assert is_enabled(root) is True


def test_is_enabled_defaults_to_true_when_flag_missing(tmp_path):
    root = _project(tmp_path, {"max_attempts": 5})
    assert is_enabled(root) is True


def test_is_enabled_reads_false_when_set(tmp_path):
    root = _project(tmp_path, {"enabled": False})
    assert is_enabled(root) is False


def test_is_enabled_handles_none_project_root():
    # Defaults to True (no per-project opt-out path).
    assert is_enabled(None) is True


def test_is_loop_available_returns_disabled_when_user_opted_out(tmp_path):
    root = _project(tmp_path, {"enabled": False})
    available, reason = is_loop_available(root)
    assert available is False
    assert "disabled in project settings" in reason


def test_is_loop_available_runs_probe_when_enabled_true(tmp_path):
    """With enabled=True and no text_renderer configured, the function
    falls through to the existing probe, which yields the "validator
    unavailable" reason (not the "disabled" one)."""
    root = _project(tmp_path, {"enabled": True})
    available, reason = is_loop_available(root)
    assert available is False
    # Existing path: missing text_renderer → "validator unavailable".
    # Not the "disabled" reason — operator did NOT opt out.
    assert "disabled in project settings" not in reason


def test_save_setting_persists_enabled_flag(tmp_path):
    root = _project(tmp_path)
    save_setting(root, "enabled", False)
    on_disk = json.loads(
        (root / "config" / "config.json").read_text(encoding="utf-8")
    )
    assert on_disk[SECTION_NAME]["enabled"] is False
    # Re-read via the helper.
    assert is_enabled(root) is False
