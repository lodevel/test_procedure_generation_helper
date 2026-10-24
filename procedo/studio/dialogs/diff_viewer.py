"""The diff window, as on the approved board S7: a proposal card's View diff… (one file of the
proposal at a time), with the card's Accept / Reject (:meth:`DiffViewer.review`).

Its header names the file and says nothing is written until Accept; a proposal that failed
validation keeps its warning banner on top (only this window may apply it, so the user must see
why); then the Original | Proposed cards, their changed lines tinted, the unified diff, and the
footer's +a −r with Reject and the primary Accept changes. The diff is the proposal's own
(``core.proposal``): the lines shown, tinted and counted are those the card counts.
"""

from __future__ import annotations

from pathlib import PurePath

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QDialog, QFrame, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QVBoxLayout,
)

from procedo.ui import ui_kit

from .. import theme
from ..core.proposal import ADDED, HEADER, REMOVED, changed_lines, count_changes, diff_lines, diff_rows
from ..tabs.code_tab import PythonSyntaxHighlighter
from ..tabs.json_tab import JsonSyntaxHighlighter
from ..widgets.editor_card import CodeEditor, tint_lines
from ..widgets.text_highlighter import ProcedureTextHighlighter

#: The panes' syntax colours, by the file's suffix (the views' own highlighters).
_HIGHLIGHTERS = {".md": ProcedureTextHighlighter, ".json": JsonSyntaxHighlighter,
                 ".py": PythonSyntaxHighlighter}
#: How many rows of findings the validator banner shows before it scrolls.
_BANNER_ROWS = 6


def _line_format(background: QColor | None = None, foreground: QColor | None = None) -> QTextCharFormat:
    fmt = QTextCharFormat()
    if background is not None:
        fmt.setBackground(background)
    if foreground is not None:
        fmt.setForeground(foreground)
    return fmt


def _diff_marks(diff: list[str]) -> dict[int, QTextCharFormat]:
    """The unified diff's colours, by what each row is (:func:`diff_rows`): added lines green,
    removed red, the headers (file names, ``@@``) in the accent."""
    marks = {HEADER: _line_format(foreground=theme.tone("accent_deep")),
             ADDED: _line_format(theme.tone("pill_success_bg"), theme.tone("pill_success_fg")),
             REMOVED: _line_format(theme.tone("pill_danger_bg"), theme.tone("pill_danger_fg"))}
    return {number: marks[kind] for number, (kind, _, _) in enumerate(diff_rows(diff), 1)
            if kind in marks}


class DiffViewer(QDialog):
    """One file of a proposal: *original* (its text now) against *proposed*; *banner*, when the
    proposal failed validation, says why; *counter* (this file, of how many) when it has several.
    ``decision`` is what the user decided: "accept", "reject", or "" (the window was closed)."""

    def __init__(self, original: str, proposed: str, file_name: str = "", parent=None,
                 banner: str = "", counter: tuple[int, int] = (1, 1)):
        super().__init__(parent)
        self.setWindowTitle(f"Review Changes: {file_name}" if file_name else "Review Changes")
        self.setMinimumSize(800, 500)
        self.resize(1240, 820)
        self._original = original
        self._proposed = proposed
        self._banner = banner
        self._file_name = file_name
        self._diff = diff_lines(original, proposed)
        self._removed, self._added = changed_lines(self._diff)
        self._highlighters = []
        self.decision = ""
        self._setup_ui(counter)

    def _setup_ui(self, counter: tuple[int, int]) -> None:
        number, files = counter
        subtitle = [self._file_name] if self._file_name else []
        subtitle += [f"file {number} of {files}"] if files > 1 else []
        subtitle.append("nothing is written until you accept")
        approval = ui_kit.set_pill(QLabel(), "warning", "Approval needed")
        approval.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # centred
        header = ui_kit.HeaderBand("Review the proposed changes", " · ".join(subtitle),
                                   [approval], stacked=True)
        self.reject_btn = ui_kit.set_state(QPushButton("Reject"), "danger", True)
        self.reject_btn.clicked.connect(lambda: self._decide("reject"))
        self.accept_btn = QPushButton("Accept changes")
        ui_kit.set_primary(self.accept_btn)  # the look only: Enter never accepts (no default)
        self.accept_btn.clicked.connect(lambda: self._decide("accept"))
        for button in (self.reject_btn, self.accept_btn):
            button.setAutoDefault(False)  # else the focused one, or the first, takes Enter
        footer = ui_kit.FooterBand([self.reject_btn, self.accept_btn], self._changes_note())
        body = ui_kit.framed(self, header, footer, margins=(18, 14, 18, 14), spacing=12)

        if self._banner:
            body.addWidget(self._banner_frame())

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.setHandleWidth(12)
        panes.setChildrenCollapsible(False)
        tint_removed = _line_format(theme.tone("pill_danger_bg"))
        tint_added = _line_format(theme.tone("pill_success_bg"))
        self.original_view = self._pane(panes, "Original", self._original, self._removed, tint_removed)
        self.proposed_view = self._pane(panes, "Proposed", self._proposed, self._added, tint_added)

        unified = ui_kit.Card("Unified diff")
        self.diff_view = ui_kit.monospace(QPlainTextEdit())
        self.diff_view.setObjectName("cardEditor")
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff_view.setPlainText("\n".join(self._diff))
        tint_lines(self.diff_view, _diff_marks(self._diff))
        unified.body.addWidget(self.diff_view)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setHandleWidth(12)
        split.setChildrenCollapsible(False)
        split.addWidget(panes)
        split.addWidget(unified)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        body.addWidget(split, 1)

    def _banner_frame(self) -> QFrame:
        """The validator's warning: its first line, bold, always in view; the findings under it
        in a read-only text that scrolls past :data:`_BANNER_ROWS` rows (every one reachable)."""
        headline, _, findings = self._banner.partition("\n")
        frame = QFrame()
        frame.setObjectName("validatorBanner")
        column = QVBoxLayout(frame)
        column.setContentsMargins(14, 8, 14, 8)
        column.setSpacing(2)
        title = ui_kit.bold(headline)
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(title)
        if findings:
            self.banner_findings = view = QPlainTextEdit(findings)
            view.setReadOnly(True)
            view.document().setDocumentMargin(0)  # in line with the headline
            column.addWidget(view)
            view.ensurePolished()  # in the banner: the stylesheet's font sets the row height
            rows = min(findings.count("\n") + 1, _BANNER_ROWS)
            # + 1: QPlainTextEdit counts its last row shown only with a pixel to spare
            view.setFixedHeight(rows * view.fontMetrics().lineSpacing() + 2 * view.frameWidth() + 1)
        return frame

    def _pane(self, panes: QSplitter, title: str, text: str, changed: list[int],
              tint: QTextCharFormat) -> CodeEditor:
        """An Original / Proposed card: *text* with its line numbers, the *changed* lines tinted."""
        card = ui_kit.Card(title, self._file_name)
        editor = CodeEditor()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        highlighter = _HIGHLIGHTERS.get(PurePath(self._file_name).suffix)
        if highlighter is not None:
            self._highlighters.append(highlighter(editor.document()))
        tint_lines(editor, dict.fromkeys(changed, tint))
        card.body.addWidget(editor)
        panes.addWidget(card)
        return editor

    def _changes_note(self) -> str:
        added, removed = count_changes(self._diff)
        plus, minus = (theme.tone(f"pill_{tone}_fg").name() for tone in ("success", "danger"))
        return (f"Changes: <b style='color:{plus}'>+{added}</b> lines, "
                f"<b style='color:{minus}'>−{removed}</b> lines")

    def showEvent(self, event) -> None:  # noqa: N802 (Qt)
        """Open on the first change (each pane on its own side's, else the other's)."""
        super().showEvent(event)
        for editor, own, other in ((self.original_view, self._removed, self._added),
                                   (self.proposed_view, self._added, self._removed)):
            first = (own or other or [0])[0]
            if first:
                editor.jump_to_line(first)

    def _decide(self, decision: str) -> None:
        self.decision = decision
        if decision == "accept":
            self.accept()
        else:
            self.reject()

    @staticmethod
    def review(original: str, proposed: str, file_name: str = "", parent=None, banner: str = "",
               counter: tuple[int, int] = (1, 1)) -> str:
        """Show the diff window; what the user decided: "accept", "reject" or "" (closed)."""
        dialog = DiffViewer(original, proposed, file_name, parent, banner, counter)
        dialog.exec()
        return dialog.decision
