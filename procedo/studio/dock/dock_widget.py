"""
The Assistant column (the right column of Procedo Studio), as on the approved board S5: its
header ("Assistant" and the rules picker "Rules · n of N ▾"), the underlined tabs Chat /
Session / Findings N / Raw, and the chat's composer under every tab.
"""

from PySide6.QtWidgets import QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget
from typing import TYPE_CHECKING

from procedo.core import ui_kit

from ..widgets.rule_selector import RuleSelectorDialog, current_selection
from .chat_panel import ChatPanel
from .findings_panel import FindingsPanel
from .raw_response_viewer import RawResponseViewer
from .session_viewer import SessionViewer

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace


class DockWidget(QWidget):
    """
    The Assistant column: a fixed column of the workspace (no float, no close).

    Tabs: Chat (the LLM conversation), Session (assumptions / decisions / questions),
    Findings (validation issues, their count in the tab), Raw (the last responses).
    """

    def __init__(self, main_window: "StudioWorkspace", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setMinimumWidth(300)

        self.rules_picker = QPushButton("Rules ▾")
        self.rules_picker.setObjectName("rulesPicker")
        self.rules_picker.setToolTip(
            "Select Rules… — which rule documents go into the LLM context of the current view")
        self.rules_picker.clicked.connect(self._on_select_rules)
        head = QWidget()
        head.setLayout(ui_kit.hbox(ui_kit.bold("Assistant"), None, self.rules_picker))
        head.layout().setContentsMargins(14, 10, 14, 8)

        self.chat_panel = ChatPanel(self.main_window, self)
        self.session_viewer = SessionViewer(self.main_window, self)
        self.findings_panel = FindingsPanel(self.main_window, self)
        self.raw_viewer = RawResponseViewer(self.main_window, self)
        self.findings_panel.issues_changed.connect(self._show_findings_count)

        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("assistantTabs")
        self.tab_widget.setProperty("underline", True)  # the app QSS's underlined tabs
        self.tab_widget.addTab(self.chat_panel, "Chat")
        self.tab_widget.addTab(self.session_viewer, "Session")
        self.tab_widget.addTab(self.findings_panel, "Findings")
        self.tab_widget.addTab(self.raw_viewer, "Raw")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(head)
        layout.addWidget(self.tab_widget, 1)
        layout.addWidget(self.chat_panel.composer)

        # The picker counts the CURRENT view's rules: follow the view switch.
        self.main_window.view_stack.currentChanged.connect(self.refresh_rules_picker)
        self.refresh_rules_picker()

    def _current_tab_context(self):
        """The current view's TabContext (None: a view without an LLM context)."""
        return getattr(self.main_window.view_stack.currentWidget(), "tab_context", None)

    def refresh_rules_picker(self, *_) -> None:
        """"Rules · n of N ▾": how many of the project's rule documents the current view sends."""
        tab_context = self._current_tab_context()
        if tab_context is None:
            self.rules_picker.setText("Rules ▾")
            return
        available = [doc.key for doc in self.main_window.project_manager.get_rule_docs()]
        chosen = set(current_selection(self.main_window.task_config_manager, tab_context.tab_id,
                                       available))
        count = sum(1 for name in available if name in chosen)
        self.rules_picker.setText(f"Rules · {count} of {len(available)} ▾")

    def _on_select_rules(self):
        """Show the rule selector for the current view."""
        tab_context = self._current_tab_context()
        if tab_context is None:  # e.g. Traceability: no LLM context
            QMessageBox.information(
                self,
                "Not Available",
                "Rule selection is only available for tabs with LLM context (Text-JSON, JSON-Code)."
            )
            return
        from .. import shell  # the views' names, the footer (shell builds the window: imported late)
        tab_id = tab_context.tab_id
        dialog = RuleSelectorDialog(
            tab_id,
            self.main_window.project_manager,
            self.main_window.task_config_manager,
            self,
            view=shell.VIEWS[self.main_window.view_stack.currentIndex()][0],
        )
        accepted = dialog.exec()
        if accepted:
            selected_rules = dialog.get_selected_rules()
            tab_context.set_selected_rules(selected_rules)
        # The selector saves each tick, even when cancelled, and may have added a document:
        # the footer and this picker count again.
        shell.refresh_footer(self.main_window)
        if accepted:
            QMessageBox.information(
                self,
                "Rules Updated",
                f"Updated rules for {tab_id} tab: {len(selected_rules)} rules selected."
            )

    def show_validation_result_from_list(self, issues: list):
        """Show validation issues from a list of dicts."""
        ss = getattr(self.main_window, 'session_state', None)
        if ss is not None:
            ss.validation_issues = list(issues)
        self.findings_panel.display()
        if issues:
            self.tab_widget.setCurrentWidget(self.findings_panel)

    def _show_findings_count(self, count: int):
        """The Findings tab carries the issue count ("Findings 3")."""
        index = self.tab_widget.indexOf(self.findings_panel)
        self.tab_widget.setTabText(index, f"Findings {count}" if count else "Findings")

    def refresh_session(self):
        """Refresh the session viewer."""
        self.session_viewer.refresh()
