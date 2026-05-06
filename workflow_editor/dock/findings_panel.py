"""
Findings Panel - Display validation issues and warnings.

Implements Section 10.3 of the spec.

Design: session_state.validation_issues (list[dict]) is the SINGLE SOURCE OF TRUTH.
Findings are tied to the current test, not to individual tabs.
This panel is a pure display widget that reads from session_state.
Callers write issues to session_state.validation_issues, then call display().
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QApplication, QMenu
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from typing import TYPE_CHECKING, Optional

from ..theme import finding_error, finding_warning, finding_info, finding_success

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..core.session_state import SessionState


class FindingsPanel(QWidget):
    """
    Findings panel for displaying validation issues.

    Pure display widget. The single source of truth is
    session_state.validation_issues (list of dicts).
    Findings are per-test, not per-tab.
    """

    # Signals
    issue_selected = Signal(object)  # Emitted when an issue is selected
    issues_changed = Signal(int)     # Emitted with new count when display changes

    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._session_state: Optional["SessionState"] = None

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Header
        header = QHBoxLayout()
        self.count_label = QLabel("<b>Issues</b>")
        header.addWidget(self.count_label)
        header.addStretch()

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip(
            "Copy selected issues as text (or all issues if none selected). "
            "Ctrl+C also works."
        )
        self.copy_btn.clicked.connect(self._on_copy)
        header.addWidget(self.copy_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        header.addWidget(self.clear_btn)

        layout.addLayout(header)

        # Issue list (multi-select for copy)
        self.issue_list = QListWidget()
        self.issue_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.issue_list.itemClicked.connect(self._on_issue_clicked)
        self.issue_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.issue_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.issue_list, stretch=1)

        # Ctrl+C copies selected (or all if nothing selected)
        copy_shortcut = QShortcut(QKeySequence.Copy, self.issue_list)
        copy_shortcut.setContext(Qt.WidgetShortcut)
        copy_shortcut.activated.connect(self._on_copy)

        # Summary
        self.summary_label = QLabel("No issues")
        self.summary_label.setStyleSheet(f"color: {finding_success()};")
        layout.addWidget(self.summary_label)

    # ── Public API ──────────────────────────────────────────

    def set_session(self, session_state: Optional["SessionState"]):
        """Switch to a different session state (new test opened) and refresh."""
        self._session_state = session_state
        self._refresh_display()

    def display(self):
        """Refresh display from current session_state.validation_issues."""
        self._refresh_display()

    # ── Private ─────────────────────────────────────────────

    def _refresh_display(self):
        """Rebuild the list widget from session_state.validation_issues."""
        self.issue_list.clear()

        issues = []
        if self._session_state is not None:
            issues = self._session_state.validation_issues or []

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
            item.setForeground(finding_error())
            item.setText(f"\u2717 {msg}")
        elif severity == "warning":
            item.setForeground(finding_warning())
            item.setText(f"\u26a0 {msg}")
        else:
            item.setForeground(finding_info())
            item.setText(f"\u2139 {msg}")

        self.issue_list.addItem(item)

    def _update_summary(self, total: int):
        """Update header and summary labels."""
        self.count_label.setText(
            f"<b>Issues ({total})</b>" if total else "<b>Issues</b>"
        )

        if total == 0:
            self.summary_label.setText("No issues")
            self.summary_label.setStyleSheet(f"color: {finding_success()};")
            return

        issues = (
            self._session_state.validation_issues or []
            if self._session_state is not None
            else []
        )
        errors = sum(1 for i in issues if i.get("severity") == "error")
        warnings = sum(1 for i in issues if i.get("severity") == "warning")

        if errors > 0:
            self.summary_label.setText(f"{errors} errors, {warnings} warnings")
            self.summary_label.setStyleSheet(f"color: {finding_error().name()};")
        elif warnings > 0:
            self.summary_label.setText(f"{warnings} warnings")
            self.summary_label.setStyleSheet(f"color: {finding_warning().name()};")
        else:
            self.summary_label.setText(f"{total} info items")
            self.summary_label.setStyleSheet(f"color: {finding_info().name()};")

    def _on_issue_clicked(self, item: QListWidgetItem):
        """Handle issue click."""
        issue = item.data(Qt.UserRole)
        if issue:
            self.issue_selected.emit(issue)

    def _on_clear(self):
        """Clear issues from session_state and refresh display."""
        if self._session_state is not None:
            self._session_state.validation_issues = []
        self._refresh_display()

    def _on_copy(self):
        """Copy selected issues (or all if none selected) to clipboard as text.

        Format is plain markdown — easy to paste into chat. Each issue
        becomes one bullet with severity, code, message, location, and
        suggested fix on a continuation line when present.
        """
        selected = self.issue_list.selectedItems()
        if selected:
            issues = [item.data(Qt.UserRole) for item in selected]
        else:
            issues = []
            for i in range(self.issue_list.count()):
                issues.append(self.issue_list.item(i).data(Qt.UserRole))

        if not issues:
            return

        QApplication.clipboard().setText(self._format_issues(issues))

    def _on_context_menu(self, pos):
        """Right-click context menu with copy actions."""
        menu = QMenu(self.issue_list)
        has_selection = bool(self.issue_list.selectedItems())
        has_any = self.issue_list.count() > 0

        copy_sel = menu.addAction("Copy Selected")
        copy_sel.setEnabled(has_selection)
        copy_sel.triggered.connect(self._copy_selected)

        copy_all = menu.addAction("Copy All")
        copy_all.setEnabled(has_any)
        copy_all.triggered.connect(self._copy_all)

        menu.exec(self.issue_list.viewport().mapToGlobal(pos))

    def _copy_selected(self):
        items = self.issue_list.selectedItems()
        if not items:
            return
        QApplication.clipboard().setText(
            self._format_issues([item.data(Qt.UserRole) for item in items])
        )

    def _copy_all(self):
        if self.issue_list.count() == 0:
            return
        issues = [
            self.issue_list.item(i).data(Qt.UserRole)
            for i in range(self.issue_list.count())
        ]
        QApplication.clipboard().setText(self._format_issues(issues))

    @staticmethod
    def _format_issues(issues: list) -> str:
        """Render a list of issue dicts as plain-text markdown bullets."""
        lines = []
        for issue in issues:
            if not isinstance(issue, dict):
                lines.append(f"- {issue}")
                continue
            severity = issue.get("severity", "info")
            code = issue.get("code", "")
            message = issue.get("message", "")
            location = issue.get("location", "")
            fix = issue.get("suggested_fix", "")

            head = f"- [{severity}]"
            if code:
                head += f" {code}:"
            if message:
                head += f" {message}"
            if location:
                head += f" ({location})"
            lines.append(head)
            if fix:
                lines.append(f"  Fix: {fix}")
        return "\n".join(lines)
