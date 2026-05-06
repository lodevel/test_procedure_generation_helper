"""Per-message Qt widgets used inside the chat panel.

Extracted from ``chat_panel.py`` (which now re-imports them) so each
class lives at a size that fits in a reader's head:

  - :class:`MessageDetailDialog` — modal that shows the full prompt +
    response of a single ChatMessage when the operator double-clicks it.
  - :class:`ProposalWidget` — accept / reject / view-diff card for an
    LLM-proposed artifact change.
  - :class:`MessageWidget` — one row in the chat scroll area; carries
    role-styling, thinking-content collapse, and the double-click signal
    that opens :class:`MessageDetailDialog`.

The split is purely organisational; no behaviour changed. ``ChatPanel``
is the only consumer; the classes don't import each other except for
``MessageDetailDialog`` calling its own static helpers recursively.
"""
from __future__ import annotations

import html
import json
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QFont, QMouseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import theme

log = logging.getLogger(__name__)


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


class ProposalWidget(QFrame):
    """Widget showing a code/JSON proposal with accept/reject actions."""

    accepted = Signal(str, str)  # (artifact_type, content)
    rejected = Signal(str)  # artifact_type
    view_diff_requested = Signal(str)  # artifact_type

    def __init__(self, artifact_name: str, content: str, artifact_type: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self._artifact_type = artifact_type
        self._content = content
        self._artifact_name = artifact_name

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(5)

        # Header
        header = QLabel(f"📄 Proposal: {artifact_name}")
        header.setStyleSheet("font-weight: bold;")
        layout.addWidget(header)

        # Preview (first 8 lines)
        preview_lines = content.split('\n')[:8]
        preview_text = '\n'.join(preview_lines)
        if len(content.split('\n')) > 8:
            preview_text += '\n...'

        self.preview = QPlainTextEdit()
        self.preview.setPlainText(preview_text)
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(150)
        self.preview.setFont(QFont("Consolas", 9))
        layout.addWidget(self.preview)

        # Buttons
        btn_layout = QHBoxLayout()
        self.accept_btn = QPushButton("✓ Accept")
        self.accept_btn.setStyleSheet(f"background-color: {theme.accept_btn_bg()};")
        self.accept_btn.clicked.connect(self._on_accept)
        self.reject_btn = QPushButton("✗ Reject")
        self.reject_btn.setStyleSheet(f"background-color: {theme.reject_btn_bg()};")
        self.reject_btn.clicked.connect(self._on_reject)
        self.diff_btn = QPushButton("View Diff")
        self.diff_btn.clicked.connect(self._on_view_diff)

        btn_layout.addWidget(self.accept_btn)
        btn_layout.addWidget(self.reject_btn)
        btn_layout.addWidget(self.diff_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Style
        self.setStyleSheet(f"""
            ProposalWidget {{
                background-color: {theme.proposal_bg()};
                border: 1px solid {theme.proposal_border()};
                border-radius: 5px;
            }}
        """)

    def _on_accept(self):
        self.accepted.emit(self._artifact_type, self._content)
        self._set_handled("Accepted ✓")

    def _on_reject(self):
        self.rejected.emit(self._artifact_type)
        self._set_handled("Rejected ✗")

    def _on_view_diff(self):
        self.view_diff_requested.emit(self._artifact_type)

    def _set_handled(self, status: str):
        """Disable buttons and show status after handling."""
        self.accept_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.diff_btn.setEnabled(False)
        self.setStyleSheet(f"""
            ProposalWidget {{
                background-color: {theme.proposal_handled_bg()};
                border: 1px solid {theme.proposal_handled_border()};
                border-radius: 5px;
            }}
        """)
        # Update header with status
        header = self.layout().itemAt(0).widget()
        if header:
            header.setText(f"📄 {self._artifact_name}: {status}")


class MessageWidget(QFrame):
    """A single chat message display."""

    # Signal emitted when message is double-clicked
    double_clicked = Signal(str)  # msg_id (UUID string)

    def __init__(self, role: str, content: str, msg_id: str = "", thinking_content: str = "", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self._msg_id = msg_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)

        # Role header
        role_label = QLabel(role.upper())
        role_label.setStyleSheet("font-weight: bold; font-size: 10px;")
        layout.addWidget(role_label)

        # Thinking/reasoning section (collapsible, for assistant messages)
        if thinking_content and role.lower() == "assistant":
            self._thinking_visible = False
            self._toggle_btn = QPushButton("▶ Show thinking")
            self._toggle_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {theme.toggle_color()}; "
                "font-size: 10px; font-style: italic; text-align: left; padding: 2px 0; }"
                f"QPushButton:hover {{ color: {theme.toggle_hover_color()}; }}"
            )
            self._toggle_btn.setCursor(Qt.PointingHandCursor)
            self._toggle_btn.clicked.connect(self._toggle_thinking)
            layout.addWidget(self._toggle_btn)

            formatted_thinking = self._format_json_in_content(thinking_content)
            self._thinking_label = QLabel(formatted_thinking)
            self._thinking_label.setWordWrap(True)
            self._thinking_label.setTextFormat(Qt.RichText)
            self._thinking_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._thinking_label.setStyleSheet(
                f"color: {theme.thinking_fg()}; font-style: italic; font-size: 11px; "
                f"padding: 4px 8px; background-color: {theme.thinking_bg()}; "
                f"border-left: 2px solid {theme.thinking_border()};"
            )
            self._thinking_label.setVisible(False)
            layout.addWidget(self._thinking_label)

        # Escape HTML in user messages so characters like < > & display literally
        if role.lower() == "user":
            content = html.escape(content)

        # Content - format JSON if found
        formatted_content = self._format_json_in_content(content)

        # Content - store as instance variable for updates
        self.content_label = QLabel(formatted_content)
        self.content_label.setWordWrap(True)
        self.content_label.setTextFormat(Qt.RichText)
        self.content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Install event filter to catch double-clicks on label
        self.content_label.installEventFilter(self)
        layout.addWidget(self.content_label)

        # Style based on role
        if role.lower() == "user":
            self.setStyleSheet(f"""
                MessageWidget {{
                    background-color: {theme.message_bg('user')};
                    border: 1px solid {theme.message_border('user')};
                    border-radius: 5px;
                }}
            """)
        elif role.lower() == "assistant":
            self.setStyleSheet(f"""
                MessageWidget {{
                    background-color: {theme.message_bg('assistant')};
                    border: 1px solid {theme.message_border('assistant')};
                    border-radius: 5px;
                }}
            """)
        elif role.lower() == "system":
            self.setStyleSheet(f"""
                MessageWidget {{
                    background-color: {theme.message_bg('system')};
                    border: 1px solid {theme.message_border('system')};
                    border-radius: 5px;
                }}
            """)

    def _toggle_thinking(self):
        """Toggle visibility of thinking/reasoning content."""
        if not hasattr(self, '_thinking_label'):
            return
        self._thinking_visible = not self._thinking_visible
        self._thinking_label.setVisible(self._thinking_visible)
        if self._thinking_visible:
            self._toggle_btn.setText("▼ Hide thinking")
        else:
            self._toggle_btn.setText("▶ Show thinking")

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Handle double-click to show message details."""
        log.debug("MessageWidget.mouseDoubleClickEvent: msg_id=%s", self._msg_id)
        if self._msg_id:
            log.debug("Emitting double_clicked signal for msg_id=%s", self._msg_id)
            self.double_clicked.emit(self._msg_id)
        super().mouseDoubleClickEvent(event)

    def eventFilter(self, obj, event):
        """Catch double-clicks on child widgets (especially the content label)."""
        if event.type() == QEvent.Type.MouseButtonDblClick:
            log.debug("eventFilter caught double-click on %s, msg_id=%s", obj.__class__.__name__, self._msg_id)
            if self._msg_id:
                log.debug("eventFilter emitting double_clicked signal for msg_id=%s", self._msg_id)
                self.double_clicked.emit(self._msg_id)
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _format_json_in_content(content: str) -> str:
        """Try to find and format JSON blocks in content for better readability.

        Also converts newlines to <br> tags for proper HTML rendering,
        since QLabel with RichText format ignores plain newlines.
        """
        # Look for JSON blocks in markdown code fences
        import re

        def format_json_match(match):
            json_text = match.group(1)
            try:
                parsed = json.loads(json_text)
                formatted = json.dumps(parsed, indent=2)
                return f"```\n{formatted}\n```"
            except (json.JSONDecodeError, ValueError):
                return match.group(0)  # Return original if not valid JSON

        # Try to format JSON in code blocks
        content = re.sub(r'```\n(.*?)\n```', format_json_match, content, flags=re.DOTALL)

        # Handle literal \n escape sequences that weren't decoded
        # (occurs when text contains double-escaped \\n from some sources)
        content = content.replace('\\n', '\n')

        # Convert newlines to <br> tags for HTML rendering
        # QLabel with Qt.RichText format ignores plain \n characters
        content = content.replace('\n', '<br>')

        return content
