"""Reusable Find/Replace bar for QPlainTextEdit-based tab editors.

Embeds at the bottom of a tab. Hidden by default; show via Ctrl+F
(find-only) or Ctrl+H (replace expanded). Esc closes. Targets a
specific QPlainTextEdit set via :meth:`set_target`.

The bar carries no per-tab state — each tab owns one instance and
swaps the target editor when focus moves between its left/right
panes.
"""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)


class FindReplaceBar(QWidget):
    """Two-row docked find/replace strip.

    Row 1 (always visible when shown): Find field + Prev / Next / Close.
    Row 2 (shown when in replace mode): Replace field + Replace / Replace All.
    Toggles: case-sensitive, whole-word, regex.
    """

    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._target: Optional[QPlainTextEdit] = None
        # Editors registered via install_find_shortcuts; used to re-pick
        # the focused-or-leftmost target on every show_find/show_replace
        # so the menu-driven path (Edit → Find) tracks focus the same
        # way the keyboard shortcut does.
        self._editors: list[QPlainTextEdit] = []
        self._setup_ui()
        self.hide()

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        # --- Row 1: find ----------------------------------------------------
        find_row = QHBoxLayout()
        find_row.setSpacing(4)

        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText("Find")
        self.find_field.returnPressed.connect(self.find_next)
        self.find_field.textChanged.connect(self._on_find_text_changed)

        self.prev_button = QPushButton("Prev")
        self.prev_button.setToolTip("Find previous (Shift+Enter)")
        self.prev_button.clicked.connect(self.find_prev)

        self.next_button = QPushButton("Next")
        self.next_button.setToolTip("Find next (Enter)")
        self.next_button.clicked.connect(self.find_next)

        self.case_toggle = QCheckBox("Aa")
        self.case_toggle.setToolTip("Match case")

        self.word_toggle = QCheckBox("W")
        self.word_toggle.setToolTip("Whole word")

        self.regex_toggle = QCheckBox(".*")
        self.regex_toggle.setToolTip("Regex")

        self.status_label = QLineEdit()
        self.status_label.setReadOnly(True)
        self.status_label.setFrame(False)
        self.status_label.setMaximumWidth(120)
        self.status_label.setStyleSheet("background: transparent;")

        self.close_button = QPushButton("✕")
        self.close_button.setToolTip("Close (Esc)")
        self.close_button.setMaximumWidth(28)
        self.close_button.clicked.connect(self.close_bar)

        find_row.addWidget(self.find_field, 1)
        find_row.addWidget(self.prev_button)
        find_row.addWidget(self.next_button)
        find_row.addWidget(self.case_toggle)
        find_row.addWidget(self.word_toggle)
        find_row.addWidget(self.regex_toggle)
        find_row.addWidget(self.status_label)
        find_row.addWidget(self.close_button)
        outer.addLayout(find_row)

        # --- Row 2: replace -------------------------------------------------
        self._replace_row_widgets: list[QWidget] = []
        replace_row = QHBoxLayout()
        replace_row.setSpacing(4)

        self.replace_field = QLineEdit()
        self.replace_field.setPlaceholderText("Replace with")
        self.replace_field.returnPressed.connect(self.replace_one)

        self.replace_button = QPushButton("Replace")
        self.replace_button.setToolTip("Replace current match")
        self.replace_button.clicked.connect(self.replace_one)

        self.replace_all_button = QPushButton("Replace All")
        self.replace_all_button.setToolTip("Replace every match")
        self.replace_all_button.clicked.connect(self.replace_all)

        replace_row.addWidget(self.replace_field, 1)
        replace_row.addWidget(self.replace_button)
        replace_row.addWidget(self.replace_all_button)
        outer.addLayout(replace_row)

        for w in (self.replace_field, self.replace_button, self.replace_all_button):
            self._replace_row_widgets.append(w)

        # Esc shortcut: only when the bar is visible and focus is inside it.
        esc = QShortcut(QKeySequence("Esc"), self)
        esc.setContext(Qt.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close_bar)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def set_target(self, editor: QPlainTextEdit) -> None:
        """Set the QPlainTextEdit that find/replace operates on."""
        self._target = editor

    def show_find(self, target: Optional[QPlainTextEdit] = None) -> None:
        """Show the bar in find-only mode (Ctrl+F). When called with
        no target and an editor list is registered, re-pick the focused
        editor (or leftmost) so the menu path tracks focus."""
        if target is None and self._editors:
            target = self._pick_target_from_editors()
        if target is not None:
            self.set_target(target)
        self._set_replace_visible(False)
        self._show_and_focus()

    def show_replace(self, target: Optional[QPlainTextEdit] = None) -> None:
        """Show the bar with replace row expanded (Ctrl+H). Same
        focus-tracking behaviour as show_find."""
        if target is None and self._editors:
            target = self._pick_target_from_editors()
        if target is not None:
            self.set_target(target)
        self._set_replace_visible(True)
        self._show_and_focus()

    def _pick_target_from_editors(self) -> QPlainTextEdit:
        focused = QApplication.focusWidget()
        if focused in self._editors:
            return focused  # type: ignore[return-value]
        return self._editors[0]

    def close_bar(self) -> None:
        self.hide()
        self._clear_status()
        # Hand focus back to the target editor so typing resumes naturally.
        if self._target is not None:
            self._target.setFocus()
        self.closed.emit()

    # ------------------------------------------------------------------ #
    # Find / Replace operations                                          #
    # ------------------------------------------------------------------ #

    def find_next(self) -> bool:
        return self._find(forward=True)

    def find_prev(self) -> bool:
        return self._find(forward=False)

    def replace_one(self) -> None:
        """Replace the current selection (if it matches the find pattern),
        then advance to the next match. If nothing is selected yet, just
        find next — operator-friendly: first Enter finds, second replaces."""
        if self._target is None:
            return
        cursor = self._target.textCursor()
        if cursor.hasSelection() and self._selection_matches_pattern(cursor.selectedText()):
            cursor.insertText(self.replace_field.text())
        self.find_next()

    def replace_all(self) -> None:
        if self._target is None:
            return
        pattern = self.find_field.text()
        if not pattern:
            return
        replacement = self.replace_field.text()
        text = self._target.toPlainText()
        try:
            new_text, count = self._replace_all_in_string(text, pattern, replacement)
        except re.error as exc:
            self._set_status(f"Bad regex: {exc}")
            return
        if count == 0:
            self._set_status("No matches")
            return
        # One undo unit for the whole replace-all.
        cursor = self._target.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.Document)
        cursor.insertText(new_text)
        cursor.endEditBlock()
        self._set_status(f"{count} replaced")

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _show_and_focus(self) -> None:
        # Pre-fill find field with the target's current selection if any.
        if self._target is not None:
            sel = self._target.textCursor().selectedText()
            if sel and " " not in sel and len(sel) < 200:
                self.find_field.setText(sel)
        self.show()
        self.find_field.setFocus()
        self.find_field.selectAll()

    def _set_replace_visible(self, visible: bool) -> None:
        for w in self._replace_row_widgets:
            w.setVisible(visible)

    def _on_find_text_changed(self, _text: str) -> None:
        self._clear_status()

    def _clear_status(self) -> None:
        self.status_label.setText("")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _build_qtextdocument_flags(self, forward: bool) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindBackward
        if self.case_toggle.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        if self.word_toggle.isChecked():
            flags |= QTextDocument.FindWholeWords
        return flags

    def _find(self, *, forward: bool) -> bool:
        if self._target is None:
            return False
        pattern = self.find_field.text()
        if not pattern:
            return False
        doc = self._target.document()
        flags = self._build_qtextdocument_flags(forward)
        cursor = self._target.textCursor()

        if self.regex_toggle.isChecked():
            try:
                regex = self._compile_regex(pattern)
            except re.error as exc:
                self._set_status(f"Bad regex: {exc}")
                return False
            found = self._find_regex(regex, cursor, forward=forward)
        else:
            found = doc.find(pattern, cursor, flags)

        if found is None or found.isNull():
            # Wrap around once.
            wrap_cursor = QTextCursor(doc)
            if not forward:
                wrap_cursor.movePosition(QTextCursor.End)
            if self.regex_toggle.isChecked():
                # Already attempted regex above; retry from doc edge.
                found = self._find_regex(
                    self._compile_regex(pattern), wrap_cursor, forward=forward,
                )
            else:
                found = doc.find(pattern, wrap_cursor, flags)
            if found is None or found.isNull():
                self._set_status("No matches")
                return False
            self._set_status("Wrapped")
        else:
            self._clear_status()
        self._target.setTextCursor(found)
        return True

    def _compile_regex(self, pattern: str) -> re.Pattern[str]:
        flags = 0 if self.case_toggle.isChecked() else re.IGNORECASE
        return re.compile(pattern, flags)

    def _find_regex(
        self, regex: re.Pattern[str], cursor: QTextCursor, *, forward: bool,
    ) -> Optional[QTextCursor]:
        if self._target is None:
            return None
        text = self._target.toPlainText()
        if forward:
            start = cursor.selectionEnd() if cursor.hasSelection() else cursor.position()
            m = regex.search(text, start)
        else:
            end = cursor.selectionStart() if cursor.hasSelection() else cursor.position()
            # Reverse search: last match before `end`.
            m = None
            for cand in regex.finditer(text, 0, end):
                m = cand
        if m is None:
            return None
        doc = self._target.document()
        new_cursor = QTextCursor(doc)
        new_cursor.setPosition(m.start())
        new_cursor.setPosition(m.end(), QTextCursor.KeepAnchor)
        return new_cursor

    def _selection_matches_pattern(self, selected: str) -> bool:
        """True when the current selection matches the find pattern under
        the current case/word/regex settings. Prevents Replace from
        replacing a non-match (e.g. arbitrary user selection)."""
        pattern = self.find_field.text()
        if not pattern:
            return False
        if self.regex_toggle.isChecked():
            try:
                regex = self._compile_regex(pattern)
            except re.error:
                return False
            return regex.fullmatch(selected) is not None
        if self.case_toggle.isChecked():
            return selected == pattern
        return selected.lower() == pattern.lower()

    def _replace_all_in_string(
        self, text: str, pattern: str, replacement: str,
    ) -> tuple[str, int]:
        """Pure helper for ``replace_all`` — exposed for testability."""
        if self.regex_toggle.isChecked():
            regex = self._compile_regex(pattern)
        else:
            esc = re.escape(pattern)
            if self.word_toggle.isChecked():
                esc = rf"\b{esc}\b"
            flags = 0 if self.case_toggle.isChecked() else re.IGNORECASE
            regex = re.compile(esc, flags)
        new_text, count = regex.subn(replacement, text)
        return new_text, count


# --------------------------------------------------------------------------- #
# Convenience: bind Ctrl+F / Ctrl+H on a tab to open the bar                  #
# --------------------------------------------------------------------------- #


def install_find_shortcuts(
    tab: QWidget, editors: list[QPlainTextEdit], bar: FindReplaceBar,
) -> None:
    """Bind Ctrl+F (find) and Ctrl+H (replace) on *tab* so they open the
    bar targeting the focused editor — or ``editors[0]`` (leftmost) when
    no editor in the list has focus.

    Also registers the editor list on the bar so menu-driven invocations
    (Edit → Find / Edit → Replace) get the same focus-tracking
    behaviour as the keyboard shortcut.

    Shortcut context is ``Qt.WidgetWithChildrenShortcut`` so the binding
    only fires while the tab is active. Idempotent: calling twice
    registers two shortcuts; the GUI tabs only call it once during
    ``_setup_ui``.
    """
    bar._editors = list(editors)

    find_sc = QShortcut(QKeySequence("Ctrl+F"), tab)
    find_sc.setContext(Qt.WidgetWithChildrenShortcut)
    find_sc.activated.connect(bar.show_find)

    replace_sc = QShortcut(QKeySequence("Ctrl+H"), tab)
    replace_sc.setContext(Qt.WidgetWithChildrenShortcut)
    replace_sc.activated.connect(bar.show_replace)
