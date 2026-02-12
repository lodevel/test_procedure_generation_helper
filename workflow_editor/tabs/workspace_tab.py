"""
Workspace Tab - Project and test folder selection.

Implements Section 9.1 of the spec.
"""

from pathlib import Path
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QGroupBox, QFrame, QInputDialog
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from .base_tab import BaseTab
from ..core import ArtifactType


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
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Test list only - project selection is in File menu
        list_group = QGroupBox("Tests")
        list_layout = QVBoxLayout(list_group)
        
        self.test_list = QListWidget()
        self.test_list.itemSelectionChanged.connect(self._on_test_selection_changed)
        self.test_list.itemDoubleClicked.connect(self._on_test_double_clicked)
        list_layout.addWidget(self.test_list)
        
        # New test button
        self.new_test_btn = QPushButton("Create New Test...")
        self.new_test_btn.clicked.connect(self._on_create_new_test)
        self.new_test_btn.setEnabled(False)
        list_layout.addWidget(self.new_test_btn)
        
        layout.addWidget(list_group, stretch=1)
    
    def _load_test_list(self):
        """Load test folders into the list."""
        self.test_list.clear()
        
        folders = self.project_manager.enumerate_test_folders()
        
        for info in folders:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, info.path)
            
            # Build display text with indicators
            indicators = []
            if info.has_text:
                indicators.append("T")
            if info.has_json:
                indicators.append("J")
            if info.has_code:
                indicators.append("C")
            
            indicator_str = f"[{'/'.join(indicators)}]" if indicators else "[empty]"
            item.setText(f"{info.name}  {indicator_str}")
            
            # Color based on state
            if not indicators:
                item.setForeground(QColor(150, 150, 150))
            elif info.has_json and info.has_code:
                item.setForeground(QColor(0, 150, 0))
            
            self.test_list.addItem(item)
    
    def _select_test_by_path(self, path: Path):
        """Select a test in the list by path."""
        for i in range(self.test_list.count()):
            item = self.test_list.item(i)
            if item.data(Qt.UserRole) == path:
                self.test_list.setCurrentItem(item)
                break
    
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
            self._load_test_list()
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
