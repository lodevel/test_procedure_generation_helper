"""The Assistant column's message input, for the dock chat (``ChatPanel``) and the skill chat:
Enter, the main key or the keypad's, sends (:attr:`ComposerInput.submitted`); Shift+Enter
starts a new line."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QPlainTextEdit

_ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)


class ComposerInput(QPlainTextEdit):
    """A multi-line input (the #composerInput look) whose Enter sends."""

    submitted = Signal()

    def __init__(self, placeholder: str, height: int, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("composerInput")
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(height)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.key() in _ENTER_KEYS and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)
