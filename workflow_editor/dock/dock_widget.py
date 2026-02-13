"""
Dock Widget - Container for right-side dock panels.

Implements the dock panel structure from Section 10.
"""

from PySide6.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QTabWidget
)
from PySide6.QtCore import Qt, Signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..core import ValidationResult


class DockWidget(QDockWidget):
    """
    Container dock widget for right panel.
    
    Contains tabbed sections:
    - Chat: LLM conversation
    - Session: Assumptions/decisions/questions
    - Findings: Validation issues
    - Raw: Debug response viewer
    """
    
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__("Assistant", parent)
        self.main_window = main_window
        
        self.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.setMinimumWidth(300)
        
        self._findings_count = 0  # Track issue count for badge
        
        self._setup_ui()
    
    def _setup_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Toolbar with rule selector button
        from PySide6.QtWidgets import QHBoxLayout, QPushButton
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(5, 5, 5, 5)
        
        self.rule_selector_btn = QPushButton("Select Rules...")
        self.rule_selector_btn.setToolTip("Configure which rules are included in LLM context for the current tab")
        self.rule_selector_btn.clicked.connect(self._on_select_rules)
        toolbar_layout.addWidget(self.rule_selector_btn)
        toolbar_layout.addStretch()
        
        layout.addLayout(toolbar_layout)
        
        # Tab widget for panels
        self.tab_widget = QTabWidget()
        
        # Import panels here to avoid circular imports
        from .chat_panel import ChatPanel
        from .session_viewer import SessionViewer
        from .findings_panel import FindingsPanel
        from .raw_response_viewer import RawResponseViewer
        
        self.chat_panel = ChatPanel(self.main_window, self)
        self.session_viewer = SessionViewer(self.main_window, self)
        self.findings_panel = FindingsPanel(self.main_window, self)
        self.raw_viewer = RawResponseViewer(self.main_window, self)
        
        # Connect findings panel clear to badge update
        self.findings_panel.issue_list.model().rowsRemoved.connect(self._on_findings_cleared)
        
        self.tab_widget.addTab(self.chat_panel, "Chat")
        self.tab_widget.addTab(self.session_viewer, "Session")
        self.tab_widget.addTab(self.findings_panel, "Findings")
        self.tab_widget.addTab(self.raw_viewer, "Raw")
        
        layout.addWidget(self.tab_widget)
        
        self.setWidget(container)
    
    def show_chat(self):
        """Show the chat panel."""
        self.tab_widget.setCurrentWidget(self.chat_panel)
        self.show()
    
    def show_session(self):
        """Show the session viewer."""
        self.tab_widget.setCurrentWidget(self.session_viewer)
        self.show()
    
    def show_findings(self):
        """Show the findings panel."""
        self.tab_widget.setCurrentWidget(self.findings_panel)
        self.show()
    
    def _on_select_rules(self):
        """Show rule selector dialog for current tab."""
        from ..widgets.rule_selector import RuleSelectorDialog
        from PySide6.QtWidgets import QMessageBox
        
        # Get current tab from main window
        current_widget = self.main_window.tab_widget.currentWidget()
        
        # Determine tab_id and tab_context
        tab_context = None
        tab_id = None
        
        if hasattr(current_widget, 'tab_context'):
            tab_context = current_widget.tab_context
            tab_id = tab_context.tab_id
        else:
            # Fallback for tabs without tab_context (e.g., workspace tab)
            QMessageBox.information(
                self,
                "Not Available",
                "Rule selection is only available for tabs with LLM context (Text-JSON, JSON-Code)."
            )
            return
        
        # Show dialog
        dialog = RuleSelectorDialog(tab_id, self.main_window.project_manager, self)
        if dialog.exec():
            selected_rules = dialog.get_selected_rules()
            # Update tab context with new rules
            tab_context.set_selected_rules(selected_rules)
            
            # Show confirmation
            QMessageBox.information(
                self,
                "Rules Updated",
                f"Updated rules for {tab_id} tab: {len(selected_rules)} rules selected."
            )
    
    def show_raw(self):
        """Show the raw response viewer."""
        self.tab_widget.setCurrentWidget(self.raw_viewer)
        self.show()
    
    def show_validation_result(self, result: "ValidationResult"):
        """Show a validation result in findings panel."""
        self.findings_panel.show_validation_result(result)
        # Update count based on actual items in the list
        self._findings_count = self.findings_panel.issue_list.count()
        self._update_findings_tab_text()
        if result.issues:
            self.tab_widget.setCurrentWidget(self.findings_panel)
    
    def show_validation_result_from_list(self, issues: list):
        """Show validation issues from a list of dicts."""
        self.findings_panel.show_issues(issues)
        # Update count based on actual items in the list
        self._findings_count = self.findings_panel.issue_list.count()
        self._update_findings_tab_text()
        if issues:
            self.tab_widget.setCurrentWidget(self.findings_panel)
    
    def _update_findings_tab_text(self):
        """Update findings tab text with badge count."""
        findings_index = self.tab_widget.indexOf(self.findings_panel)
        if findings_index >= 0:
            if self._findings_count > 0:
                self.tab_widget.setTabText(findings_index, f"Findings ({self._findings_count})")
            else:
                self.tab_widget.setTabText(findings_index, "Findings")
    
    def _on_findings_cleared(self):
        """Handle when findings list is cleared."""
        # Update count based on current number of items in list
        self._findings_count = self.findings_panel.issue_list.count()
        self._update_findings_tab_text()
    
    def add_chat_message(self, role: str, content: str):
        """Add a message to the chat panel."""
        self.chat_panel.add_message(role, content)
    
    def show_llm_response(self, raw_text: str, parsed_data: dict):
        """Show LLM response in appropriate panels."""
        # Show raw in debug viewer
        self.raw_viewer.show_response(raw_text)
        
        # Show any issues in findings
        if "validation_issues" in parsed_data:
            self.findings_panel.show_issues(parsed_data["validation_issues"])
            # Update count based on actual items in the list
            self._findings_count = self.findings_panel.issue_list.count()
            self._update_findings_tab_text()
        
        # Show session updates
        if "session_delta" in parsed_data:
            self.session_viewer.refresh()
    
    def refresh_session(self):
        """Refresh the session viewer."""
        self.session_viewer.refresh()
