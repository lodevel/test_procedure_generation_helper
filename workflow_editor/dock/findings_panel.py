"""
Findings Panel - Display validation issues and warnings.

Implements Section 10.3 of the spec.

Design: tab_context.validation_issues (list[dict]) is the SINGLE SOURCE OF TRUTH.
This panel is a pure display widget that reads from tab_context.
It never writes back to tab_context except on explicit clear.
Callers write issues to tab_context.validation_issues, then call display().
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..llm.tab_context import TabContext


class FindingsPanel(QWidget):
    """
    Findings panel for displaying validation issues.

    Pure display widget. The single source of truth is
    tab_context.validation_issues (list of dicts).
    """

    # Signals
    issue_selected = Signal(object)  # Emitted when an issue is selected
    issues_changed = Signal(int)     # Emitted with new count when display changes

    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._current_tab_context: Optional["TabContext"] = None

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Header
        header = QHBoxLayout()
        self.count_label = QLabel("<b>Issues</b>")
        header.addWidget(self.count_label)
        header.addStretch()

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        header.addWidget(self.clear_btn)

        layout.addLayout(header)

        # Issue list
        self.issue_list = QListWidget()
        self.issue_list.itemClicked.connect(self._on_issue_clicked)
        layout.addWidget(self.issue_list, stretch=1)

        # Summary
        self.summary_label = QLabel("No issues")
        self.summary_label.setStyleSheet("color: green;")
        layout.addWidget(self.summary_label)

    # ── Public API ──────────────────────────────────────────

    def set_context(self, tab_context: Optional["TabContext"]):
        """Switch to a different tab context and refresh display."""
        self._current_tab_context = tab_context
        self._refresh_display()

    def display(self):
        """Refresh display from current tab_context.validation_issues."""
        self._refresh_display()

    # ── Private ─────────────────────────────────────────────

    def _refresh_display(self):
        """Rebuild the list widget from tab_context.validation_issues."""
        self.issue_list.clear()

        issues = []
        if self._current_tab_context is not None:
            issues = self._current_tab_context.validation_issues or []

        for issue_dict in issues:
            self._add_issue_item(issue_dict)

        self._update_summary(len(issues))
        self.issues_changed.emit(len(issues))

    def _add_issue_item(self, issue: dict):
        """Add a single issue dict to the list widget."""
        item = QListWidgetItem()
        item.setData(Qt.UserRole, issue)

        msg = issue.get("message", str(issue))
        location = issue.get("location", "")
        if location:
            msg = f"{msg} ({location})"

        severity = issue.get("severity", "info")
        if severity == "error":
            item.setForeground(QColor("#c62828"))
            item.setText(f"\u2717 {msg}")
        elif severity == "warning":
            item.setForeground(QColor("#ef6c00"))
            item.setText(f"\u26a0 {msg}")
        else:
            item.setForeground(QColor("#1565c0"))
            item.setText(f"\u2139 {msg}")

        self.issue_list.addItem(item)

    def _update_summary(self, total: int):
        """Update header and summary labels."""
        self.count_label.setText(
            f"<b>Issues ({total})</b>" if total else "<b>Issues</b>"
        )

        if total == 0:
            self.summary_label.setText("No issues")
            self.summary_label.setStyleSheet("color: green;")
            return

        issues = (
            self._current_tab_context.validation_issues or []
            if self._current_tab_context is not None
            else []
        )
        errors = sum(1 for i in issues if i.get("severity") == "error")
        warnings = sum(1 for i in issues if i.get("severity") == "warning")

        if errors > 0:
            self.summary_label.setText(f"{errors} errors, {warnings} warnings")
            self.summary_label.setStyleSheet("color: red;")
        elif warnings > 0:
            self.summary_label.setText(f"{warnings} warnings")
            self.summary_label.setStyleSheet("color: orange;")
        else:
            self.summary_label.setText(f"{total} info items")
            self.summary_label.setStyleSheet("color: blue;")

    def _on_issue_clicked(self, item: QListWidgetItem):
        """Handle issue click."""
        issue = item.data(Qt.UserRole)
        if issue:
            self.issue_selected.emit(issue)

    def _on_clear(self):
        """Clear issues from tab_context and refresh display."""
        if self._current_tab_context is not None:
            self._current_tab_context.validation_issues = []
        self._refresh_display()
