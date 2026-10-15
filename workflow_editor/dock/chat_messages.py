"""The chat's messages, as on the approved board S5 (Bench's look: ``project_services.ui_kit`` and
Bench's stylesheet):

  - :class:`MessageWidget` — one message: a bubble (the user's right and tinted, the
    assistant's left on white, with its "▸ Show thinking" fold and its token line + Details…)
    or, for a system / validator line, a centred pill in the tone its sender gives.
  - :class:`StreamingBubble` — the assistant's bubble while a reply streams in
    ("💭 Thinking… 0:07", "✍ Responding…" and the text so far).
  - :class:`MessageDetailDialog` — the full prompt and response of one message (Details…, or a
    double-click on the message).

``ChatPanel`` lays them out; the colours are the stylesheet's (``QFrame#chatBubble[role]``,
``QLabel#chatSystemLine[pill]``, ``QLabel#thinkingText``, ``QLabel#streamText``).
"""
from __future__ import annotations

import html
import json
import math
import re
from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFont, QMouseEvent, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from project_services import ui_kit

from ..widgets.work_timer import WorkTimer

#: The share of the chat's width a message may take: the user's bubble, the others.
USER_SHARE = 0.86
OTHER_SHARE = 0.92
#: How much of a streaming thought / reply is shown (the tail).
_MAX_THINKING, _MAX_RESPONSE = 3000, 5000


def token_line(total: int, prompt: int, completion: int) -> str:
    """``18,646 tokens · 18,234 prompt + 412 completion``."""
    return f"{total:,} tokens · {prompt:,} prompt + {completion:,} completion"


def _natural_width(label: QLabel) -> int:
    """The width *label*'s text takes on one line per paragraph (its style sheet's font)."""
    label.ensurePolished()
    doc = QTextDocument()
    doc.setDefaultFont(label.font())
    if label.textFormat() == Qt.TextFormat.RichText:
        doc.setHtml(label.text())
    else:
        doc.setPlainText(label.text())
    return math.ceil(doc.idealWidth())


def _text_label(text: str, name: str, rich: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _ThinkingFold:
    """A reply's thought behind a quiet "▸ Show thinking" toggle (#foldToggle): the toggle and
    the hidden label (#thinkingText) it opens and closes."""

    def __init__(self, rich: bool):
        self.label = _text_label("", "thinkingText", rich=rich)
        self.button = QPushButton()
        self.button.setObjectName("foldToggle")
        self.button.setFlat(True)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(lambda: self.set_open(self.label.isHidden()))
        self.set_open(False)

    def set_open(self, shown: bool) -> None:
        self.label.setVisible(shown)
        self.button.setText("▾ Hide thinking" if shown else "▸ Show thinking")


class MessageWidget(QFrame):
    """One chat message (see the module): ``role`` user / assistant / system. A double-click,
    or Details… under an assistant reply, emits ``double_clicked(msg_id)``."""

    double_clicked = Signal(str)  # msg_id (UUID string)

    def __init__(self, role: str, content: str, msg_id: str = "", thinking_content: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
                 tone: str = "neutral", parent=None):
        super().__init__(parent)
        self._msg_id = msg_id
        self.role = role.lower()
        self.share = USER_SHARE if self.role == "user" else OTHER_SHARE
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        if self.role == "system":
            # A centred pill in the line's tone (no bubble around it).
            self.align = Qt.AlignmentFlag.AlignHCenter
            layout.setContentsMargins(0, 0, 0, 0)
            self.content_label = _text_label(content, "chatSystemLine", rich=False)
            self.content_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ui_kit.set_pill(self.content_label, tone, content)
            layout.addWidget(self.content_label)
        else:
            self.setObjectName("chatBubble")
            self.setProperty("role", self.role)
            self.align = (Qt.AlignmentFlag.AlignRight if self.role == "user"
                          else Qt.AlignmentFlag.AlignLeft)
            layout.setContentsMargins(11, 8, 11, 8)
            if thinking_content and self.role == "assistant":
                self._add_thinking(layout, thinking_content)
            if self.role == "user":  # the user's < > & show as typed
                content = html.escape(content)
            self.content_label = _text_label(format_json_in_content(content), "chatText")
            layout.addWidget(self.content_label)
            if total_tokens > 0:
                tokens = ui_kit.note(token_line(total_tokens, prompt_tokens, completion_tokens))
                tokens.setObjectName("chatTokens")
                details = ui_kit.link("Details…", lambda: self.double_clicked.emit(self._msg_id))
                details.setToolTip("The full prompt and response of this message")
                row = ui_kit.hbox(tokens, details)
                row.setAlignment(details, Qt.AlignmentFlag.AlignBottom)
                layout.addLayout(row)
        self.content_label.installEventFilter(self)  # its double-clicks open the details

    def width_for(self, limit: int) -> int:
        """Its width in a chat whose messages may take *limit*: an assistant reply all of it,
        the user's bubble and a system pill as wide as their text (at most *limit*)."""
        if self.role == "assistant":
            return limit
        margins = self.layout().contentsMargins()
        pill_padding = 24 if self.role == "system" else 2  # the pill's padding / the border
        natural = _natural_width(self.content_label) + margins.left() + margins.right() + pill_padding
        return min(limit, natural)

    def _add_thinking(self, layout: QVBoxLayout, thinking: str) -> None:
        self._fold = _ThinkingFold(rich=True)
        self._fold.label.setText(format_json_in_content(thinking))
        layout.addWidget(self._fold.button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._fold.label)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self._msg_id:
            self.double_clicked.emit(self._msg_id)
        super().mouseDoubleClickEvent(event)

    def eventFilter(self, obj, event):
        """The content label's double-clicks (it takes the mouse for selection)."""
        if event.type() == QEvent.Type.MouseButtonDblClick:
            if self._msg_id:
                self.double_clicked.emit(self._msg_id)
            return True
        return super().eventFilter(obj, event)


class StreamingBubble(QFrame):
    """The assistant's bubble while its reply streams in: "💭 Thinking… 0:07" (the thought
    folded behind "▸ Show thinking"), then "✍ Responding…" and the reply so far, its tail in mono."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatBubble")
        self.setProperty("role", "assistant")
        self.role, self.share, self.align = "assistant", OTHER_SHARE, Qt.AlignmentFlag.AlignLeft
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 8, 11, 8)
        layout.setSpacing(4)
        self._thinking_header = ui_kit.note("💭 Thinking…", wrap=False)
        self._fold = _ThinkingFold(rich=False)
        self._fold.button.setVisible(False)  # until a thought arrives
        layout.addLayout(ui_kit.hbox(self._thinking_header, self._fold.button, None))
        layout.addWidget(self._fold.label)
        self._response_header = ui_kit.note("✍ Responding…", wrap=False)
        self._response_header.setVisible(False)
        layout.addWidget(self._response_header)
        self._response_label = ui_kit.monospace(_text_label("", "streamText", rich=False))
        self._response_label.setVisible(False)
        layout.addWidget(self._response_label)
        self._thinking_text = self._response_text = ""
        self._timer = WorkTimer(on_tick=self._tick, parent=self)
        self._timer.start()

    def width_for(self, limit: int) -> int:
        return limit

    def _tick(self, elapsed: str) -> None:
        if not self._response_text:
            self._thinking_header.setText(f"💭 Thinking… {elapsed}")

    def append_thinking(self, text: str) -> None:
        self._thinking_text += text
        self._fold.button.setVisible(True)
        shown = self._thinking_text[-_MAX_THINKING:]
        self._fold.label.setText(("..." if len(self._thinking_text) > _MAX_THINKING else "") + shown)

    def append_response(self, text: str) -> None:
        if not self._response_text:  # the thought is over: fold it, show the reply
            self._thinking_header.setText("💭 Thinking (done)")
            self._thinking_header.setVisible(bool(self._thinking_text))
            self._fold.set_open(False)
            self._response_header.setVisible(True)
            self._response_label.setVisible(True)
        self._response_text += text
        shown = self._response_text[-_MAX_RESPONSE:]
        self._response_label.setText(("..." if len(self._response_text) > _MAX_RESPONSE else "") + shown)

    def stop(self) -> None:
        self._timer.stop()


def format_json_in_content(content: str) -> str:
    """The message as rich text: JSON in ``` fences pretty-printed, newlines as ``<br>`` (a
    rich-text label ignores plain newlines; literal ``\\n`` sequences count as newlines)."""

    def format_json_match(match):
        try:
            return f"```\n{json.dumps(json.loads(match.group(1)), indent=2)}\n```"
        except (json.JSONDecodeError, ValueError):
            return match.group(0)

    content = re.sub(r'```\n(.*?)\n```', format_json_match, content, flags=re.DOTALL)
    return content.replace('\\n', '\n').replace('\n', '<br>')


class MessageDetailDialog(QDialog):
    """Dialog showing full prompt and response for a message."""

    def __init__(self, prompt: Optional[str], response: Optional[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Message Details")
        self.resize(800, 600)

        layout = QVBoxLayout(self)

        # Single text display
        text_editor = QPlainTextEdit()
        text_editor.setFont(QFont("Consolas", 10))
        text_editor.setReadOnly(True)

        # Build combined content with section headers
        content_parts = []

        if prompt:
            content_parts.append("=" * 80)
            content_parts.append("PROMPT")
            content_parts.append("=" * 80)
            content_parts.append(self._format_json_if_possible(prompt))
            content_parts.append("")  # Empty line
        else:
            content_parts.append("(No prompt recorded)")
            content_parts.append("")

        if response:
            content_parts.append("=" * 80)
            content_parts.append("RESPONSE")
            content_parts.append("=" * 80)
            content_parts.append(self._format_json_if_possible(response))
        else:
            content_parts.append("(No response recorded)")

        text_editor.setPlainText("\n".join(content_parts))
        layout.addWidget(text_editor)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    @staticmethod
    def _format_json_if_possible(text: str) -> str:
        """
        Recursively format JSON, including nested JSON strings.

        Detects and pretty-prints JSON strings embedded within JSON objects,
        supporting multiple levels of nesting.
        """
        if not text:
            return text
        try:
            parsed = json.loads(text)
            # Recursively expand nested JSON strings
            formatted_obj = MessageDetailDialog._recursively_format_nested_json(parsed, max_depth=10)
            return json.dumps(formatted_obj, indent=2, ensure_ascii=True)
        except (json.JSONDecodeError, ValueError, TypeError):
            return text

    @staticmethod
    def _recursively_format_nested_json(obj, max_depth: int):
        """
        Recursively detect and expand JSON strings within a data structure.

        Args:
            obj: The object to process (dict, list, str, or primitive)
            max_depth: Maximum recursion depth to prevent infinite loops

        Returns:
            Object with nested JSON strings expanded to dicts/lists
        """
        if max_depth <= 0:
            return obj

        if isinstance(obj, dict):
            return {k: MessageDetailDialog._recursively_format_nested_json(v, max_depth - 1)
                    for k, v in obj.items()}

        elif isinstance(obj, list):
            return [MessageDetailDialog._recursively_format_nested_json(item, max_depth - 1)
                    for item in obj]

        elif isinstance(obj, str):
            if len(obj) < 2:
                return obj

            stripped = obj.strip()
            if not (stripped.startswith('{') or stripped.startswith('[')):
                return obj

            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, (dict, list)):
                    return MessageDetailDialog._recursively_format_nested_json(parsed, max_depth - 1)
                else:
                    return obj
            except (json.JSONDecodeError, ValueError, TypeError):
                return obj

        else:
            return obj
