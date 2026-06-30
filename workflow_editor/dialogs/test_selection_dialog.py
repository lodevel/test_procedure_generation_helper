"""Reusable test-folder selection checklist for the editor's exports.

Both the full Word report and the multi-test Markdown export run over the SAME
selection (mirroring the main app's checked-procedure list). Rows lacking the
required artifact (``procedure.json`` for Word, ``procedure_text.md`` for Markdown)
are shown greyed/disabled with their artifact state.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)


class TestSelectionDialog(QDialog):
    """Pick which test folders to export. ``require`` is ``"json"`` or ``"text"`` —
    folders missing that artifact are disabled."""

    def __init__(self, folders, require: str = "json", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select tests to export")
        self.resize(440, 480)
        self._require = require

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tick the tests to include in the export:"))
        self._list = QListWidget()
        for f in folders:
            available = bool(getattr(f, f"has_{require}", False))
            label = f"{f.name}    [{f.artifact_state}]"
            if not available:
                label += f"  —  no {require}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, f)
            if available:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
        lay.addWidget(self._list, 1)

        row = QHBoxLayout()
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch()
        lay.addLayout(row)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _set_all(self, state: "Qt.CheckState") -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(state)

    def selected_folders(self) -> list:
        """The checked ``TestFolderInfo`` rows."""
        out = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if (item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                    and item.checkState() == Qt.CheckState.Checked):
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out
