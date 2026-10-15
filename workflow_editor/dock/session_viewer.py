"""
Session Viewer - View and manage LLM session state, as on the approved board S5: "Session
state" with refresh and Clear, the intent in its card (Edit), then Assumptions, Decisions and
Questions as sections of one flat tree (double-click a pending question to answer it).

Implements Section 10.2 of the spec.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QFrame, QLabel, QTreeWidget, QTreeWidgetItem,
    QInputDialog, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from typing import TYPE_CHECKING, Optional

from project_services import ui_kit

from .. import theme

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
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.refresh_btn = ui_kit.icon_button("refresh", "Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setToolTip("Clear session state")
        header = ui_kit.hbox(ui_kit.bold("Session state"), None, self.refresh_btn, self.clear_btn)
        header.setSpacing(6)
        layout.addLayout(header)

        # The intent, in its card
        intent_card = QFrame()
        intent_card.setObjectName("intentCard")
        intent = QVBoxLayout(intent_card)
        intent.setContentsMargins(11, 9, 11, 9)
        intent.setSpacing(3)
        caption = ui_kit.bold("Intent")
        caption.setObjectName("sectionCaption")
        self.edit_intent_btn = ui_kit.link("Edit", self._on_edit_intent)
        intent.addLayout(ui_kit.hbox(caption, None, self.edit_intent_btn))
        self.intent_label = QLabel("Not set")
        self.intent_label.setWordWrap(True)
        intent.addWidget(self.intent_label)
        layout.addWidget(intent_card)

        # Assumptions / Decisions / Questions: the sections of one flat tree
        self.tree = QTreeWidget()
        self.tree.setObjectName("sessionTree")
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setItemsExpandable(False)
        self.tree.setWordWrap(True)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.tree, stretch=1)

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

    def _section(self, title: str, rows: list) -> None:
        """A section: its bold caption, then its rows (``(text, data)``), "none" when empty; a
        pending question in the error colour."""
        head = QTreeWidgetItem([title])
        font = head.font(0)
        font.setBold(True)
        head.setFont(0, font)
        head.setForeground(0, QColor(theme.muted_color()))
        head.setFlags(Qt.ItemFlag.ItemIsEnabled)
        for text, data in rows or [("none", None)]:
            child = QTreeWidgetItem([text])
            child.setData(0, Qt.UserRole, data)
            if data is None:
                child.setForeground(0, QColor(theme.muted_color()))
            elif data[0] == "question" and data[1].answer is None:
                child.setForeground(0, QColor(theme.error_color()))
            head.addChild(child)
        self.tree.addTopLevelItem(head)
        head.setExpanded(True)

    def refresh(self):
        """Refresh the tree view."""
        self.tree.clear()

        if not self.session_state:
            return

        # Intent
        self.intent_label.setText(self.session_state.intent or "Not set")

        assumptions = self.session_state.assumptions
        self._section(f"Assumptions · {len(assumptions)}" if assumptions else "Assumptions",
                      [(a, ("assumption", a)) for a in assumptions])
        self._section("Decisions",
                      [(f"{d.decision} — {d.why}" if d.why else d.decision, ("decision", d))
                       for d in self.session_state.decisions])

        questions = self.session_state.open_questions + self.session_state.resolved_questions
        pending = sum(1 for q in questions if q.answer is None)
        self._section(
            f"Questions · {pending} pending" if pending else "Questions",
            [(f"✓ {q.question} → {q.answer}" if q.answer is not None else f"? {q.question}",
              ("question", q)) for q in questions])

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
