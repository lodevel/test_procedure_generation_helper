"""
New Project Dialog - Create a new project folder structure.
"""

from pathlib import Path
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QCheckBox, QGroupBox
)
from PySide6.QtCore import Qt


class NewProjectDialog(QDialog):
    """
    Dialog for creating a new project with proper folder structure.
    
    Creates:
    - Project root folder
    - tests/ subdirectory (required)
    - rules/ subdirectory (required)
    - config/ subdirectory (optional)
    - README.md with project info (optional)
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create New Project")
        self.setMinimumWidth(500)
        
        self.project_path: Path | None = None
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QLabel(
            "<b>Create a new test procedure project</b><br>"
            "<i>This will create a folder structure for your test procedures.</i>"
        )
        layout.addWidget(header)
        
        # Project name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Project Name:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g., MyTestProject")
        self.name_edit.textChanged.connect(self._update_preview)
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)
        
        # Parent location
        location_layout = QHBoxLayout()
        location_layout.addWidget(QLabel("Location:"))
        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText("Select parent folder...")
        self.location_edit.setReadOnly(True)
        self.location_edit.textChanged.connect(self._update_preview)
        location_layout.addWidget(self.location_edit, stretch=1)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._on_browse)
        location_layout.addWidget(browse_btn)
        layout.addLayout(location_layout)
        
        # Preview
        preview_group = QGroupBox("Project will be created at:")
        preview_layout = QVBoxLayout()
        self.preview_label = QLabel("<i>Enter project name and location</i>")
        self.preview_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_label)
        preview_group.setLayout(preview_layout)
        layout.addWidget(preview_group)
        
        # Options
        options_group = QGroupBox("Create:")
        options_layout = QVBoxLayout()
        
        self.create_config_check = QCheckBox("config/ folder with default settings")
        self.create_config_check.setChecked(False)
        self.create_config_check.setToolTip("Creates config/tab_contexts.json with default task configurations")
        options_layout.addWidget(self.create_config_check)
        
        self.create_readme_check = QCheckBox("README.md file")
        self.create_readme_check.setChecked(True)
        self.create_readme_check.setToolTip("Creates a README.md file with basic project information")
        options_layout.addWidget(self.create_readme_check)
        
        tests_label = QLabel("✓ tests/ folder (always created)")
        tests_label.setStyleSheet("color: #666;")
        options_layout.addWidget(tests_label)
        
        rules_label = QLabel("✓ rules/ folder (always created)")
        rules_label.setStyleSheet("color: #666;")
        options_layout.addWidget(rules_label)
        
        options_group.setLayout(options_layout)
        layout.addWidget(options_group)
        
        # Structure preview
        structure_group = QGroupBox("Folder Structure:")
        structure_layout = QVBoxLayout()
        self.structure_label = QLabel(
            "<pre>"
            "ProjectName/\n"
            "  ├── tests/           <i>(required - test folders go here)</i>\n"
            "  ├── rules/           <i>(required - LLM guidance files)</i>\n"
            "  ├── config/          <i>(optional - tab configurations)</i>\n"
            "  └── README.md        <i>(optional - project info)</i>"
            "</pre>"
        )
        structure_layout.addWidget(self.structure_label)
        structure_group.setLayout(structure_layout)
        layout.addWidget(structure_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        self.create_btn = QPushButton("Create Project")
        self.create_btn.setDefault(True)
        self.create_btn.clicked.connect(self._on_create)
        self.create_btn.setEnabled(False)
        button_layout.addWidget(self.create_btn)
        
        layout.addLayout(button_layout)
    
    def _on_browse(self):
        """Browse for parent folder location."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Parent Folder",
            str(Path.home()),
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if folder:
            self.location_edit.setText(folder)
    
    def _update_preview(self):
        """Update the preview of project path."""
        name = self.name_edit.text().strip()
        location = self.location_edit.text().strip()
        
        if name and location:
            project_path = Path(location) / name
            self.preview_label.setText(f"<b>{project_path}</b>")
            self.create_btn.setEnabled(True)
        else:
            self.preview_label.setText("<i>Enter project name and location</i>")
            self.create_btn.setEnabled(False)
    
    def _on_create(self):
        """Validate and create the project."""
        name = self.name_edit.text().strip()
        location = self.location_edit.text().strip()
        
        if not name:
            QMessageBox.warning(self, "Invalid Input", "Please enter a project name.")
            return
        
        if not location:
            QMessageBox.warning(self, "Invalid Input", "Please select a parent folder location.")
            return
        
        # Validate project name (no invalid path characters)
        invalid_chars = '<>:"|?*'
        if any(char in name for char in invalid_chars):
            QMessageBox.warning(
                self,
                "Invalid Project Name",
                f"Project name cannot contain these characters: {invalid_chars}"
            )
            return
        
        self.project_path = Path(location) / name
        
        # Check if folder already exists
        if self.project_path.exists():
            reply = QMessageBox.question(
                self,
                "Folder Already Exists",
                f"The folder '{self.project_path}' already exists.\n\n"
                "Do you want to initialize it as a project anyway?\n"
                "(This will create missing folders but won't delete existing files)",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        self.accept()
    
    def get_project_config(self) -> dict:
        """
        Get the project configuration from dialog choices.
        
        Returns:
            Dictionary with:
            - path: Path object for project root
            - create_config: bool
            - create_readme: bool
        """
        return {
            "path": self.project_path,
            "create_config": self.create_config_check.isChecked(),
            "create_readme": self.create_readme_check.isChecked(),
        }
