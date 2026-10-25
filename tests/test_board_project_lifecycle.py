"""Exercise board and chat binding through real window startup and workers."""

import threading
import time

import pytest
from PySide6.QtTest import QTest

from .test_main_window_characterization import (
    _make_project,
    make_window as make_window,
    mbox as mbox,
    qapp as qapp,
)
from procedo.studio.core import odb_inspect
from procedo.studio.widgets.netlist_panel import NetlistPanel, _LIVE_WORKERS


def _until(app, predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert predicate()


def _board(name):
    return {"components": [{"refdes": name, "pins": [], "properties": {}}], "nets": [], "error": ""}


def test_cli_startup_discards_late_no_project_result(make_window, qapp, tmp_path, monkeypatch):
    root = _make_project(tmp_path / "project")
    release_empty = threading.Event()
    empty_started = threading.Event()

    def load(project):
        if project is None:
            empty_started.set()
            assert release_empty.wait(10)
            return {"components": [], "nets": [], "error": "No project is open."}
        assert project == root
        return _board("U_CURRENT")

    monkeypatch.setattr(odb_inspect, "load_board", load)
    window = make_window(project_root=root, test_name="t1")
    try:
        window.window().show()
        panel = window.text_only_tab._netlist_panel
        _until(qapp, lambda: bool(panel._components))
        assert window.project_manager.project_root == root
        assert empty_started.is_set()
        release_empty.set()
        _until(qapp, lambda: not _LIVE_WORKERS)
        assert panel._components == _board("U_CURRENT")["components"]
        assert "No project" not in panel._status.text()
    finally:
        release_empty.set()
        _until(qapp, lambda: not _LIVE_WORKERS)


def test_cli_startup_binds_chat_when_text_is_already_selected(
    make_window, qapp, tmp_path, monkeypatch
):
    root = _make_project(tmp_path / "project")
    (root / "board.tgz").write_bytes(b"synthetic archive")
    monkeypatch.setattr(odb_inspect, "load_board", lambda project: _board("U_CURRENT"))
    window = make_window(project_root=root, test_name="t1")
    try:
        window.window().show()
        _until(qapp, lambda: not _LIVE_WORKERS)
        assert window.view_stack.currentWidget() is window.text_only_tab
        chat = window.dock.chat_panel
        assert chat._current_tab_context is window.text_only_tab.tab_context
        assert not chat.board_data_checkbox.isHidden()
        assert chat.board_data_checkbox.isEnabled()
    finally:
        _until(qapp, lambda: not _LIVE_WORKERS)


@pytest.mark.parametrize("same_project", [False, True])
def test_latest_board_load_wins_even_when_older_worker_finishes_last(
    qapp, tmp_path, monkeypatch, same_project
):
    old_root = tmp_path / "old"
    new_root = old_root if same_project else tmp_path / "new"
    old_started = threading.Event()
    release_old = threading.Event()

    def load(project):
        if not old_started.is_set():
            old_started.set()
            assert release_old.wait(10)
            return _board("U_OLD")
        assert project == new_root
        return _board("U_NEW")

    monkeypatch.setattr(odb_inspect, "load_board", load)
    panel = NetlistPanel()
    try:
        panel.load(old_root)
        _until(qapp, old_started.is_set)
        panel.load(new_root)
        assert panel.loading.active and panel.loading.label.text() == "Loading board…"
        _until(qapp, lambda: bool(panel._components))
        assert panel._components == _board("U_NEW")["components"] and not panel.loading.active
        release_old.set()
        _until(qapp, lambda: not _LIVE_WORKERS)
        assert panel._project_root == new_root
        assert panel._comp_tree.topLevelItem(0).text(0) == "U_NEW"
        assert panel._components == _board("U_NEW")["components"]
    finally:
        release_old.set()
        _until(qapp, lambda: not _LIVE_WORKERS)
        panel.close()
