"""
Workspace Tab - Project and test folder selection.

Implements Section 9.1 of the spec.
"""

from pathlib import Path
import hashlib
import json
import logging
import os
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QGroupBox, QFrame, QInputDialog,
    QMenu, QMessageBox
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QBrush

from .base_tab import BaseTab
from ..badge_delegate import BADGES_ROLE, BadgeDelegate
from ..core import ArtifactType
from ..core.sync_utils import normalize_for_hash
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
        
        self.test_list = QListWidget()
        self.test_list.setObjectName("workflowTestCards")
        self.test_list.setItemDelegate(BadgeDelegate(self.test_list))
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
            out_of_sync = self._is_test_out_of_sync(info.path)
            
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
    
    def _select_test_by_path(self, path: Path):
        """Select a test in the list by path."""
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            if item.data(Qt.UserRole) == path:
                self.test_list.setCurrentItem(item)
                break
    
    def _is_test_out_of_sync(self, test_path: Path) -> bool:
        """Check if a test's artifacts are out of sync.

        First checks the ``artifacts_in_sync`` flag in ``.llm_session.json``.
        If the flag reports in-sync, performs a live SHA-256 hash comparison
        of the canonical artifacts (procedure.json, test.py) against the
        stored baselines so that external edits made while the editor was
        closed are detected immediately when the Tests list is loaded.

        Returns False (in sync) if the session file doesn't exist or the
        stored hashes are absent (first-open / legacy session).
        """
        session_file = test_path / ".llm_session.json"
        if not session_file.exists():
            return False
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            return False

        # If the flag already says out-of-sync, trust it
        if not data.get("artifacts_in_sync", True):
            return True

        # Perform a live hash check to catch edits made while the editor was closed
        stored_hashes: dict = data.get("artifact_hashes", {})
        if not stored_hashes:
            # No baseline yet (first open or legacy session) — assume in sync
            return False

        patterns = self.project_manager.get_equipment_patterns()
        # Must mirror ArtifactManager._SYNC_TRACKED_NAMES — adding text
        # so operator edits to procedure_text.md flip the ⚠️ flag too.
        canonical_files = ("procedure.json", "test.py", "procedure_text.md")
        for filename in canonical_files:
            stored = stored_hashes.get(filename)
            if not stored:
                continue
            file_path = test_path / filename
            if not file_path.exists():
                continue
            try:
                disk_content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            normalised = normalize_for_hash(disk_content, filename, patterns)
            disk_hash = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
            if disk_hash != stored:
                return True

        return False
    
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
                # Highlight opened test
                item.setBackground(QBrush(selected_test_bg()))  # Steel blue
                item.setForeground(selected_test_fg())  # White text
            else:
                # Restore original colors for non-opened tests
                item.setBackground(QBrush())
                info = item.data(Qt.UserRole + 1)
                out_of_sync = self._is_test_out_of_sync(item_path)
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
        out_of_sync = self._is_test_out_of_sync(path)
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
        elif action == delete_action:
            self._delete_test(path)

    def _mark_test_in_sync(self, path: Path):
        """Rebaseline ``.llm_session.json`` for *path*.

        Computes the same normalised SHA-256 hashes that
        :meth:`_is_test_out_of_sync` compares against, then writes them
        to the test's session file alongside ``artifacts_in_sync=True``.
        Preserves any other session-state fields already on disk
        (intent, decisions, open_questions, …) by reading-then-merging.

        Only called for unloaded tests — the context menu disables
        this entry when ``path == self._current_opened_test`` so the
        loaded path stays under main_window control.
        """
        session_file = path / ".llm_session.json"
        if session_file.exists():
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        else:
            data = {}

        patterns = self.project_manager.get_equipment_patterns()
        # Must mirror ArtifactManager._SYNC_TRACKED_NAMES — adding text
        # so operator edits to procedure_text.md flip the ⚠️ flag too.
        canonical_files = ("procedure.json", "test.py", "procedure_text.md")
        hashes: dict = dict(data.get("artifact_hashes", {}))
        for filename in canonical_files:
            file_path = path / filename
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception as e:
                QMessageBox.critical(
                    self, "Mark In Sync Failed",
                    f"Could not read {filename}:\n{e}",
                )
                return
            normalised = normalize_for_hash(content, filename, patterns)
            hashes[filename] = hashlib.sha256(
                normalised.encode("utf-8"),
            ).hexdigest()

        data["artifact_hashes"] = hashes
        data["artifacts_in_sync"] = True

        try:
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Mark In Sync Failed",
                f"Could not write {session_file.name}:\n{e}",
            )
            return

        self.refresh()
    
    def _delete_test(self, path: Path):
        """Delete a test folder after confirmation."""
        import shutil
        
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
        
        try:
            shutil.rmtree(path)
        except Exception as e:
            QMessageBox.critical(
                self,
                "Delete Failed",
                f"Could not delete '{path.name}':\n{e}"
            )
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
