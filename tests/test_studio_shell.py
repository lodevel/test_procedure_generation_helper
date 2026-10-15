"""Procedo Studio's frame (workflow_editor/shell.py, redesign slice 2): the header's pills
follow the open test, the Sync pill acknowledges, the OpenCode pill's menu restarts the server,
Ctrl+1..4 and the segmented switch change the view, the footer names the project, rules, bundle
and validator and its links act, View hides the fixed columns, the window only hosts the
workspace."""

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QMessageBox, QStatusBar

import workflow_editor.main_window as mw_mod
from workflow_editor import shell

from .test_main_window_characterization import (
    _make_project, _open_project, make_window as make_window, mbox as mbox, qapp as qapp,
)


@pytest.fixture
def opened(make_window, tmp_path):
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    return w, root


def _pill(label):
    return label.text(), label.property("pill")


def test_pills_follow_the_open_test_and_save_all_counts_unsaved(opened):
    w, root = opened
    assert w.header.title.text() == "t1"
    assert w.header.subtitle.text() == "Procedo Studio · proj · 0 steps"
    assert [_pill(p) for p in w.artifact_pills.values()] == [
        ("Text ✓", "success"), ("JSON ✓", "success"), ("Code ✓", "success")]
    assert not w.save_all_btn.isEnabled() and w.save_all_btn.objectName() == ""

    w.json_code_tab.code_editor.setPlainText("def main():\n    return 2\n")

    assert _pill(w.artifact_pills["Code"]) == ("Code ● modified", "warning")
    assert w.save_all_btn.text() == "Save all · 1"
    assert w.save_all_btn.isEnabled() and w.save_all_btn.objectName() == "primaryButton"

    w.save_all_btn.click()

    assert "return 2" in (root / "tests" / "t1" / "test.py").read_text(encoding="utf-8")
    assert _pill(w.artifact_pills["Code"]) == ("Code ✓", "success")
    assert w.save_all_btn.text() == "Save all" and not w.save_all_btn.isEnabled()
    # test.py changed alone: the Sync pill asks to be acknowledged
    assert _pill(w.sync_pill) == ("⇄ Sync unconfirmed · acknowledge", "warning")


def test_the_header_names_the_project_before_a_test_is_open(make_window, tmp_path):
    w = make_window()
    _open_project(w, _make_project(tmp_path / "proj"))
    assert w.header.title.text() == "No test open"
    assert w.header.subtitle.text() == "Procedo Studio · proj"


def test_clicking_the_sync_pill_acknowledges(opened, mbox):
    w, _root = opened
    w.session_state.artifacts_in_sync = False
    w._update_status_indicators()
    mbox.question_answer = QMessageBox.StandardButton.Yes

    QTest.mouseClick(w.sync_pill, Qt.MouseButton.LeftButton)

    assert [t for t, _ in mbox.question] == ["Acknowledge Sync"]
    assert w.session_state.artifacts_in_sync is True
    assert _pill(w.sync_pill) == ("⇄ JSON ↔ Code in sync", "success")


def test_the_opencode_pill_menu_restarts_the_server_and_opens_llm_settings(make_window, monkeypatch):
    calls = []
    monkeypatch.setattr(mw_mod.StudioWorkspace, "_on_restart_backend", lambda self: calls.append("restart"))
    monkeypatch.setattr(mw_mod.StudioWorkspace, "_on_settings", lambda self: calls.append("settings"))
    w = make_window()
    menu = w.server_pill.menu()
    assert [a.text() for a in menu.actions()] == ["Restart server", "LLM settings…"]

    for action in menu.actions():
        action.trigger()

    assert calls == ["restart", "settings"]
    assert _pill(w.server_pill) == ("LLM off ▾", "neutral")  # no OpenCode backend here
    assert not hasattr(w.dock.chat_panel, "restart_btn")  # moved off the chat


def test_the_server_pill_shows_the_model_or_the_trouble(make_window):
    w = make_window()
    w._server_manager = type("SM", (), {"config": type("C", (), {"model": "openai/gpt-5.4"})(),
                                        "last_status": None, "stop": lambda self: None})()
    w._refresh_server_indicator(True)
    assert _pill(w.server_pill) == ("● OpenCode · openai/gpt-5.4 ▾", "success")
    w._refresh_server_indicator(False)
    assert _pill(w.server_pill) == ("● OpenCode · down ▾", "danger")
    w._server_recovering = True
    w._refresh_server_indicator(False)
    assert _pill(w.server_pill) == ("● OpenCode · recovering… ▾", "warning")


def test_ctrl_1_to_4_and_the_switch_change_the_view(opened):
    w, _root = opened
    shortcuts = {s.key().toString(): s for s in w.findChildren(QShortcut)}
    for n in (4, 2, 3, 1):
        shortcuts[QKeySequence(f"Ctrl+{n}").toString()].activated.emit()
        assert w.view_stack.currentIndex() == n - 1
        assert w.view_buttons.checkedId() == n - 1
        assert w.view_files.text() == shell.VIEWS[n - 1][1]

    w.view_buttons.button(2).click()
    assert w.view_stack.currentWidget() is w.json_code_tab
    assert w.save_action.text() == "&Save JSON–Code"


def test_footer_names_project_rules_bundle_validator(opened):
    w, root = opened
    rules = root / "bundle" / "rules"
    rules.mkdir()
    for name in ("a.md", "b.md"):
        (rules / name).write_text("# rule\n", encoding="utf-8")
    (root / "bundle" / "defaults.json").write_text("{}", encoding="utf-8")  # a complete bundle
    config = root / "config" / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["validator_loop"] = {"enabled": False, "max_attempts": 5}
    config.write_text(json.dumps(data), encoding="utf-8")
    assert w.project_manager.set_rules_root(rules)

    w._update_project_rules_indicators()

    assert w.project_link.text() == "Project: proj"
    assert str(root) in w.project_link.toolTip()  # full paths only in the tooltips
    assert w.rules_link.text() == f"Rules: {rules.relative_to(root)} · 2 docs"
    assert str(rules) in w.rules_link.toolTip()
    assert w.bundle_label.text() == "Bundle: b 1.0.0"
    assert w.validator_label.text() == "Validator: off · 5 attempts"


def test_footer_links_open_a_project_and_pick_the_rules(make_window, monkeypatch):
    w = make_window()
    opened = []
    monkeypatch.setattr(w, "_on_open_project", lambda: opened.append(True))
    asked = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda parent, title, *a: asked.append(title) or ""))

    w.project_link.click()
    w.rules_link.click()

    assert opened == [True]
    assert asked == ["Select Rules Folder"]


def test_view_hides_and_shows_the_fixed_columns(make_window):
    w = make_window()
    w.toggle_workspace_action.setChecked(False)
    w.toggle_dock_action.setChecked(False)
    assert w.workspace_column.isHidden() and w.assistant_column.isHidden()
    w.toggle_workspace_action.setChecked(True)
    w.toggle_dock_action.setChecked(True)
    assert not w.workspace_column.isHidden() and not w.assistant_column.isHidden()


def test_the_window_only_hosts_the_workspace(make_window):
    w = make_window()
    window = w.window()
    assert window.windowTitle() == "Procedo Studio"
    assert not window.windowIcon().isNull()
    assert window.menuBar() is w.menu_bar
    assert window.centralWidget() is w
    assert window.findChildren(QStatusBar) == []  # the status line is the workspace's footer
