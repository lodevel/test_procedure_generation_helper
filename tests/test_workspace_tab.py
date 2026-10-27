"""Studio's left column (WorkspaceTab, boards S1, StudioLeftTabs): its switch, its rows (T / J / C states), the open
test's highlight, the search, the New test button and the context menu's actions."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QLabel,
    QLineEdit,
    QMenu,
)

from procedo.core import studio_launch_env  # noqa: E402
from procedo.ui.badge_delegate import BADGES_ROLE  # noqa: E402
from procedo.ui.ui_indicators import FORMAT_BADGE  # noqa: E402
from procedo.studio.core.project_manager import TestFolderInfo as FolderInfo  # noqa: E402
from procedo.studio.tabs import workspace_tab  # noqa: E402
from procedo.studio.tabs.workspace_tab import LEGEND, WorkspaceTab, artifact_states  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _folders(root: Path) -> list[FolderInfo]:
    return [
        FolderInfo(root / "Alpha", "Alpha", has_text=True, has_json=True, has_code=True),
        FolderInfo(root / "Beta", "Beta", has_text=True),
        FolderInfo(root / "Gamma", "Gamma"),
    ]


@pytest.fixture
def tab(qapp, tmp_path, monkeypatch):
    stale = {tmp_path / "Alpha": ("procedure.json",)}
    monkeypatch.setattr(
        workspace_tab.test_sync, "stale_artifacts", lambda path, _patterns: stale.get(path, ())
    )
    pm = SimpleNamespace(
        project_root=None,
        enumerate_test_folders=lambda: _folders(tmp_path),
        get_equipment_patterns=lambda: [],
    )
    widget = WorkspaceTab(SimpleNamespace(project_manager=pm))
    widget.resize(300, 400)
    widget.tmp = tmp_path
    widget.pm = pm
    yield widget
    widget.close()
    widget.deleteLater()


def _load(tab, monkeypatch, indicators: str) -> dict:
    monkeypatch.setenv(studio_launch_env.APP_INDICATORS, indicators)
    tab.pm.project_root = tab.tmp
    tab.refresh()
    return {
        tab.test_list.item(i).data(Qt.UserRole).name: tab.test_list.item(i)
        for i in range(tab.test_list.count())
    }


def test_artifact_states_map_presence_and_sync():
    full = FolderInfo(Path("x"), "x", has_text=True, has_json=True, has_code=True)
    text_only = FolderInfo(Path("y"), "y", has_text=True)
    every = ("procedure.json", "test.py", "procedure_text.md")  # the session flag alone
    assert artifact_states(full, ()) == [
        ("T", "Text", "present"),
        ("J", "JSON", "present"),
        ("C", "Code", "present"),
    ]
    assert artifact_states(full, ("test.py",)) == [  # only the stale artifact
        ("T", "Text", "present"),
        ("J", "JSON", "present"),
        ("C", "Code", "out of sync"),
    ]
    assert artifact_states(full, every) == [
        ("T", "Text", "out of sync"),
        ("J", "JSON", "out of sync"),
        ("C", "Code", "out of sync"),
    ]
    assert artifact_states(text_only, every) == [
        ("T", "Text", "out of sync"),
        ("J", "JSON", "missing"),
        ("C", "Code", "missing"),
    ]


#: A text test's format pill, before its squares (board StudioCode).
TXT = FORMAT_BADGE["text"]


def test_badge_rows_show_t_j_c_squares(tab, monkeypatch):
    rows = _load(tab, monkeypatch, "badges")
    assert [rows[n].text() for n in ("Alpha", "Beta", "Gamma")] == ["Alpha", "Beta", "Gamma"]
    assert rows["Alpha"].data(BADGES_ROLE) == [
        TXT,
        ("T", "success"),
        ("J", "warning"),
        ("C", "success"),
    ]
    assert rows["Alpha"].toolTip() == "Text: present · JSON: out of sync · Code: present"
    assert rows["Beta"].data(BADGES_ROLE) == [
        TXT,
        ("T", "success"),
        ("J", "missing"),
        ("C", "missing"),
    ]
    assert rows["Gamma"].data(BADGES_ROLE) == [
        TXT,
        ("T", "missing"),
        ("J", "missing"),
        ("C", "missing"),
    ]
    assert rows["Beta"].toolTip() == "Text: present · JSON: missing · Code: missing"


def test_classic_rows_keep_the_marks_in_the_text(tab, monkeypatch):
    rows = _load(tab, monkeypatch, "classic")
    assert rows["Alpha"].text() == "⚠️ Alpha  [T/J/C]"
    assert rows["Beta"].text() == "Beta  [T]"
    assert rows["Gamma"].text() == "Gamma  [empty]"
    assert all(row.data(BADGES_ROLE) == [TXT] for row in rows.values())  # the format: a pill


def test_a_switch_shows_tests_or_shared_phases_each_with_its_own_new_button(tab, monkeypatch):
    """Board StudioLeftTabs: "Tests (N) | Shared phases (N)" at the top, one list at a time; the
    legend, the search and "+ New test…" only on Tests, "+ New shared phase…" only on Shared
    phases; a test opened shows Tests."""
    _load(tab, monkeypatch, "badges")
    assert tab.tests_tab.text() == "Tests (3)" and tab.phases_tab.text() == "Shared phases"
    tab.shared_phases._show([])  # the project's list read: its count
    assert tab.phases_tab.text() == "Shared phases (0)"
    assert LEGEND == "T text · J JSON · C code"
    tests, phases = tab.lists.widget(0), tab.lists.widget(1)
    assert [label for label in tests.findChildren(QLabel) if label.text() == LEGEND]
    assert tab.search_bar in tests.findChildren(QLineEdit) and tab.new_test_btn.parent() is tests
    assert tab.shared_phases.new_button.parent() is phases
    assert tab.shared_phases.new_button.text() == "+ New shared phase…"
    assert tab.tests_tab.isChecked() and tab.lists.currentWidget() is tests
    tab.phases_tab.click()
    assert tab.lists.currentWidget() is phases and not tab.tests_tab.isChecked()
    tab.set_opened_test(None)  # nothing open: the user's choice stays
    assert tab.lists.currentWidget() is phases
    tab.set_opened_test(tab.tmp / "Beta")
    assert tab.lists.currentWidget() is tests and tab.tests_tab.isChecked()


def test_the_list_is_benchs_test_list(tab):
    assert tab.test_list.objectName() == "testList"
    assert tab.test_list.property("marks") == "open"
    assert tab.test_list.selectionMode() == QAbstractItemView.SelectionMode.NoSelection


def test_open_test_is_the_only_selected_row_and_semibold(tab, monkeypatch):
    rows = _load(tab, monkeypatch, "badges")
    tab.set_opened_test(tab.tmp / "Beta")
    assert [n for n, row in rows.items() if row.isSelected()] == ["Beta"]
    assert rows["Beta"].data(Qt.FontRole).weight() == QFont.Weight.DemiBold
    assert rows["Alpha"].data(Qt.FontRole) is None
    rows = _load(tab, monkeypatch, "badges")  # a reload (after a save) keeps the highlight
    assert [n for n, row in rows.items() if row.isSelected()] == ["Beta"]
    tab.set_opened_test(None)
    assert not any(row.isSelected() for row in rows.values())


def test_search_hides_rows_by_folder_name(tab, monkeypatch):
    rows = _load(tab, monkeypatch, "classic")  # the text carries marks: the name is matched
    tab.search_bar.setText("ET")
    assert [n for n, row in rows.items() if not row.isHidden()] == ["Beta"]
    tab.search_bar.setText("empty")
    assert all(row.isHidden() for row in rows.values())
    tab.search_bar.clear()
    assert not any(row.isHidden() for row in rows.values())


def test_new_test_and_new_shared_phase_need_a_project(tab, monkeypatch):
    assert tab.new_test_btn.text() == "+ New test…"
    tab.refresh()
    assert not tab.new_test_btn.isEnabled() and not tab.shared_phases.new_button.isEnabled()
    _load(tab, monkeypatch, "badges")
    assert tab.new_test_btn.isEnabled() and tab.shared_phases.new_button.isEnabled()


def test_double_click_opens(tab, monkeypatch):
    rows = _load(tab, monkeypatch, "badges")
    opened = []
    tab.test_opened.connect(opened.append)
    tab.test_list.itemDoubleClicked.emit(rows["Gamma"])
    assert opened == [tab.tmp / "Gamma"]


def _choose(monkeypatch, text: str, seen: dict | None = None) -> None:
    """The context menu picks *text* instead of opening (*seen*: each entry's enabled state)."""

    class Menu(QMenu):
        def exec(self, *_args):
            actions = {a.text(): a for a in self.actions() if a.text()}
            if seen is not None:
                seen.update({name: a.isEnabled() for name, a in actions.items()})
            return actions[text]

    monkeypatch.setattr(workspace_tab, "QMenu", Menu)


def _menu_on(tab, name: str) -> None:
    tab.show()
    item = next(
        tab.test_list.item(i)
        for i in range(tab.test_list.count())
        if tab.test_list.item(i).data(Qt.UserRole).name == name
    )
    tab._on_context_menu(tab.test_list.visualItemRect(item).center())


def test_context_menu_open(tab, monkeypatch):
    _load(tab, monkeypatch, "badges")
    opened, seen = [], {}
    tab.test_opened.connect(opened.append)
    _choose(monkeypatch, "Open", seen)
    _menu_on(tab, "Beta")
    assert opened == [tab.tmp / "Beta"]
    assert seen == {
        "Open": True,
        "Mark Procedure In Sync": False,
        "Rename Test…": True,
        "Delete Test…": True,
    }


def test_context_menu_mark_in_sync_unloaded_then_loaded(tab, monkeypatch):
    _load(tab, monkeypatch, "badges")
    marked, loaded = [], []
    monkeypatch.setattr(
        workspace_tab.test_sync, "mark_in_sync", lambda path, _patterns: marked.append(path)
    )
    tab.request_mark_loaded_in_sync.connect(lambda: loaded.append(True))
    _choose(monkeypatch, "Mark Procedure In Sync")
    _menu_on(tab, "Alpha")
    assert marked == [tab.tmp / "Alpha"] and not loaded
    tab.set_opened_test(tab.tmp / "Alpha")
    _menu_on(tab, "Alpha")
    assert loaded == [True] and marked == [tab.tmp / "Alpha"]


@pytest.mark.parametrize(
    "text, handler", [("Rename Test…", "_rename_test"), ("Delete Test…", "_delete_test")]
)
def test_context_menu_rename_and_delete(tab, monkeypatch, text, handler):
    _load(tab, monkeypatch, "badges")
    calls = []
    monkeypatch.setattr(WorkspaceTab, handler, lambda self, path: calls.append(path))
    _choose(monkeypatch, text)
    _menu_on(tab, "Gamma")
    assert calls == [tab.tmp / "Gamma"]


def test_refresh_opened_test_rereads_only_the_open_row(tab, monkeypatch):
    """A save or an acknowledgment changes the open test alone: its row is re-read from disk,
    the others are not listed again."""
    rows = _load(tab, monkeypatch, "badges")
    beta = tab.tmp / "Beta"
    beta.mkdir()
    for name in ("procedure_text.md", "procedure.json"):
        (beta / name).write_text("x", encoding="utf-8")
    tab.set_opened_test(beta)
    listed = []
    tab.pm.enumerate_test_folders = lambda: listed.append(1) or _folders(tab.tmp)
    monkeypatch.setattr(
        workspace_tab.test_sync,
        "stale_artifacts",
        lambda path, _patterns: ("procedure_text.md",) if path == beta else (),
    )
    tab.refresh_opened_test()
    assert listed == []
    assert rows["Beta"].data(BADGES_ROLE) == [
        TXT,
        ("T", "warning"),
        ("J", "success"),
        ("C", "missing"),
    ]
    assert rows["Alpha"].data(BADGES_ROLE) == [
        TXT,
        ("T", "success"),
        ("J", "warning"),
        ("C", "success"),
    ]


def test_refresh_opened_test_lists_again_without_its_folder(tab, monkeypatch):
    _load(tab, monkeypatch, "badges")
    tab.set_opened_test(tab.tmp / "Gamma")  # listed, but its folder is gone
    listed = []
    tab.pm.enumerate_test_folders = lambda: listed.append(1) or _folders(tab.tmp)
    tab.refresh_opened_test()
    assert listed == [1]
