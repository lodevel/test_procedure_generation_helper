"""The views' editors, as on the approved boards S1–S4: a white card with the file's name, its
Saved / ● Modified pill and its own Save, over a code editor with a line-number gutter.

``CodeEditor`` is the one editor widget of all four views: monospace, line numbers in its gutter
(or a step's mark, ``set_marks``), highlighted line ranges with the accent bar beside them.
``EditorCard`` binds one to one artifact: every edit is mirrored into the ArtifactManager (so
Review / Validate and the header's pills see the live text), the pill and Save follow the
artifact's dirty state, and Save writes it to disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout,
    QWidget,
)

from project_services import ui_kit

from ..core import ArtifactType

if TYPE_CHECKING:
    from ..tabs.base_tab import BaseTab

#: The editors' text size (the boards' 12px monospace).
_FONT_PX = 12
#: The gutter's padding right of the numbers, and the accent bar's width beside a highlight.
_GUTTER_PAD = 10
_BAR = 3


def refresh_editor_after_save(editor: QPlainTextEdit, saved_content: str) -> None:
    """Mirror save-time-corrected artifact content back into the editor.

    ``ArtifactManager.save_artifact`` may rewrite the content it was given (id == folder
    enforcement), so the visible buffer must be refreshed to match disk or the screen lies until
    reload. Signals are blocked so the ``textChanged`` mirror doesn't re-dirty the just-saved
    artifact; the caret position is preserved (clamped to the new end). No-op when the save
    wrote the buffer verbatim — the common case.
    """
    if editor.toPlainText() == saved_content:
        return
    pos = editor.textCursor().position()
    editor.blockSignals(True)
    editor.setPlainText(saved_content)
    editor.blockSignals(False)
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.setPosition(min(pos, cursor.position()))
    editor.setTextCursor(cursor)


class _Gutter(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        return QSize(self._editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._editor.paint_gutter(self, event.rect())


class CodeEditor(QPlainTextEdit):
    """A monospace editor with its line numbers in a gutter (``wrap``: long lines wrap)."""

    def __init__(self, *, wrap: bool = True) -> None:
        super().__init__()
        self.setObjectName("cardEditor")
        font = ui_kit.mono_font(self.font())
        font.setPixelSize(_FONT_PX)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if wrap
                             else QPlainTextEdit.LineWrapMode.NoWrap)
        self._marks: dict[int, str] = {}
        self._highlighted = range(0)
        self._gutter = _Gutter(self)
        self.blockCountChanged.connect(self._fit_gutter)
        self.updateRequest.connect(self._follow_scroll)
        self._fit_gutter()

    # ---- what the gutter shows ---------------------------------------------------------------

    def set_marks(self, marks: dict[int, str]) -> None:
        """Show *marks* (1-based line → label, a step's ``▸7``) in place of those lines' numbers."""
        self._marks = dict(marks)
        self._fit_gutter()
        self._gutter.update()

    def highlight_lines(self, first: int, last: int) -> None:
        """Tint lines *first*..*last* (1-based), the accent bar beside them in the gutter."""
        self._highlighted = range(first, last + 1)
        selections = []
        for line in self._highlighted:
            block = self.document().findBlockByNumber(line - 1)
            if block.isValid():
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(self._tint())
                selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                selection.cursor = QTextCursor(block)
                selections.append(selection)
        self.setExtraSelections(selections)
        self._gutter.update()

    def clear_highlight(self) -> None:
        self._highlighted = range(0)
        self.setExtraSelections([])
        self._gutter.update()

    def jump_to_line(self, line: int) -> None:
        """Put the caret on line *line* (1-based) and scroll it to the middle."""
        block = self.document().findBlockByNumber(max(line - 1, 0))
        if block.isValid():
            self.setTextCursor(QTextCursor(block))
            self.centerCursor()

    # ---- the gutter ------------------------------------------------------------------------

    def gutter_width(self) -> int:
        widest = max([str(max(self.blockCount(), 1)), *self._marks.values()], key=len)
        return _BAR + 8 + self.fontMetrics().horizontalAdvance(widest + "0") + _GUTTER_PAD

    def _fit_gutter(self, *_) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _follow_scroll(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        self._fit_gutter()  # the stylesheet may have changed the font since
        area = self.contentsRect()
        self._gutter.setGeometry(QRect(area.left(), area.top(), self.gutter_width(), area.height()))

    @staticmethod
    def _accent() -> QColor:
        """The app's accent (the editor's own Highlight is the stylesheet's pale selection)."""
        return QApplication.palette().color(QPalette.ColorRole.Highlight)

    def _tint(self) -> QColor:
        tint = self._accent()
        tint.setAlpha(22)
        return tint

    def paint_gutter(self, gutter: QWidget, area: QRect) -> None:
        painter = QPainter(gutter)
        painter.fillRect(area, self.palette().color(QPalette.ColorRole.Base))
        accent = self._accent()
        number_color = QColor(self.palette().color(QPalette.ColorRole.PlaceholderText))
        number_color.setAlpha(150)
        bold = painter.font()
        bold.setBold(True)
        width = gutter.width() - _GUTTER_PAD
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= area.bottom():
            bottom = top + round(self.blockBoundingRect(block).height())
            line = block.blockNumber() + 1
            if block.isVisible() and bottom >= area.top():
                if line in self._highlighted:
                    painter.fillRect(0, top, gutter.width(), bottom - top, self._tint())
                    painter.fillRect(0, top, _BAR, bottom - top, accent)
                mark = self._marks.get(line)
                painter.setFont(bold if mark else self.font())
                painter.setPen(accent if mark else number_color)
                painter.drawText(0, top, width, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, mark or str(line))
            block = block.next()
            top = bottom
        painter.end()


class EditorCard(QFrame):
    """One artifact's editor in a white card: its file name, the Saved / ● Modified pill and its
    Save in the header row (``add_to_header``: more left of Save), the editor under it."""

    #: The editor's text changed (typing, a proposal applied, a sync) — not a load.
    changed = Signal()
    #: Save wrote the artifact to disk.
    saved = Signal()

    def __init__(self, tab: BaseTab, kind: ArtifactType, file_name: str, *, wrap: bool = True,
                 placeholder: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        self._tab = tab
        self.kind = kind
        self.file_name = file_name
        #: The artifact's name in the LLM context ("procedure_text", "procedure_json", "test_code").
        self.artifact_name = kind.name.lower()
        self.name = QLabel(file_name)
        self.name.setObjectName("cardFileName")
        self.pill = QLabel()
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip(f"Save {file_name} to disk")
        self.save_button.clicked.connect(self.save)
        self._head = QHBoxLayout()
        self._head.setContentsMargins(12, 8, 12, 8)
        self._head.setSpacing(8)
        for widget in (self.name, self.pill):
            self._head.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._head.addStretch(1)
        self._head.addWidget(self.save_button)
        self.editor = CodeEditor(wrap=wrap)
        self.editor.setPlaceholderText(placeholder)
        self.editor.textChanged.connect(self._on_edited)
        rule = QFrame()
        rule.setObjectName("rowRule")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(1, 0, 1, 1)  # inside the card's border
        self.body.setSpacing(0)
        self.body.addLayout(self._head)
        self.body.addWidget(rule)
        self.body.addWidget(self.editor, 1)
        self._show_state(False)

    def add_to_header(self, widget: QWidget) -> None:
        """*widget* in the header row, right-aligned left of Save (a "draft" chip, a hint)."""
        self._head.insertWidget(self._head.count() - 1, widget, 0, Qt.AlignmentFlag.AlignVCenter)

    @property
    def dirty(self) -> bool:
        """The artifact differs from what is on disk."""
        am = self._tab.artifact_manager
        return am is not None and am.is_dirty(self.kind)

    def load(self) -> None:
        """Show the artifact's content (no ``changed``: nothing was edited)."""
        am = self._tab.artifact_manager
        self.editor.blockSignals(True)
        self.editor.setPlainText(am.get_content(self.kind))
        self.editor.blockSignals(False)
        self._show_state(self.dirty)

    def sync(self) -> None:
        """Put the editor's text into the artifact (what every edit already does)."""
        self._tab.artifact_manager.set_content(self.kind, self.editor.toPlainText())

    def set_text(self, text: str) -> None:
        """Replace the editor's text, as an edit (a proposal or a sync applied)."""
        self.editor.setPlainText(text)

    def save(self) -> bool:
        """Write the artifact to disk; the editor shows what was written (the id is the folder's)."""
        am = self._tab.artifact_manager
        try:
            self.sync()
            am.save_artifact(self.kind)
        except Exception as exc:  # noqa: BLE001 — told to the operator, the card stays modified
            self._tab.show_error("Save Failed", str(exc))
            return False
        refresh_editor_after_save(self.editor, am.get_content(self.kind))
        self._show_state(False)
        self._tab.status_message.emit(f"{self.file_name} saved")
        self._tab.artifact_saved.emit()
        self.saved.emit()
        return True

    def _on_edited(self) -> None:
        if self._tab.artifact_manager is None:  # no test open (an editor being cleared)
            return
        self.sync()
        self._show_state(self.dirty)
        self.changed.emit()

    def _show_state(self, dirty: bool) -> None:
        if dirty:
            ui_kit.set_pill(self.pill, "warning", "● Modified")
        else:
            ui_kit.set_pill(self.pill, "success", "✓ Saved")
        self.save_button.setEnabled(dirty)
