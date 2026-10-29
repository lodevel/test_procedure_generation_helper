"""Studio's left column (board StudioLeftTabs): a segmented switch "Tests (N) | Shared phases
(N)" at its top shows one list at a time, the one that follows what is open in the middle (a
test: Tests; a file of shared phases: Shared phases), the user free to switch.

Tests (board S1): the T / J / C legend, the search, one compact row per test (its name, its
TXT / HTF format as Bench's lists show it (board StudioCode), then the state of its text, JSON
and code), the open test highlighted, "+ New test…" at the bottom (board StudioNew: a text or a
Python test). Double-click opens a test; its context menu opens it, marks it in sync, renames or
deletes it. Shared phases: the project's (``widgets.shared_phases_panel``), "+ New shared
phase…" at the bottom.

The rows are Bench's test rows (``#testList``, its ``BadgeDelegate``); Bench's Indicators setting
(``PROCEDO_APP_INDICATORS``) shows the states as squares (Badges) or as marks in the text (Pictos).
"""

import logging
import os
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from procedo.core import studio_launch_env
from procedo.core.off_gui import run_off_gui
from procedo.core.starter_test import sanitize_test_id
from procedo.core.paths import exists_quiet, is_dir_quiet, unreachable
from procedo.core.test_format import CODE, TEXT
from procedo.ui import ui_indicators, ui_kit
from procedo.ui.badge_delegate import BADGES_ROLE, BadgeDelegate
from procedo.ui.segmented import segment, segmented

from .base_tab import BaseTab
from ..core import project_open, test_folder_ops, test_sync
from ..core.artifact_manager import ArtifactManager
from ..core.project_manager import TestFolderInfo
from ..dialogs.new_test_dialog import NewTestDialog
from ..widgets.shared_phases_panel import SharedPhasesPanel

log = logging.getLogger(__name__)

#: The header's legend of the row letters.
LEGEND = "T text · J JSON · C code"
#: The column's two lists, in the switch's order.
TESTS_LIST, PHASES_LIST = 0, 1
#: A row's artifacts: letter, name, the TestFolderInfo flag that says it is there, its file.
_ARTIFACTS = (
    ("T", "Text", "has_text", ArtifactManager.PROCEDURE_TEXT_NAME),
    ("J", "JSON", "has_json", ArtifactManager.PROCEDURE_JSON_NAME),
    ("C", "Code", "has_code", ArtifactManager.TEST_CODE_NAME),
)
#: An artifact's state and its square's tone: there, there but not acknowledged as in sync with
#: the others (Mark Procedure In Sync clears it), not there. Which artifact is out of sync comes
#: from core.test_sync.stale_artifacts: the file(s) changed since the acknowledged baseline, or
#: every one when the session only says "out of sync" without a file to show for it.
_TONES = {"present": "success", "out of sync": "warning", "missing": "missing"}
_ROW_HEIGHT = 31
_SQUARE = 17


def artifact_states(info, stale: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """``(letter, name, state)`` of the test's text, JSON and code (states: :data:`_TONES`);
    ``stale``: the file names test_sync.stale_artifacts gives."""
    return [
        (
            letter,
            name,
            ("out of sync" if file in stale else "present") if getattr(info, flag) else "missing",
        )
        for letter, name, flag, file in _ARTIFACTS
    ]


def _show_state(item: QListWidgetItem, info, stale: tuple[str, ...], badges: bool) -> None:
    """The row of one test: its name, its format and its artifacts' states (squares, or marks in
    the text)."""
    states = artifact_states(info, stale)
    item.setToolTip(" · ".join(f"{name}: {state}" for _letter, name, state in states))
    fmt = ui_indicators.FORMAT_BADGE[CODE if info.is_code else TEXT]
    if badges:
        item.setText(info.name)
        item.setData(
            BADGES_ROLE, [fmt, *((letter, _TONES[state]) for letter, _name, state in states)]
        )
        return
    item.setData(BADGES_ROLE, [fmt])
    present = [letter for letter, _name, state in states if state != "missing"]
    marks = f"[{'/'.join(present)}]" if present else "[empty]"
    item.setText(f"{'⚠️ ' if stale else ''}{info.name}  {marks}")


def _uses_badges() -> bool:
    return ui_indicators.uses_badges(os.environ.get(studio_launch_env.APP_INDICATORS))


def _padded(widget, left: int, top: int, right: int, bottom: int) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setContentsMargins(left, top, right, bottom)
    layout.addWidget(widget)
    return layout


def _list_page(button: QPushButton, *rows: QVBoxLayout) -> QWidget:
    """One of the column's lists: its *rows* (the last one takes the height), the hairline,
    its own *button* at the bottom."""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    for row in rows:
        layout.addLayout(row)
    layout.setStretch(len(rows) - 1, 1)
    layout.addWidget(ui_kit.row_rule())
    bottom = ui_kit.hbox(button, None)
    bottom.setContentsMargins(12, 10, 12, 10)
    layout.addLayout(bottom)
    return page


def _full_width(switch: QWidget) -> QWidget:
    """*switch* (``segmented``) across the column, its segments of one width."""
    track = switch.layout().itemAt(0).widget()
    for index in range(track.layout().count()):
        track.layout().setStretch(index, 1)
    switch.layout().setStretch(0, 1)
    switch.layout().setStretch(1, 0)
    return switch


class WorkspaceTab(BaseTab):
    """Procedo Studio's left column: the project's tests (open, rename, delete, create) | its
    shared phases."""

    test_opened = Signal(Path)  # Emitted when a test should be opened
    test_deleted = Signal(Path)  # Emitted when a test folder was deleted
    # Right-clicking the currently-loaded test → Mark Procedure In Sync.
    # The loaded test holds in-memory session_state that would overwrite
    # any disk-only write; main_window connects this signal to the
    # existing acknowledgment flow which handles both layers atomically.
    request_mark_loaded_in_sync = Signal()

    def _setup_ui(self):
        self._current_opened_test = None
        self._reading: object = None  # the tests read now (token): only its rows land
        self._reveal: Path | None = None  # a test just made: shown once the list is read
        self.tests_tab, self.phases_tab = segment("Tests"), segment("Shared phases")
        self._tabs = QButtonGroup(self)
        self._tabs.addButton(self.tests_tab, TESTS_LIST)
        self._tabs.addButton(self.phases_tab, PHASES_LIST)

        # Same semantics as Bench's test search: a case-insensitive substring of the
        # folder name, applied live.
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search tests…")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._apply_search_filter)

        # Bench's flat rows; the selection is the open test (a click does not move it).
        self.test_list = QListWidget()
        self.test_list.setObjectName("testList")
        self.test_list.setProperty("marks", "open")
        self.test_list.setItemDelegate(
            BadgeDelegate(self.test_list, row_height=_ROW_HEIGHT, square=_SQUARE)
        )
        self.test_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.test_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.test_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.test_list.setSpacing(1)
        self.test_list.itemDoubleClicked.connect(self._on_test_double_clicked)
        self.test_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.test_list.customContextMenuRequested.connect(self._on_context_menu)

        self.cue = ui_kit.LoadingCue()  # the tests being read (off the GUI thread)
        self.shared_phases = SharedPhasesPanel(self.main_window)
        self.shared_phases.loaded.connect(self._count_phases)

        self.new_test_btn = QPushButton("+ New test…")
        self.new_test_btn.clicked.connect(self._on_create_new_test)
        for button in (self.new_test_btn, self.shared_phases.new_button):
            button.setEnabled(False)  # until a project is open

        self.lists = QStackedWidget()
        self.lists.addWidget(
            _list_page(
                self.new_test_btn,
                _padded(ui_kit.note(LEGEND, wrap=False), 14, 0, 14, 6),
                _padded(self.search_bar, 12, 0, 12, 8),
                _padded(self.cue, 12, 0, 12, 4),
                _padded(self.test_list, 8, 0, 8, 8),
            )
        )
        self.lists.addWidget(
            _list_page(self.shared_phases.new_button, _padded(self.shared_phases, 8, 0, 8, 8))
        )
        self._tabs.idClicked.connect(self.lists.setCurrentIndex)
        self.show_list(TESTS_LIST)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(
            _padded(_full_width(segmented(self.tests_tab, self.phases_tab)), 12, 12, 12, 8)
        )
        layout.addWidget(self.lists, 1)

    def show_list(self, index: int) -> None:
        """Show the column's Tests (:data:`TESTS_LIST`) or Shared phases (:data:`PHASES_LIST`)."""
        self._tabs.button(index).setChecked(True)
        self.lists.setCurrentIndex(index)

    def _count_phases(self) -> None:
        """The tab's "Shared phases (N)"; no count while the list is being read."""
        panel = self.shared_phases
        self.phases_tab.setText(
            "Shared phases" if panel.cue.active else f"Shared phases ({len(panel.phases)})"
        )

    def _show_tests(self, rows: list[project_open.Row]) -> None:
        """The project's tests as read (*rows*), the scroll position kept."""
        self._reading = None
        self.cue.done()
        scroll = self.test_list.verticalScrollBar()
        top = scroll.value()
        self.test_list.clear()
        badges = _uses_badges()
        for info, stale in rows:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, info.path)
            _show_state(item, info, stale, badges)
            self.test_list.addItem(item)
        self.tests_tab.setText(f"Tests ({len(rows)})")
        self._update_opened_test_highlight()
        self._apply_search_filter()
        scroll.setValue(top)
        if self._reveal is not None:
            self._show_test(self._reveal)
            self._reveal = None

    def _read_tests(self) -> None:
        """The rows read again off the GUI thread (``project_open.test_rows``: on a share
        each test is a dozen round trips), "Reading the tests…" meanwhile; the rows of the
        last read asked for land, an earlier read's are dropped."""
        pm = self.project_manager
        token = self._reading = object()
        self.tests_tab.setText("Tests")
        self.cue.start("Reading the tests…")

        def read() -> tuple:
            try:
                return token, project_open.test_rows(pm)
            except Exception:  # logged, the list shows nothing
                log.exception("Reading the tests of %s failed", pm.project_root)
                return token, []

        run_off_gui(read, self._on_tests_read, name="project-read")

    def _on_tests_read(self, answer: tuple) -> None:
        token, rows = answer
        if token is self._reading:
            self._show_tests(rows)

    def _apply_search_filter(self) -> None:
        """Hide list items whose folder name doesn't contain the search
        text (case-insensitive). Empty search shows everything."""
        needle = self.search_bar.text().strip().lower()
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            item.setHidden(bool(needle) and needle not in item.data(Qt.UserRole).name.lower())

    def _show_test(self, path: Path):
        """Move the list's cursor to *path*'s row and scroll it into view."""
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            if item.data(Qt.UserRole) == path:
                self.test_list.setCurrentItem(item)
                self.test_list.scrollToItem(item)
                break

    def set_opened_test(self, path: Path):
        """Set the currently opened test, highlight it and scroll it into view (the Tests list
        shown when it is one)."""
        self._current_opened_test = path
        if path is not None:
            self.show_list(TESTS_LIST)
        self._update_opened_test_highlight()
        self._show_test(path)

    def _update_opened_test_highlight(self):
        """The open test's row selected (the stylesheet's blue) and its name semi-bold."""
        self.test_list.ensurePolished()
        bold = QFont(self.test_list.font())
        bold.setWeight(QFont.Weight.DemiBold)
        # Through the selection model: a NoSelection list ignores item.setSelected().
        selection = self.test_list.selectionModel()
        selection.clearSelection()
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            is_open = item.data(Qt.UserRole) == self._current_opened_test
            if is_open:
                selection.select(
                    self.test_list.indexFromItem(item), QItemSelectionModel.SelectionFlag.Select
                )
            item.setData(Qt.FontRole, bold if is_open else None)

    def _on_test_double_clicked(self, item: QListWidgetItem):
        """Handle test double-click to open."""
        path = item.data(Qt.UserRole)
        self.test_opened.emit(path)

    def _on_context_menu(self, position):
        """Show context menu for test list."""
        item = self.test_list.itemAt(position)
        if not item:
            return

        path = item.data(Qt.UserRole)
        if not path:
            return

        menu = QMenu(self)

        open_action = menu.addAction("Open")

        # Mark In Sync — disabled only when the test is already in
        # sync (nothing to baseline). The loaded-vs-unloaded split is
        # handled at click time: loaded goes through main_window's
        # acknowledgment flow (in-memory + disk atomically), unloaded
        # writes the hash baseline directly to .llm_session.json.
        out_of_sync = bool(self._stale_artifacts(path))
        is_loaded = path == self._current_opened_test
        mark_sync_action = menu.addAction("Mark Procedure In Sync")
        mark_sync_action.setEnabled(out_of_sync)
        if not out_of_sync:
            mark_sync_action.setToolTip("Already in sync — nothing to do.")
        elif is_loaded:
            mark_sync_action.setToolTip(
                "Acknowledge the loaded test's artifacts as coherent "
                "(confirms via dialog and updates the session state)."
            )
        else:
            mark_sync_action.setToolTip(
                "Acknowledge procedure_text.md / procedure.json / test.py "
                "as coherent. Rewrites the hash baseline in .llm_session.json."
            )

        rename_action = menu.addAction("Rename Test…")
        rename_action.setEnabled(not is_loaded)
        if is_loaded:
            rename_action.setToolTip(
                "Close the test first — the loaded editor session pins the old folder path."
            )
        else:
            rename_action.setToolTip(
                "Rename the folder and re-sync the test id (procedure_text.md "
                "header + procedure.json id), test_order and scenario entries. "
                "Past run reports keep the old name."
            )

        menu.addSeparator()
        delete_action = menu.addAction("Delete Test…")
        delete_action.setToolTip("Permanently delete this test folder and all its contents")

        action = menu.exec(self.test_list.mapToGlobal(position))

        if action == open_action:
            self.test_opened.emit(path)
        elif action == mark_sync_action:
            if is_loaded:
                self.request_mark_loaded_in_sync.emit()
            else:
                self._mark_test_in_sync(path)
        elif action == rename_action:
            self._rename_test(path)
        elif action == delete_action:
            self._delete_test(path)

    def _mark_test_in_sync(self, path: Path):
        """Re-baseline an UNLOADED test's session file (core.test_sync). The
        context menu disables this entry for the opened test, which stays
        under main_window control."""
        try:
            test_sync.mark_in_sync(path, self.project_manager.get_equipment_patterns())
        except (OSError, ValueError) as e:
            QMessageBox.critical(
                self, "Mark In Sync Failed", f"Could not mark {path.name} in sync:\n{e}"
            )
            return
        self.refresh()

    def _stale_artifacts(self, path: Path) -> tuple[str, ...]:
        return test_sync.stale_artifacts(path, self.project_manager.get_equipment_patterns())

    def _rename_test(self, path: Path):
        """Rename a test folder; the id, its sync state and the project's
        references follow (core.test_folder_ops). Only offered for a test that
        is not open — the context menu disables it for the open one."""
        raw, ok = QInputDialog.getText(
            self,
            "Rename Test",
            "New test name:",
            text=path.name,
        )
        if not ok or not raw.strip():
            return
        new_name = sanitize_test_id(raw.strip())
        if new_name == path.name:
            return
        pm = self.project_manager
        try:
            new_path, warnings = test_folder_ops.rename_test(
                pm.project_root,
                pm.get_tests_dir(),
                path,
                new_name,
                pm.get_equipment_patterns(),
            )
        except test_folder_ops.TestFolderError as e:
            QMessageBox.critical(self, "Rename Failed", str(e))
            return
        if warnings:
            QMessageBox.warning(
                self,
                "Renamed With Warnings",
                "The folder was renamed, but these could not follow "
                "(ids re-sync on the next save):\n\n" + "\n".join(warnings),
            )

        self.refresh()
        self._show_test(new_path)

    def _delete_test(self, path: Path):
        """Delete a test folder after confirmation (core.test_folder_ops)."""
        # Count files for the confirmation message
        if reason := unreachable(path):
            QMessageBox.warning(self, "Delete Test", reason)
            return
        files = list(path.iterdir()) if path.is_dir() else []
        file_count = len([f for f in files if f.is_file()])

        result = QMessageBox.warning(
            self,
            "Delete Test",
            f"Permanently delete test '{path.name}'?\n\n"
            f"This will remove the folder and all {file_count} file(s) inside it.\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if result != QMessageBox.Yes:
            return

        pm = self.project_manager
        try:
            test_folder_ops.delete_test(pm.get_tests_dir(), path)
        except test_folder_ops.TestFolderError as e:
            QMessageBox.critical(self, "Delete Failed", str(e))
            if (
                not exists_quiet(path) and unreachable(path) is None
            ):  # moved aside, partly deleted: it is gone as a test
                self.test_deleted.emit(path)
                self.refresh()
            return

        # Emit signal so main window can clear editors if needed
        self.test_deleted.emit(path)

        # Refresh the list
        self.refresh()

    def _on_create_new_test(self):
        """Create a new test folder (the New test window: its name, a text or a Python test)."""
        dialog = NewTestDialog(self.project_manager.get_tests_dir(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name

        path = self.project_manager.create_test_folder(name, dialog.active)
        if path:
            self._reveal = path  # its row scrolled into view once the list is read
            self.refresh()
            self.test_opened.emit(path)
        else:
            self.show_error(
                "Failed to Create Test",
                f"Could not create test folder '{name}'.\n"
                "It may already exist or the path is invalid.",
            )

    def refresh_opened_test(self):
        """Re-read the open test's row only: saving or acknowledging it changes no other test.
        The whole list when that row or its folder is not there."""
        path = self._current_opened_test
        if path is not None and is_dir_quiet(path):
            for i in range(self.test_list.count()):
                item = self.test_list.item(i)
                if item.data(Qt.UserRole) == path:
                    _show_state(
                        item, TestFolderInfo.of(path), self._stale_artifacts(path), _uses_badges()
                    )
                    return
        self.refresh()

    def refresh(self):
        """List the tests again (one made, renamed, deleted, marked in sync): read off the
        GUI thread. No project: the list empty, the buttons off."""
        if self.project_manager.project_root:
            self._read_tests()
        else:
            self._show_tests([])
        self._enable_buttons()

    def show_project(self, rows: list[project_open.Row]) -> None:
        """A project was opened and read (*rows*: its tests, ``project_open.read``): its
        tests listed from them, its shared phases read."""
        self._show_tests(rows)
        self._enable_buttons()
        self.shared_phases.load()

    def _enable_buttons(self) -> None:
        is_open = bool(self.project_manager.project_root)
        for button in (self.new_test_btn, self.shared_phases.new_button):
            button.setEnabled(is_open)
