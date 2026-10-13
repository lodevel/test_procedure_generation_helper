"""
Workspace Tab - Project and test folder selection.

Implements Section 9.1 of the spec.
"""

from pathlib import Path
import logging
import os
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QGroupBox, QFrame, QInputDialog,
    QLineEdit, QMenu, QMessageBox
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QBrush, QPalette

from .base_tab import BaseTab
from ..badge_delegate import BADGES_ROLE, CURRENT_ROLE, BadgeDelegate
from ..core import ArtifactType
from ..core import test_folder_ops, test_sync
from ..theme import (
    sync_warning_color, empty_test_color, ready_test_color,
    default_text_color, selected_test_bg, selected_test_fg,
)
from .. import theme


def _modern_workspace_enabled() -> bool:
    return os.environ.get("TPG_APP_LAYOUT") == "modern_workspace"


def _badge_indicators_enabled() -> bool:
    return os.environ.get("TPG_APP_INDICATORS") == "badges"


def _modern_card_list_stylesheet() -> str:
    if theme.is_dark():
        bg = "#1b1f27"
        item_bg = "#252b35"
        item_hover = "#2c3440"
        selected_bg = "#314b6f"
        border = "#465263"
        selected_border = "#7ab8ff"
        text = "#f1f5f9"
    else:
        bg = "#f5f7fb"
        item_bg = "#ffffff"
        item_hover = "#eef4ff"
        selected_bg = "#dcecff"
        border = "#d5dce8"
        selected_border = "#2f7dd3"
        text = "#1f2937"
    return f"""
        QListWidget {{
            background-color: {bg};
            border: 1px solid {border};
            border-radius: 9px;
            padding: 4px;
            color: {text};
        }}
        QListWidget::item {{
            background-color: {item_bg};
            border: 1px solid {border};
            border-radius: 8px;
            margin: 3px 1px;
            padding: 8px;
            color: {text};
        }}
        QListWidget::item:hover {{
            background-color: {item_hover};
        }}
        QListWidget::item:selected {{
            background-color: {selected_bg};
            border-color: {selected_border};
            color: {text};
        }}
    """


class WorkspaceTab(BaseTab):
    """
    Workspace tab for project and test folder management.
    
    Features:
    - Project root selection
    - Test folder listing with artifact indicators
    - Actions based on detected artifacts
    """
    
    # Signals
    test_selected = Signal(Path)  # Emitted when a test is selected
    test_opened = Signal(Path)    # Emitted when a test should be opened
    test_deleted = Signal(Path)   # Emitted when a test folder was deleted
    # Right-clicking the currently-loaded test → Mark Procedure In Sync.
    # The loaded test holds in-memory session_state that would overwrite
    # any disk-only write; main_window connects this signal to the
    # existing acknowledgment flow which handles both layers atomically.
    request_mark_loaded_in_sync = Signal()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Test list only - project selection is in File menu
        list_group = QGroupBox("Tests")
        list_layout = QVBoxLayout(list_group)

        # Search bar — same semantics as the main GUI's test list filter:
        # case-insensitive substring on the folder name, applied live.
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search tests...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_search_changed)
        list_layout.addWidget(self.search_bar)

        self.test_list = QListWidget()
        self.test_list.setObjectName("workflowTestCards")
        self.test_list.setItemDelegate(BadgeDelegate(self.test_list))
        # The custom item delegate draws selection from the palette Highlight
        # role; under the modern card stylesheet the default highlight washes
        # out, so set a clearly-visible one. Same #0078d4/white the main GUI
        # test list uses — which is exactly why ITS selection is visible.
        pal = self.test_list.palette()
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#0078d4"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
        self.test_list.setPalette(pal)
        if _modern_workspace_enabled():
            self.test_list.setSpacing(4)
            self.test_list.setStyleSheet(_modern_card_list_stylesheet())
        self.test_list.itemSelectionChanged.connect(self._on_test_selection_changed)
        self.test_list.itemDoubleClicked.connect(self._on_test_double_clicked)
        self.test_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.test_list.customContextMenuRequested.connect(self._on_context_menu)
        list_layout.addWidget(self.test_list)
        
        # New test button
        self.new_test_btn = QPushButton("Create New Test...")
        self.new_test_btn.clicked.connect(self._on_create_new_test)
        self.new_test_btn.setEnabled(False)
        list_layout.addWidget(self.new_test_btn)
        
        layout.addWidget(list_group, stretch=1)
        
        # Track currently opened test
        self._current_opened_test = None
    
    def _load_test_list(self):
        """Load test folders into the list."""
        self.test_list.clear()
        
        folders = self.project_manager.enumerate_test_folders()
        
        for info in folders:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, info.path)
            item.setData(Qt.UserRole + 1, info)  # Store info for later
            if _modern_workspace_enabled():
                item.setSizeHint(QSize(0, 52))
            
            # Check sync status from session file
            out_of_sync = self._out_of_sync(info.path)
            
            # Build display text with indicators
            indicators = []
            if info.has_text:
                indicators.append("T")
            if info.has_json:
                indicators.append("J")
            if info.has_code:
                indicators.append("C")
            
            if _badge_indicators_enabled():
                badges = []
                if out_of_sync:
                    badges.append(("OUT OF SYNC", "warning"))
                if info.has_text:
                    badges.append(("TEXT", "muted"))
                if info.has_json:
                    badges.append(("JSON", "purple"))
                if info.has_code:
                    badges.append(("CODE", "success"))
                if not indicators:
                    badges.append(("EMPTY", "muted"))
                item.setText(info.name)
                item.setData(BADGES_ROLE, badges)
            else:
                indicator_str = f"[{'/'.join(indicators)}]" if indicators else "[empty]"
                sync_flag = "\u26a0\ufe0f " if out_of_sync else ""
                item.setText(f"{sync_flag}{info.name}  {indicator_str}")
                item.setData(BADGES_ROLE, [])
            
            # Color based on state
            if out_of_sync:
                item.setForeground(sync_warning_color())  # Orange for out-of-sync
            elif not indicators:
                item.setForeground(empty_test_color())
            elif info.has_json and info.has_code:
                item.setForeground(ready_test_color())
            
            self.test_list.addItem(item)

        self._apply_search_filter()

    def _on_search_changed(self, text: str) -> None:
        del text  # read live in _apply_search_filter
        self._apply_search_filter()

    def _apply_search_filter(self) -> None:
        """Hide list items whose folder name doesn't contain the search
        text (case-insensitive). Empty search shows everything."""
        needle = self.search_bar.text().strip().lower()
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            path = item.data(Qt.UserRole)
            name = path.name if path else item.text()
            item.setHidden(bool(needle) and needle not in name.lower())

    def _select_test_by_path(self, path: Path):
        """Select a test in the list by path."""
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            if item.data(Qt.UserRole) == path:
                self.test_list.setCurrentItem(item)
                break
    
    
    def set_opened_test(self, path: Path):
        """Set the currently opened test and highlight it."""
        self._current_opened_test = path
        self._update_opened_test_highlight()
    
    def _update_opened_test_highlight(self):
        """Update highlight for currently opened test."""
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            item_path = item.data(Qt.UserRole)
            
            # Check if this is the opened test
            if item_path == self._current_opened_test:
                # Highlight opened test. setBackground/Foreground only show in
                # classic mode — the modern card stylesheet overrides them — so
                # also flag CURRENT_ROLE for the delegate to frame the card.
                item.setBackground(QBrush(selected_test_bg()))  # Steel blue
                item.setForeground(selected_test_fg())  # White text
                item.setData(CURRENT_ROLE, True)
            else:
                # Restore original colors for non-opened tests
                item.setData(CURRENT_ROLE, False)
                item.setBackground(QBrush())
                info = item.data(Qt.UserRole + 1)
                out_of_sync = self._out_of_sync(item_path)
                if out_of_sync:
                    item.setForeground(sync_warning_color())  # Orange
                elif info:
                    if not info.has_text and not info.has_json and not info.has_code:
                        item.setForeground(empty_test_color())
                    elif info.has_json and info.has_code:
                        item.setForeground(ready_test_color())
                    else:
                        item.setForeground(default_text_color())
    
    def _on_test_selection_changed(self):
        """Handle test selection change."""
        items = self.test_list.selectedItems()
        if items:
            path = items[0].data(Qt.UserRole)
            self.test_selected.emit(path)
    
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
        out_of_sync = self._out_of_sync(path)
        is_loaded = (path == self._current_opened_test)
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

        rename_action = menu.addAction("Rename Test...")
        rename_action.setEnabled(not is_loaded)
        if is_loaded:
            rename_action.setToolTip(
                "Close the test first — the loaded editor session pins "
                "the old folder path."
            )
        else:
            rename_action.setToolTip(
                "Rename the folder and re-sync the test id (procedure_text.md "
                "header + procedure.json id), test_order and scenario entries. "
                "Past run reports keep the old name."
            )

        menu.addSeparator()
        delete_action = menu.addAction("Delete Test...")
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
            QMessageBox.critical(self, "Mark In Sync Failed", f"Could not mark {path.name} in sync:\n{e}")
            return
        self.refresh()

    def _out_of_sync(self, path: Path) -> bool:
        return test_sync.is_out_of_sync(path, self.project_manager.get_equipment_patterns())
    
    def _rename_test(self, path: Path):
        """Rename a test folder; the id, its sync state and the project's
        references follow (core.test_folder_ops). Only offered for a test that
        is not open — the context menu disables it for the open one."""
        from ..core.test_id import sanitize_test_id

        raw, ok = QInputDialog.getText(
            self, "Rename Test", "New test name:", text=path.name,
        )
        if not ok or not raw.strip():
            return
        new_name = sanitize_test_id(raw.strip())
        if new_name == path.name:
            return
        pm = self.project_manager
        try:
            new_path, warnings = test_folder_ops.rename_test(
                pm.project_root, pm.get_tests_dir(), path, new_name,
                pm.get_equipment_patterns(),
            )
        except test_folder_ops.TestFolderError as e:
            QMessageBox.critical(self, "Rename Failed", str(e))
            return
        if warnings:
            QMessageBox.warning(
                self, "Renamed With Warnings",
                "The folder was renamed, but these could not follow "
                "(ids re-sync on the next save):\n\n"
                + "\n".join(warnings),
            )

        self.refresh()
        self._select_test_by_path(new_path)

    def _delete_test(self, path: Path):
        """Delete a test folder after confirmation (core.test_folder_ops)."""
        # Count files for the confirmation message
        files = list(path.iterdir()) if path.is_dir() else []
        file_count = len([f for f in files if f.is_file()])

        result = QMessageBox.warning(
            self,
            "Delete Test",
            f"Permanently delete test '{path.name}'?\n\n"
            f"This will remove the folder and all {file_count} file(s) inside it.\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if result != QMessageBox.Yes:
            return

        pm = self.project_manager
        try:
            test_folder_ops.delete_test(pm.get_tests_dir(), path)
        except test_folder_ops.TestFolderError as e:
            QMessageBox.critical(self, "Delete Failed", str(e))
            if not path.exists():  # moved aside, partly deleted: it is gone as a test
                self.test_deleted.emit(path)
                self.refresh()
            return

        # Emit signal so main window can clear editors if needed
        self.test_deleted.emit(path)

        # Refresh the list
        self.refresh()
    
    def _on_create_new_test(self):
        """Create a new test folder."""
        name, ok = QInputDialog.getText(
            self,
            "Create New Test",
            "Enter test name:",
        )
        
        if not ok or not name:
            return
        
        path = self.project_manager.create_test_folder(name)
        if path:
            self._load_test_list()
            self._select_test_by_path(path)
            self.test_opened.emit(path)
        else:
            self.show_error(
                "Failed to Create Test",
                f"Could not create test folder '{name}'.\n"
                "It may already exist or the path is invalid."
            )
    
    def refresh(self):
        """Refresh the test list."""
        if self.project_manager.project_root:
            # Preserve the currently selected test
            current_selection = None
            items = self.test_list.selectedItems()
            if items:
                current_selection = items[0].data(Qt.UserRole)
            
            # Reload the list
            self._load_test_list()
            
            # Reselect the previously selected test if it still exists
            if current_selection:
                self._select_test_by_path(current_selection)
            
            # Reapply opened test highlight
            self._update_opened_test_highlight()
            
            self.new_test_btn.setEnabled(True)
        else:
            self.new_test_btn.setEnabled(False)
    
    def set_project_root(self, path: Path):
        """Set project root programmatically (from CLI)."""
        if self.project_manager.set_project_root(path):
            self._load_test_list()
            self.new_test_btn.setEnabled(True)
            return True
        return False
