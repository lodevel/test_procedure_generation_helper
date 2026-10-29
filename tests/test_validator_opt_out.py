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

Qt ownership note: the tabs' action bars below (their validator buttons
included) are installed on the tab (``QVBoxLayout(tab)``) — never left
without a parent.  A parentless row of ``QPushButton``s is torn down in
arbitrary order at interpreter shutdown and kills the process with an
access violation after pytest has already reported success.  Full
explanation in ``test_tab_registry_integration.py``'s module docstring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from procedo.studio.llm.validator_dispatch import is_loop_available
from procedo.studio.llm.validator_loop_settings import (
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


def test_is_loop_available_runs_probe_when_enabled_true(tmp_path, monkeypatch):
    """With enabled=True the function falls through to the wheel-import
    probe. Phase 5.1: no per-project parser variant gate — the wheel
    decides.
    """
    # Simulate wheel-missing so the probe returns False (the test runs
    # in a venv where the wheel may or may not be installed; we don't
    # want availability tied to that).
    from procedo.studio.pack_ops import pack_parsers

    monkeypatch.setattr(
        pack_parsers,
        "is_available",
        lambda project_root=None: (False, "rules_packager_base not importable in test venv"),
    )
    root = _project(tmp_path, {"enabled": True})
    available, reason = is_loop_available(root)
    assert available is False
    # The reason should reflect the wheel-import failure, NOT the
    # "disabled in project settings" branch (operator did not opt out).
    assert "disabled in project settings" not in reason
    assert "not importable" in reason


def test_is_loop_available_surfaces_wheel_import_error(tmp_path, monkeypatch):
    """When the rules_packager_base wheel can't be imported (stale wheel,
    pre-2.0.1 install, wrong venv), `is_loop_available` returns False
    with the underlying ImportError message in the reason so the operator
    sees "reinstall the wheel" — not a generic "no variant" message.

    Regression for the user-report 2026-05-11: validator stayed greyed
    out because the host venv had a wheel missing `check_name_fidelity`.
    """
    from procedo.studio.pack_ops import pack_parsers

    fake_error = (
        "rules_packager_base.rules.v2_0_2.parser is not importable: "
        "cannot import name 'check_name_fidelity'. Reinstall the "
        "rules_packager_base wheel (>= 2.0.1)."
    )
    monkeypatch.setattr(
        pack_parsers,
        "is_available",
        lambda project_root=None: (False, fake_error),
    )
    root = _project(tmp_path, {"enabled": True})
    available, reason = is_loop_available(root)
    assert available is False
    assert "Reinstall the rules_packager_base wheel" in reason


def test_save_setting_persists_enabled_flag(tmp_path):
    root = _project(tmp_path)
    save_setting(root, "enabled", False)
    on_disk = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert on_disk[SECTION_NAME]["enabled"] is False
    # Re-read via the helper.
    assert is_enabled(root) is False


def test_save_setting_refuses_a_config_it_cannot_read(tmp_path):
    # A damaged (or unreachable) config.json is never rewritten with this section alone.
    root = _project(tmp_path)
    cfg = root / "config" / "config.json"
    cfg.write_text('{"test_order": {"order": ["t1"]}, ', encoding="utf-8")
    with pytest.raises(ValueError):
        save_setting(root, "enabled", False)
    assert cfg.read_text(encoding="utf-8") == '{"test_order": {"order": ["t1"]}, '


# ---------------------------------------------------------------------------
# Auto-correct preference key is distinct from master enable
# ---------------------------------------------------------------------------


def test_auto_correct_key_is_separate_from_enabled(tmp_path):
    """Phase 4.6 split: chat-panel writes ``auto_correct``, Settings
    master toggle writes ``enabled``. They must not stomp each other."""
    root = _project(tmp_path)
    # Master toggle off.
    save_setting(root, "enabled", False)
    # Operator separately toggles auto-correct on.
    save_setting(root, "auto_correct", True)

    on_disk = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    section = on_disk[SECTION_NAME]
    assert section["enabled"] is False
    assert section["auto_correct"] is True
    # Master toggle still wins for the dispatch path.
    assert is_enabled(root) is False


# ---------------------------------------------------------------------------
# Auto-correct checkbox visual semantics (Phase 4.6)
# ---------------------------------------------------------------------------


def test_set_validator_status_unchecks_disabled_checkbox(tmp_path):
    """When validator becomes unavailable, the auto-correct checkbox
    must be both UNCHECKED and disabled — never "checked + greyed".
    The stored preference is preserved so a later "available" call
    restores it.
    """
    from PySide6.QtWidgets import QApplication
    from unittest.mock import MagicMock
    from procedo.studio.dock.chat_panel import ChatPanel

    app = QApplication.instance() or QApplication([])  # noqa: F841

    panel = ChatPanel(main_window=MagicMock())
    # Operator's stored preference: checked.
    panel.set_auto_correct_enabled(True)
    assert panel.auto_correct_checkbox.isChecked() is True

    # Validator becomes unavailable → unchecked + disabled.
    panel.set_validator_status(available=False, reason="disabled in project settings (test)")
    assert panel.auto_correct_checkbox.isEnabled() is False
    assert panel.auto_correct_checkbox.isChecked() is False
    # Stored preference preserved.
    assert panel._stored_auto_correct is True

    # Validator becomes available again → enabled + restored to stored value.
    panel.set_validator_status(available=True)
    assert panel.auto_correct_checkbox.isEnabled() is True
    assert panel.auto_correct_checkbox.isChecked() is True


def test_validate_procedure_button_hidden_when_wheel_unavailable(monkeypatch, tmp_path):
    """Phase 5.1: the rules_packager_base `validate_procedure` validator
    is gated on `pack_parsers.is_available()` — i.e. the wheel imports
    cleanly. When the wheel is missing, the pack-shipped validator must
    hide; the pack-independent `core.check_python_syntax` keeps showing.
    """
    from PySide6.QtWidgets import QApplication, QVBoxLayout
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    app = QApplication.instance() or QApplication([])  # noqa: F841

    from procedo.studio.core.task_config import TaskConfigManager
    from procedo.studio.core.validators_registry import (
        ensure_builtins_registered,
        unregister_all,
    )
    from procedo.studio.pack_ops import pack_parsers
    from procedo.studio.tabs.base_tab import BaseTab

    unregister_all()
    ensure_builtins_registered()

    # Wheel-unavailable → pack-shipped validators must hide.
    monkeypatch.setattr(
        pack_parsers,
        "is_available",
        lambda project_root=None: (False, "rules_packager_base not in this venv"),
    )

    tmp = tmp_path
    cfg = tmp / "config"
    cfg.mkdir()
    import json as _json

    (cfg / "config.json").write_text(
        _json.dumps(
            {
                "manifest": {"name": "test"},
                "validator_loop": {"enabled": True},
                "workflows": {
                    "text_json": {
                        "validators": [
                            {"id": "rules_packager_base.validate_procedure", "enabled": True},
                            {"id": "core.check_python_syntax", "enabled": True},
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    manager = TaskConfigManager(tmp / "no_fallback.json", project_root=tmp)
    mw = MagicMock()
    mw.task_config_manager = manager
    mw.project_manager = SimpleNamespace(project_root=tmp)
    tab = BaseTab(mw)
    tab.tab_id = "text_json"
    QVBoxLayout(tab).addWidget(tab._create_action_bar())
    layout = tab.action_bar.file_layout

    button_ids = []
    from PySide6.QtWidgets import QPushButton

    for i in range(layout.count()):
        w = layout.itemAt(i).widget()
        if isinstance(w, QPushButton) and w.property("validator_id"):
            button_ids.append(w.property("validator_id"))

    # Wheel-dependent validator hidden when wheel can't import.
    assert "rules_packager_base.validate_procedure" not in button_ids
    # Self-contained validator (py_compile syntax) doesn't depend on
    # the wheel — keeps showing.
    assert "core.check_python_syntax" in button_ids


def test_all_validator_buttons_hidden_when_master_toggle_off(tmp_path):
    """Master toggle OFF (validator_loop.enabled=false) hides ALL
    validator buttons regardless of which specific validator is
    listed."""
    from PySide6.QtWidgets import QApplication, QVBoxLayout
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from procedo.studio.core.task_config import TaskConfigManager
    from procedo.studio.core.validators_registry import (
        ensure_builtins_registered,
        unregister_all,
    )
    from procedo.studio.tabs.base_tab import BaseTab

    app = QApplication.instance() or QApplication([])  # noqa: F841
    unregister_all()
    ensure_builtins_registered()

    import json as _json

    tmp = tmp_path
    cfg = tmp / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(
        _json.dumps(
            {
                "manifest": {"name": "test"},
                "validator_loop": {"enabled": False},
                "workflows": {
                    "text_json": {
                        "validators": [
                            {"id": "rules_packager_base.validate_procedure", "enabled": True},
                            {"id": "core.check_python_syntax", "enabled": True},
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    manager = TaskConfigManager(tmp / "no_fallback.json", project_root=tmp)
    mw = MagicMock()
    mw.task_config_manager = manager
    mw.project_manager = SimpleNamespace(project_root=tmp)
    tab = BaseTab(mw)
    tab.tab_id = "text_json"
    QVBoxLayout(tab).addWidget(tab._create_action_bar())
    assert tab.validator_buttons.rebuild() == 0


def test_chat_panel_toggle_writes_auto_correct_not_enabled(tmp_path):
    """The chat panel's checkbox toggle must persist under the
    ``auto_correct`` key, NOT ``enabled`` (which is reserved for the
    Settings master toggle)."""
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from procedo.studio.dock.chat_panel import ChatPanel

    app = QApplication.instance() or QApplication([])  # noqa: F841

    root = _project(tmp_path)
    panel = ChatPanel(main_window=MagicMock())
    panel._current_tab_context = SimpleNamespace(project_manager=SimpleNamespace(project_root=root))
    # Trigger the toggled handler directly.
    panel._on_auto_correct_toggled(False)

    on_disk = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    section = on_disk[SECTION_NAME]
    assert section["auto_correct"] is False
    # Master enable key must NOT have been touched.
    assert "enabled" not in section


def test_the_setting_is_read_once_until_the_config_may_have_changed(tmp_path, monkeypatch):
    """Every view asks it: read once, then kept — read again after ``forget`` (the window's
    config watcher, a project open) and after a save here."""
    from procedo.studio.llm import validator_loop_settings

    config_manager = validator_loop_settings.config_manager

    root = _project(tmp_path, {"enabled": True})
    reads = []
    real = config_manager.read_config_section
    monkeypatch.setattr(
        config_manager,
        "read_config_section",
        lambda *a, **k: reads.append(1) or real(*a, **k),
    )
    validator_loop_settings.forget()
    assert is_enabled(root) and is_enabled(root) and len(reads) == 1
    (root / "config" / "config.json").write_text(
        json.dumps({"validator_loop": {"enabled": False}}), encoding="utf-8"
    )
    validator_loop_settings.forget(root)
    assert is_enabled(root) is False and len(reads) == 2
    save_setting(root, "enabled", True)
    assert is_enabled(root) is True
