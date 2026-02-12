"""
Project Bar - Shows current project and rules selection.
"""

from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QComboBox, QPushButton, QFileDialog
)
from PySide6.QtCore import Signal


class ProjectBar(QWidget):
    """
    Project and rules selector bar.
    
    Shows:
    - Current project path with dropdown (recent projects)
    - Rules folder path with status indicator
    - Browse buttons for both
    """
    
    project_changed = Signal(Path)
    rules_changed = Signal(Path)
    settings_requested = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # Project selector
        layout.addWidget(QLabel("Project:"))
        
        self.project_combo = QComboBox()
        self.project_combo.setEditable(False)
        self.project_combo.setMinimumWidth(300)
        self.project_combo.addItem("No project loaded")
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        layout.addWidget(self.project_combo, stretch=1)
        
        self.browse_project_btn = QPushButton("📁")
        self.browse_project_btn.setToolTip("Browse for project folder")
        self.browse_project_btn.setMaximumWidth(40)
        self.browse_project_btn.clicked.connect(self._on_browse_project)
        layout.addWidget(self.browse_project_btn)
        
        # Spacer
        layout.addSpacing(20)
        
        # Rules selector
        layout.addWidget(QLabel("Rules:"))
        
        self.rules_label = QLabel("Not loaded")
        self.rules_label.setMinimumWidth(200)
        layout.addWidget(self.rules_label)
        
        self.browse_rules_btn = QPushButton("📁")
        self.browse_rules_btn.setToolTip("Browse for rules folder")
        self.browse_rules_btn.setMaximumWidth(40)
        self.browse_rules_btn.clicked.connect(self._on_browse_rules)
        layout.addWidget(self.browse_rules_btn)
        
        # Settings button
        layout.addSpacing(20)
        self.settings_btn = QPushButton("⚙ Settings")
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.settings_btn)
    
    def set_project(self, path: Path):
        """Set current project path."""
        # Clear and add new project
        self.project_combo.clear()
        self.project_combo.addItem(str(path), userData=path)
        self.project_combo.setCurrentIndex(0)
    
    def add_recent_project(self, path: Path):
        """Add project to recent list."""
        # Check if already exists
        for i in range(self.project_combo.count()):
            if self.project_combo.itemData(i) == path:
                return
        # Add to dropdown
        self.project_combo.addItem(str(path), userData=path)
    
    def set_rules_status(self, path: Optional[Path], loaded: bool):
        """Update rules status indicator."""
        if path is None:
            self.rules_label.setText("⚠ Not found")
            self.rules_label.setStyleSheet("color: orange;")
        elif loaded:
            self.rules_label.setText(f"✅ {path.name}")
            self.rules_label.setStyleSheet("color: green;")
        else:
            self.rules_label.setText(f"❌ Invalid: {path.name}")
            self.rules_label.setStyleSheet("color: red;")
    
    def _on_project_changed(self, index: int):
        """Handle project selection change."""
        if index >= 0:
            path = self.project_combo.itemData(index)
            if path:
                self.project_changed.emit(path)
    
    def _on_browse_project(self):
        """Browse for project folder."""
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Project Root Folder",
            str(Path.home())
        )
        if path:
            project_path = Path(path)
            self.set_project(project_path)
            self.project_changed.emit(project_path)
    
    def _on_browse_rules(self):
        """Browse for rules folder."""
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Rules Folder",
            str(Path.home())
        )
        if path:
            rules_path = Path(path)
            self.rules_changed.emit(rules_path)
