"""
Clean Dialog - Remove generated files.

Implements Section 12.3 of the spec.
"""

from pathlib import Path
from typing import List
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QCheckBox, QMessageBox
)
from PySide6.QtCore import Qt


class CleanDialog(QDialog):
    """
    Clean dialog for removing generated files.
    
    Features:
    - List of cleanable files
    - Selective deletion
    - Confirm before delete
    """
    
    def __init__(self, test_folder: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clean Generated Files")
        self.setMinimumWidth(400)
        
        self._test_folder = test_folder
        self._files_to_delete: List[Path] = []
        
        self._setup_ui()
        self._load_files()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QLabel(
            "<b>Select files to remove:</b><br>"
            "<i>This will permanently delete the selected files.</i>"
        )
        layout.addWidget(header)
        
        # File list
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.MultiSelection)
        layout.addWidget(self.file_list, stretch=1)
        
        # Select all checkbox
        self.select_all = QCheckBox("Select All")
        self.select_all.stateChanged.connect(self._on_select_all)
        layout.addWidget(self.select_all)
        
        # Warning
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: red;")
        layout.addWidget(self.warning_label)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self._on_delete)
        self.delete_btn.setStyleSheet("background-color: #f44336; color: white;")
        btn_layout.addWidget(self.delete_btn)
        
        layout.addLayout(btn_layout)
    
    def _load_files(self):
        """Load cleanable files."""
        self.file_list.clear()
        
        # List of cleanable files
        cleanable = [
            "procedure.json",
            "test.py",
            "procedure_text.md",
            ".llm_session.json",
        ]
        
        for filename in cleanable:
            path = self._test_folder / filename
            if path.exists():
                item = QListWidgetItem(filename)
                item.setData(Qt.UserRole, path)
                self.file_list.addItem(item)
        
        if self.file_list.count() == 0:
            self.file_list.addItem("(No cleanable files found)")
            self.delete_btn.setEnabled(False)
    
    def _on_select_all(self, state):
        """Handle select all checkbox."""
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            item.setSelected(state == Qt.Checked)
    
    def _on_delete(self):
        """Delete selected files."""
        selected = self.file_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select files to delete.")
            return
        
        # Get file paths
        files = []
        for item in selected:
            path = item.data(Qt.UserRole)
            if path:
                files.append(path)
        
        if not files:
            return
        
        # Confirm
        result = QMessageBox.warning(
            self,
            "Confirm Delete",
            f"Delete {len(files)} file(s)?\n\n"
            + "\n".join(f"  • {p.name}" for p in files)
            + "\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if result != QMessageBox.Yes:
            return
        
        # Delete
        errors = []
        for path in files:
            try:
                path.unlink()
            except Exception as e:
                errors.append(f"{path.name}: {e}")
        
        if errors:
            QMessageBox.warning(
                self,
                "Some Deletions Failed",
                "\n".join(errors)
            )
        
        self._files_to_delete = files
        self.accept()
    
    def get_deleted_files(self) -> List[Path]:
        """Get the list of deleted files."""
        return self._files_to_delete
    
    @staticmethod
    def clean_test_folder(test_folder: Path, parent=None) -> List[Path]:
        """
        Show clean dialog and return list of deleted files.
        
        Returns:
            List of paths that were deleted
        """
        dialog = CleanDialog(test_folder, parent)
        result = dialog.exec()
        
        if result == QDialog.Accepted:
            return dialog.get_deleted_files()
        return []
