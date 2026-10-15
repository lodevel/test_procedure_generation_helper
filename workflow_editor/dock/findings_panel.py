"""
Findings Panel - Display validation issues and warnings, as on the approved board S5: "Issues"
with Copy / Clear, the counts as pills, one card per issue (its severity and location pills,
the message, the fix).

Implements Section 10.3 of the spec.

Design: session_state.validation_issues (list[dict]) is the SINGLE SOURCE OF TRUTH.
Findings are tied to the current test, not to individual tabs.
This panel is a pure display widget that reads from session_state.
Callers write issues to session_state.validation_issues, then call display().
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QApplication, QMenu, QFrame
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QKeySequence, QShortcut
from typing import TYPE_CHECKING, Optional

from project_services import ui_kit

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..core.session_state import SessionState

#: An issue's severity -> its pill tone.
_TONES = {"error": "danger", "warning": "warning"}
#: The gap under each issue card.
_CARD_GAP = 8


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 or word == 'info' else 's'}"


def _issue_card(issue: dict) -> QWidget:
    """One issue as a card (#findingCard): severity and location pills, the message, the fix."""
    card = QFrame()
    card.setObjectName("findingCard")
    column = QVBoxLayout(card)
    column.setContentsMargins(10, 8, 10, 8)
    column.setSpacing(4)
    severity = str(issue.get("severity", "info"))
    head = ui_kit.hbox(ui_kit.set_pill(QLabel(), _TONES.get(severity, "info"), severity.upper()), None)
    if issue.get("location"):
        head.addWidget(ui_kit.set_pill(QLabel(), "neutral", str(issue["location"])))
    column.addLayout(head)
    message = QLabel(str(issue.get("message", issue)))
    message.setWordWrap(True)
    column.addWidget(message)
    if issue.get("suggested_fix"):
        fix = QLabel(f"Fix: {issue['suggested_fix']}")
        fix.setObjectName("findingFix")
        fix.setWordWrap(True)
        column.addWidget(fix)
    if issue.get("code"):
        card.setToolTip(str(issue["code"]))
    holder = QWidget()  # the card and the gap under it
    holder.setLayout(ui_kit.hbox(card))
    holder.layout().setContentsMargins(0, 0, 0, _CARD_GAP)
    return holder


class _IssueList(QListWidget):
    """The cards: each row as tall as its card at the list's width; the selected ones marked."""

    def __init__(self):
        super().__init__()
        self.setObjectName("findingList")
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.itemSelectionChanged.connect(self._mark_selected)

    def add_issue(self, issue: dict) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, issue)
        self.addItem(item)
        self.setItemWidget(item, _issue_card(issue))
        self._fit(item)

    def _fit(self, item: QListWidgetItem) -> None:
        width = self.viewport().width()
        holder = self.itemWidget(item)
        item.setSizeHint(QSize(width, holder.heightForWidth(width)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for row in range(self.count()):
            self._fit(self.item(row))

    def _mark_selected(self) -> None:
        for row in range(self.count()):
            item = self.item(row)
            card = self.itemWidget(item).findChild(QFrame, "findingCard")
            ui_kit.set_state(card, "selected", item.isSelected())


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
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip(
            "Copy selected issues as text (or all issues if none selected). "
            "Ctrl+C also works."
        )
        self.copy_btn.clicked.connect(self._on_copy)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        layout.addLayout(ui_kit.hbox(ui_kit.bold("Issues"), None, self.copy_btn, self.clear_btn))

        # The counts, as pills ("1 error", "1 warning", "1 info"; "No issues")
        self.summary_row = ui_kit.hbox()
        self.summary_row.setSpacing(6)
        layout.addLayout(self.summary_row)

        # Issue list (multi-select for copy)
        self.issue_list = _IssueList()
        self.issue_list.itemClicked.connect(self._on_issue_clicked)
        self.issue_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.issue_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.issue_list, stretch=1)

        # Ctrl+C copies selected (or all if nothing selected)
        copy_shortcut = QShortcut(QKeySequence.Copy, self.issue_list)
        copy_shortcut.setContext(Qt.WidgetShortcut)
        copy_shortcut.activated.connect(self._on_copy)

        layout.addWidget(ui_kit.note("Right-click: Copy selected · Copy all"))

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
            self.issue_list.add_issue(issue_dict)

        self._update_summary(issues)
        self.issues_changed.emit(len(issues))

    def _update_summary(self, issues: list):
        """The counts' pills: errors, warnings, the rest as info ("No issues" when none)."""
        ui_kit.clear_layout(self.summary_row)
        severities = [i.get("severity", "info") if isinstance(i, dict) else "info" for i in issues]
        errors, warnings = severities.count("error"), severities.count("warning")
        pills = [("danger", _count(errors, "error")) if errors else None,
                 ("warning", _count(warnings, "warning")) if warnings else None,
                 ("info", _count(len(issues) - errors - warnings, "info"))
                 if len(issues) > errors + warnings else None]
        if not issues:
            pills = [("success", "No issues")]
        for tone, text in filter(None, pills):
            self.summary_row.addWidget(ui_kit.set_pill(QLabel(), tone, text))
        self.summary_row.addStretch(1)

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
        """Copy selected issues (or all if none selected) to clipboard as text."""
        if self.issue_list.selectedItems():
            self._copy_selected()
        else:
            self._copy_all()

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
        self._copy(self.issue_list.selectedItems())

    def _copy_all(self):
        self._copy([self.issue_list.item(i) for i in range(self.issue_list.count())])

    def _copy(self, items: list) -> None:
        """Put *items*' issues on the clipboard as plain markdown bullets (nothing: no-op)."""
        if items:
            QApplication.clipboard().setText(
                self._format_issues([item.data(Qt.UserRole) for item in items]))

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
