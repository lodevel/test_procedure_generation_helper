"""
Session Viewer - View and manage LLM session state.

Implements Section 10.2 of the spec.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QPushButton, QLabel, QFrame, QTreeWidget, QTreeWidgetItem,
    QInputDialog, QMessageBox
)
from PySide6.QtCore import Qt, Signal
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..llm.tab_context import TabContext


class SessionViewer(QWidget):
    """
    Session viewer for managing LLM session state.
    
    Features:
    - View assumptions, decisions, questions
    - Answer pending questions
    - Clear session
    - Session summary
    - Per-tab session state switching
    """
    
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
        header.addWidget(QLabel("<b>Session State</b>"))
        header.addStretch()
        
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(30)
        self.refresh_btn.clicked.connect(self.refresh)
        self.refresh_btn.setToolTip("Refresh")
        header.addWidget(self.refresh_btn)
        
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setToolTip("Clear session state")
        header.addWidget(self.clear_btn)
        
        layout.addLayout(header)
        
        # Tree view
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Item", "Value"])
        self.tree.setColumnWidth(0, 150)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.tree, stretch=1)
        
        # Intent editor
        intent_layout = QHBoxLayout()
        intent_layout.addWidget(QLabel("Intent:"))
        self.intent_label = QLabel("<i>Not set</i>")
        self.intent_label.setWordWrap(True)
        intent_layout.addWidget(self.intent_label, stretch=1)
        
        self.edit_intent_btn = QPushButton("Edit")
        self.edit_intent_btn.setFixedWidth(50)
        self.edit_intent_btn.clicked.connect(self._on_edit_intent)
        intent_layout.addWidget(self.edit_intent_btn)
        
        layout.addLayout(intent_layout)
    
    def switch_context(self, tab_context: Optional["TabContext"]):
        """Switch to a different tab's session state.
        
        Args:
            tab_context: The TabContext to switch to, or None to clear.
        """
        self._current_tab_context = tab_context
        self.refresh()
    
    @property
    def session_state(self):
        """Get the session state from current tab context."""
        if self._current_tab_context is not None:
            return self._current_tab_context.session_state
        # Fallback to main_window.session_state for backward compatibility
        return self.main_window.session_state
    
    def refresh(self):
        """Refresh the tree view."""
        self.tree.clear()
        
        if not self.session_state:
            return
        
        # Intent
        if self.session_state.intent:
            self.intent_label.setText(self.session_state.intent)
        else:
            self.intent_label.setText("<i>Not set</i>")
        
        # Assumptions
        assumptions_item = QTreeWidgetItem(["Assumptions", ""])
        for assumption in self.session_state.assumptions:
            child = QTreeWidgetItem(["", assumption])
            child.setData(0, Qt.UserRole, ("assumption", assumption))
            assumptions_item.addChild(child)
        if not self.session_state.assumptions:
            child = QTreeWidgetItem(["", "<none>"])
            assumptions_item.addChild(child)
        self.tree.addTopLevelItem(assumptions_item)
        assumptions_item.setExpanded(True)
        
        # Decisions
        decisions_item = QTreeWidgetItem(["Decisions", ""])
        for decision in self.session_state.decisions:
            child = QTreeWidgetItem([decision.decision, decision.why])
            child.setData(0, Qt.UserRole, ("decision", decision))
            decisions_item.addChild(child)
        if not self.session_state.decisions:
            child = QTreeWidgetItem(["", "<none>"])
            decisions_item.addChild(child)
        self.tree.addTopLevelItem(decisions_item)
        decisions_item.setExpanded(True)
        
        # Questions
        questions_item = QTreeWidgetItem(["Questions", ""])
        pending_count = 0
        all_questions = self.session_state.open_questions + self.session_state.resolved_questions
        for question in all_questions:
            status = "✓" if question.answer is not None else "?"
            child = QTreeWidgetItem([f"{status} {question.question[:30]}...", 
                                     question.answer or ""])
            child.setData(0, Qt.UserRole, ("question", question))
            if question.answer is None:
                child.setForeground(0, Qt.red)
                pending_count += 1
            questions_item.addChild(child)
        if not all_questions:
            child = QTreeWidgetItem(["", "<none>"])
            questions_item.addChild(child)
        
        label = f"Questions ({pending_count} pending)" if pending_count else "Questions"
        questions_item.setText(0, label)
        self.tree.addTopLevelItem(questions_item)
        questions_item.setExpanded(True)
    
    def _on_clear(self):
        """Clear the session state."""
        result = QMessageBox.question(
            self,
            "Clear Session",
            "Clear all assumptions, decisions, and questions?\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if result == QMessageBox.Yes:
            self.session_state.clear()
            self.refresh()
    
    def _on_edit_intent(self):
        """Edit the session intent."""
        current = self.session_state.intent or ""
        text, ok = QInputDialog.getText(
            self,
            "Edit Intent",
            "Describe what you're trying to accomplish:",
            text=current
        )
        
        if ok:
            self.session_state.intent = text
            # Per-tab session state is in-memory only, no save needed
            self.refresh()
    
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """Handle double-click on an item."""
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        
        item_type, item_data = data
        
        if item_type == "question":
            question = item_data
            if not question.answered:
                self._answer_question(question)
    
    def _answer_question(self, question):
        """Answer a pending question."""
        answer, ok = QInputDialog.getText(
            self,
            "Answer Question",
            f"Question: {question.question}\n\nYour answer:",
        )
        
        if ok and answer:
            question.answer = answer
            question.answered = True
            # Per-tab session state is in-memory only, no save needed
            self.refresh()
