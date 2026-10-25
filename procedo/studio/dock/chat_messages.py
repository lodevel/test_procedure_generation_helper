"""The chat's messages, as on the approved board S5 (Bench's look: ``procedo.ui.ui_kit`` and
Bench's stylesheet):

  - :class:`MessageWidget` — one message: a bubble (the user's right and tinted, the
    assistant's left on white, with its "▸ Show thinking" fold and its token line + Details…)
    or, for a system / validator line, a centred pill in the tone its sender gives.
  - :class:`StreamingBubble` — the assistant's bubble while a reply streams in
    ("💭 Thinking… 0:07", "✍ Responding…" and the text so far).
  - :class:`ProposalCard` — a change waiting for the user (a :class:`ProposalMessage`): the file,
    +a −r, the first changed lines, ✓ Accept / ✗ Reject / View diff….
  - :class:`MessageDetailDialog` — the full prompt and response of one message (Details…, or a
    double-click on the message).

``ChatPanel`` lays them out; the colours are the stylesheet's (``QFrame#chatBubble[role]``,
``QLabel#chatSystemLine[pill]``, ``QLabel#thinkingText``, ``QLabel#streamText``,
``QLabel#proposalPreview``).
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QMouseEvent, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from procedo.ui import ui_kit

from ..core.proposal import APPLIED, DISCARDED, PENDING, REJECTED, Proposal
from ..dialogs.diff_viewer import DiffViewer
from ..llm.tab_context import ChatMessage
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

    def __init__(
        self,
        role: str,
        content: str,
        msg_id: str = "",
        thinking_content: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        tone: str = "neutral",
        parent=None,
    ):
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
            self.align = (
                Qt.AlignmentFlag.AlignRight if self.role == "user" else Qt.AlignmentFlag.AlignLeft
            )
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
        natural = (
            _natural_width(self.content_label) + margins.left() + margins.right() + pill_padding
        )
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
        self._fold.label.setText(
            ("..." if len(self._thinking_text) > _MAX_THINKING else "") + shown
        )

    def append_response(self, text: str) -> None:
        if not self._response_text:  # the thought is over: fold it, show the reply
            self._thinking_header.setText("💭 Thinking (done)")
            self._thinking_header.setVisible(bool(self._thinking_text))
            self._fold.set_open(False)
            self._response_header.setVisible(True)
            self._response_label.setVisible(True)
        self._response_text += text
        shown = self._response_text[-_MAX_RESPONSE:]
        self._response_label.setText(
            ("..." if len(self._response_text) > _MAX_RESPONSE else "") + shown
        )

    def stop(self) -> None:
        self._timer.stop()


@dataclass
class ProposalMessage(ChatMessage):
    """A chat message that is a proposal card; its ``content`` is the history's one line
    (:meth:`Proposal.summary`). It lives in the view's history, so the card survives a view
    switch."""

    proposal: Optional[Proposal] = None


#: A proposal whose test is no longer open (the chat line, the card).
CLOSED = "Discarded: its test was closed"
#: What a decided card says instead of its buttons (pill tone, text).
_OUTCOMES = {
    APPLIED: ("success", "✓ Applied"),
    REJECTED: ("danger", "✗ Rejected"),
    DISCARDED: ("neutral", CLOSED),
}
_STALE = "⚠ The file changed since: use View diff…"
_OUTDATED = "⚠ {} changed since: run it again"
_FAILED = "✗ {}"
_INVALID = "⚠ Failed validation: see View diff…"


def review_proposal(proposal: Proposal, parent=None) -> None:
    """View diff…: each file of *proposal* against its CURRENT text in the diff window, in turn.
    Accept on the last one applies it (the user saw what it replaces, stale or not), Reject on
    any rejects it, closing a window decides nothing."""
    files = len(proposal.changes)
    for number, change in enumerate(proposal.changes, 1):
        decision = DiffViewer.review(
            change.read(),
            change.proposed,
            change.file_name,
            parent,
            proposal.banner,
            (number, files),
        )
        if decision != "accept":
            if decision == "reject":
                proposal.reject()
            return
    proposal.accept(reviewed=True)


class ProposalCard(QFrame):
    """A proposal waiting in the chat (board S5): "Proposal", its file, +a −r, the first changed
    lines, then ✓ Accept / ✗ Reject / View diff… — once decided, what became of it. A stale one
    (a file changed since) offers no Accept: only View diff…, against the current text; an
    outdated one (a file it was made from changed) only Reject. An Accept that failed says why.

    The decision is the :class:`Proposal`'s, which outlives the card (the chat redraws its
    messages on a view switch); ``decided`` asks the chat to redraw its other cards."""

    decided = Signal()

    def __init__(self, proposal: Proposal, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.proposal = proposal
        self.role, self.share, self.align = "proposal", 1.0, Qt.AlignmentFlag.AlignLeft
        added, removed = proposal.counts()
        files = ui_kit.monospace(QLabel(" + ".join(proposal.file_names)))
        files.setObjectName("proposalFiles")
        head = ui_kit.hbox(
            ui_kit.bold("Proposal"),
            files,
            None,
            ui_kit.set_pill(QLabel(), "info", f"+{added} −{removed}"),
        )
        head.setContentsMargins(11, 8, 11, 8)
        head.setSpacing(8)
        preview = ui_kit.monospace(
            _text_label("\n".join(proposal.preview()), "proposalPreview", rich=False)
        )
        self.state_line = QLabel()  # one line: a pill
        self.state_line.setObjectName("proposalState")
        self.accept_button = QPushButton("✓ Accept")
        ui_kit.set_primary(self.accept_button)
        self.accept_button.clicked.connect(self._on_accept)
        self.reject_button = ui_kit.set_state(QPushButton("✗ Reject"), "danger", True)
        self.reject_button.clicked.connect(self._on_reject)
        self.diff_button = QPushButton("View diff…")
        self.diff_button.setToolTip("Compare it with the current text, then accept or reject it")
        self.diff_button.clicked.connect(self._on_view_diff)
        self.actions = QWidget()
        buttons = ui_kit.hbox(self.accept_button, self.reject_button, None, self.diff_button)
        buttons.setSpacing(6)
        self.actions.setLayout(buttons)
        foot = QVBoxLayout()
        foot.setContentsMargins(11, 8, 11, 8)
        foot.setSpacing(6)
        foot.addWidget(self.state_line, 0, Qt.AlignmentFlag.AlignLeft)
        foot.addWidget(self.actions)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 0, 1, 1)  # inside the card's border
        layout.setSpacing(0)
        layout.addLayout(head)
        layout.addWidget(ui_kit.row_rule())
        layout.addWidget(preview)
        layout.addWidget(ui_kit.row_rule())
        layout.addLayout(foot)
        self.refresh()

    def width_for(self, limit: int) -> int:
        return limit

    def refresh(self) -> None:
        """Show where the proposal stands now (a file may have changed since it was drawn)."""
        proposal = self.proposal
        pending, stale, outdated = proposal.state == PENDING, proposal.stale(), proposal.outdated()
        self.actions.setVisible(pending)
        self.accept_button.setVisible(not proposal.needs_review())
        self.diff_button.setVisible(not outdated)
        tip = ""
        if not pending:
            tone, text = _OUTCOMES[proposal.state]
        elif proposal.error:
            tone, text, tip = "danger", _FAILED.format(proposal.error), proposal.error
        elif outdated:
            tone, text = "warning", _OUTDATED.format(" + ".join(outdated))
        elif stale:
            tone, text, tip = "warning", _STALE, "View diff… compares it with the current text"
        else:
            tone, text, tip = "warning", _INVALID if proposal.banner else "", proposal.banner
        self.state_line.setVisible(bool(text))
        ui_kit.set_pill(self.state_line, tone, text)
        self.state_line.setToolTip(tip)

    def _on_accept(self) -> None:
        self.proposal.accept()  # refused when a file changed since: the card then says so
        self._decided()

    def _on_reject(self) -> None:
        self.proposal.reject()
        self._decided()

    def _on_view_diff(self) -> None:
        review_proposal(self.proposal, self.window())
        self._decided()

    def _decided(self) -> None:
        self.refresh()
        self.decided.emit()


def format_json_in_content(content: str) -> str:
    """The message as rich text: JSON in ``` fences pretty-printed, newlines as ``<br>`` (a
    rich-text label ignores plain newlines; literal ``\\n`` sequences count as newlines)."""

    def format_json_match(match):
        try:
            return f"```\n{json.dumps(json.loads(match.group(1)), indent=2)}\n```"
        except (json.JSONDecodeError, ValueError):
            return match.group(0)

    content = re.sub(r"```\n(.*?)\n```", format_json_match, content, flags=re.DOTALL)
    return content.replace("\\n", "\n").replace("\n", "<br>")


#: The details window's subtitle, by the message's role.
_SENDERS = {"assistant": "Assistant reply", "user": "Your message", "system": "System message"}


class MessageDetailDialog(QDialog):
    """The full prompt and response of *message* (a ``ChatMessage``), as on the Small board's
    Message details: who sent it and its tokens in the header, one monospace card with the
    PROMPT and RESPONSE sections (JSON pretty-printed), Copy and the primary Close."""

    def __init__(self, message: ChatMessage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Message Details")
        self.resize(800, 600)
        subtitle = _SENDERS.get(message.role, message.role.capitalize())
        if message.total_tokens:
            subtitle += f" · {message.total_tokens:,} tokens"
        self.text = ui_kit.monospace(QPlainTextEdit())
        self.text.setObjectName("cardEditor")
        self.text.setReadOnly(True)
        self.text.setPlainText(self._details(message.full_prompt, message.full_response))
        copy = QPushButton("Copy")
        copy.setToolTip("Copy the prompt and response to the clipboard")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        close = ui_kit.primary(QPushButton("Close"))
        close.clicked.connect(self.accept)
        body = ui_kit.framed(
            self,
            ui_kit.HeaderBand("Message details", subtitle, stacked=True),
            ui_kit.FooterBand([copy, close]),
            margins=(16, 12, 16, 12),
        )
        card = ui_kit.card_frame()
        inside = QVBoxLayout(card)
        inside.setContentsMargins(10, 4, 10, 4)
        inside.addWidget(self.text)
        body.addWidget(card, 1)
        self.text.setFocus()  # not Copy: the text scrolls by keyboard

    @staticmethod
    def _details(prompt: Optional[str], response: Optional[str]) -> str:
        """The PROMPT and RESPONSE sections, each under its ruled heading."""
        content_parts = []

        if prompt:
            content_parts.append("=" * 80)
            content_parts.append("PROMPT")
            content_parts.append("=" * 80)
            content_parts.append(MessageDetailDialog._format_json_if_possible(prompt))
            content_parts.append("")  # Empty line
        else:
            content_parts.append("(No prompt recorded)")
            content_parts.append("")

        if response:
            content_parts.append("=" * 80)
            content_parts.append("RESPONSE")
            content_parts.append("=" * 80)
            content_parts.append(MessageDetailDialog._format_json_if_possible(response))
        else:
            content_parts.append("(No response recorded)")
        return "\n".join(content_parts)

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
            formatted_obj = MessageDetailDialog._recursively_format_nested_json(
                parsed, max_depth=10
            )
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
            return {
                k: MessageDetailDialog._recursively_format_nested_json(v, max_depth - 1)
                for k, v in obj.items()
            }

        elif isinstance(obj, list):
            return [
                MessageDetailDialog._recursively_format_nested_json(item, max_depth - 1)
                for item in obj
            ]

        elif isinstance(obj, str):
            if len(obj) < 2:
                return obj

            stripped = obj.strip()
            if not (stripped.startswith("{") or stripped.startswith("[")):
                return obj

            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, (dict, list)):
                    return MessageDetailDialog._recursively_format_nested_json(
                        parsed, max_depth - 1
                    )
                else:
                    return obj
            except (json.JSONDecodeError, ValueError, TypeError):
                return obj

        else:
            return obj
