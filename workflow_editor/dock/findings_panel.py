"""
Findings Panel - Display validation issues and warnings.

Implements Section 10.3 of the spec.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..core import ValidationResult, ValidationIssue
    from ..llm.tab_context import TabContext


class FindingsPanel(QWidget):
    """
    Findings panel for displaying validation issues.
    
    Features:
    - Issue list with severity indicators
    - Filter by severity
    - Click to navigate to issue
    """
    
    # Signals
    issue_selected = Signal(object)  # Emitted when an issue is selected
    
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._issues: List = []
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
        self.clear_btn.clicked.connect(self.clear)
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
    
    def show_validation_result(self, result: "ValidationResult"):
        """Show issues from a validation result."""
        self.clear()
        
        for issue in result.issues:
            self._add_issue(issue)
        
        self._update_summary()
        self._save_to_tab_context()
    
    def show_issues(self, issues: List[dict]):
        """Show issues from LLM response."""
        self.clear()
        
        for issue in issues:
            self._add_issue_dict(issue)
        
        self._update_summary()
        self._save_to_tab_context()
    
    def _add_issue(self, issue: "ValidationIssue"):
        """Add a ValidationIssue."""
        self._issues.append(issue)
        
        item = QListWidgetItem()
        item.setData(Qt.UserRole, issue)
        
        # Format text with location if available
        location = ""
        if issue.location:
            location = f" ({issue.location})"
        
        item.setText(f"{issue.message}{location}")
        
        # Color by severity (check both enum and string for compatibility)
        severity_str = issue.severity.value if hasattr(issue.severity, 'value') else str(issue.severity)
        if severity_str == "error":
            item.setForeground(QColor("#c62828"))
            item.setText(f"✗ {item.text()}")
        elif severity_str == "warning":
            item.setForeground(QColor("#ef6c00"))
            item.setText(f"⚠ {item.text()}")
        else:
            item.setForeground(QColor("#1565c0"))
            item.setText(f"ℹ {item.text()}")
        
        self.issue_list.addItem(item)
    
    def _add_issue_dict(self, issue: dict):
        """Add an issue from dict."""
        from ..core.validators import ValidationIssue, ValidationSeverity
        
        # Convert severity string to enum
        severity_str = issue.get("severity", "info")
        try:
            severity = ValidationSeverity(severity_str)
        except ValueError:
            severity = ValidationSeverity.INFO
        
        vi = ValidationIssue(
            message=issue.get("message", str(issue)),
            severity=severity,
            location=issue.get("location", ""),
            code=issue.get("code", ""),
        )
        self._add_issue(vi)
    
    def _update_summary(self):
        """Update the summary label."""
        errors = sum(1 for i in self._issues if getattr(i, 'severity', 'info') == 'error')
        warnings = sum(1 for i in self._issues if getattr(i, 'severity', 'info') == 'warning')
        infos = len(self._issues) - errors - warnings
        
        if errors > 0:
            self.summary_label.setText(f"{errors} errors, {warnings} warnings")
            self.summary_label.setStyleSheet("color: red;")
        elif warnings > 0:
            self.summary_label.setText(f"{warnings} warnings")
            self.summary_label.setStyleSheet("color: orange;")
        elif infos > 0:
            self.summary_label.setText(f"{infos} info items")
            self.summary_label.setStyleSheet("color: blue;")
        else:
            self.summary_label.setText("No issues")
            self.summary_label.setStyleSheet("color: green;")
        
        self.count_label.setText(f"<b>Issues ({len(self._issues)})</b>")
    
    def _on_issue_clicked(self, item: QListWidgetItem):
        """Handle issue click."""
        issue = item.data(Qt.UserRole)
        if issue:
            self.issue_selected.emit(issue)
    
    def clear(self):
        """Clear all issues."""
        self._issues.clear()
        self.issue_list.clear()
        self._update_summary()
    
    def switch_context(self, tab_context: Optional["TabContext"]):
        """
        Switch to a different tab's findings.
        
        Saves current issues to old context, then loads issues from new context.
        
        Args:
            tab_context: The TabContext to switch to, or None to clear.
        """
        # Save current issues to old context
        if self._current_tab_context is not None:
            self._current_tab_context.validation_issues = self._get_issues_as_dicts()
        
        # Switch context
        self._current_tab_context = tab_context
        
        # Load issues from new context
        self._issues.clear()
        self.issue_list.clear()
        
        if tab_context is not None and tab_context.validation_issues:
            for issue in tab_context.validation_issues:
                self._add_issue_dict(issue)
        
        self._update_summary()
    
    def _get_issues_as_dicts(self) -> List[dict]:
        """
        Convert current issues to list of dicts for storage.
        
        Returns:
            List of issue dicts with message, severity, location, code.
        """
        result = []
        for issue in self._issues:
            severity_str = issue.severity.value if hasattr(issue.severity, 'value') else str(issue.severity)
            result.append({
                "message": issue.message,
                "severity": severity_str,
                "location": getattr(issue, 'location', ""),
                "code": getattr(issue, 'code', ""),
            })
        return result
    
    def _save_to_tab_context(self):
        """Save current issues to the current tab context."""
        if self._current_tab_context is not None:
            self._current_tab_context.validation_issues = self._get_issues_as_dicts()
