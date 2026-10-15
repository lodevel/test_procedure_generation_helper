"""
Chat Panel - the Assistant column's conversation, as on the approved board S5.

The Chat tab is the messages (``chat_messages``: bubbles, system / validator pills, the
streaming bubble); :attr:`ChatPanel.composer` is the column's foot under every tab (the dock
lays it out): Board data / Force: resend all / Auto-correct, the input, Send, stop / clear /
compact, the context meter and the validator dot.
"""

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QFrame, QPlainTextEdit,
    QPushButton, QCheckBox, QProgressBar,
)
from PySide6.QtCore import Qt, Signal, QEvent
from typing import TYPE_CHECKING, Optional

from project_services import ui_kit

from .chat_messages import MessageDetailDialog, MessageWidget, StreamingBubble
from ..llm.context_usage import format_context_usage, latest_message_total
from .board_data_checkbox import BoardDataCheckBox
from ..llm.tab_context import ChatMessage


# Tooltip strings for the validator-status indicator + auto-correct
# checkbox. Co-located so the wording stays consistent and a future
# i18n pass can sweep one block. The grey/unavailable variants take an
# optional ``reason`` interpolation appended at format time.
_TOOLTIP_STATUS_AVAILABLE = (
    "Deterministic validator active.\n"
    "Quick Parse / Quick Code work; LLM responses are checked "
    "before the DiffViewer."
)
_TOOLTIP_STATUS_UNAVAILABLE = (
    "Deterministic validator unavailable; LLM-only workflow.\n"
    "Quick Parse / Quick Code buttons are inactive."
)
_TOOLTIP_AUTO_CORRECT_AVAILABLE = (
    "When the deterministic validator rejects an LLM response, "
    "automatically re-prompt the LLM with the structured errors "
    "(up to N retries) before falling back to operator review."
)
_TOOLTIP_AUTO_CORRECT_UNAVAILABLE = (
    "Deterministic validator not available — auto-correct loop has "
    "nothing to validate against. Falls back to operator-only "
    "DiffViewer review."
)
#: The context meter's resolution (the bar's maximum).
_METER_STEPS = 1000


def _with_reason(base: str, reason: str) -> str:
    """Append a ``Reason: ...`` line to a tooltip when one was supplied."""
    return f"{base}\nReason: {reason}" if reason else base

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..llm import TabContext


log = logging.getLogger(__name__)



class ChatPanel(QWidget):
    """
    Chat panel for LLM conversation: the messages of the current view's TabContext, and the
    composer (:attr:`composer`) that sends, cancels, resets and compacts.
    """

    # Signals
    message_sent = Signal(str)  # Emitted when user sends a message
    reset_requested = Signal()  # Emitted when user clicks reset button
    cancel_requested = Signal()  # Emitted when user clicks cancel button
    compact_requested = Signal()  # Emitted when user clicks compact-session

    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._cumulative_tokens = 0  # Track total tokens used in conversation
        # Compaction-correct readout: the LATEST turn's reported total
        # tokens (input+output), NOT the running sum above. OpenCode
        # reports this per turn and it drops after a compact.
        self._latest_total_tokens = 0
        self._context_limit = 16384  # Default, will be updated by main_window
        self._current_tab_context: Optional["TabContext"] = None  # Currently displayed tab context
        # Stored auto-correct preference (decoupled from the checkbox's
        # visual state). When the validator is unavailable the checkbox
        # is forced unchecked + disabled but this remembers the user's
        # actual intent so a later set_validator_status(True) restores it.
        self._stored_auto_correct: bool = True
        self._streaming: Optional[StreamingBubble] = None

        self._setup_ui()
        self.composer = self._build_composer()
        self._update_context_label()

    def set_context_limit(self, limit: int):
        """Set the model's context limit for token tracking."""
        self._context_limit = limit
        self._update_context_label()

    def switch_context(self, tab_context: Optional["TabContext"]):
        """
        Switch to a different tab's conversation context.

        This clears the current chat display and loads the messages
        from the specified tab's TabContext.

        Args:
            tab_context: The TabContext to switch to, or None to clear
        """
        log.debug("switch_context called with tab_context=%s", 'None' if tab_context is None else tab_context.tab_id)
        self._current_tab_context = tab_context
        self.board_data_checkbox.set_context(tab_context)

        # Clear current display
        self.clear_messages()

        if tab_context is None:
            self._cumulative_tokens = 0
            self._latest_total_tokens = 0
            self._update_context_label()
            return

        # Load messages from tab context
        log.info("switch_context: loading %d messages from TabContext '%s'", len(tab_context.messages), tab_context.tab_id)
        for msg in tab_context.messages:
            self._add_message_widget(msg)  # already stored in the TabContext

        # Update token counter
        log.debug("switch_context loading cumulative_tokens=%s from TabContext", tab_context.cumulative_tokens)
        self._cumulative_tokens = tab_context.cumulative_tokens
        self._latest_total_tokens = latest_message_total(tab_context.messages)
        self._update_context_label()

        # Refresh the validator UI (status indicator + auto-correct
        # toggle restoration) for the newly active tab.
        self._refresh_validator_ui_for_context(tab_context)

    def _add_message_widget(self, msg: "ChatMessage"):
        """Show one message (its token line drawn from the counts, a system line in its tone),
        without touching the TabContext."""
        msg_widget = MessageWidget(msg.role, msg.content, msg_id=msg.msg_id,
                                   thinking_content=msg.thinking_content,
                                   prompt_tokens=msg.prompt_tokens,
                                   completion_tokens=msg.completion_tokens,
                                   total_tokens=msg.total_tokens, tone=msg.tone)
        msg_widget.double_clicked.connect(self._on_message_double_clicked)
        self._insert(msg_widget)

    def _insert(self, widget) -> None:
        """Add a message (or the streaming bubble) at the end, on its side, and follow it."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        if widget.align != Qt.AlignmentFlag.AlignLeft:
            line.addStretch(1)
        line.addWidget(widget)
        if widget.align != Qt.AlignmentFlag.AlignRight:
            line.addStretch(1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, row)
        self._fit(widget)
        self._scroll_to_bottom()

    def _fit(self, widget) -> None:
        """A message takes at most its share of the chat's width (``width_for``)."""
        margins = self.messages_layout.contentsMargins()
        width = self.scroll_area.viewport().width() - margins.left() - margins.right()
        widget.setFixedWidth(widget.width_for(max(120, int(width * widget.share))))

    def _messages(self) -> list:
        """The shown messages (the streaming bubble included), each in its row."""
        rows = (self.messages_layout.itemAt(i).widget() for i in range(self.messages_layout.count()))
        return [item.widget() for row in rows if row is not None
                for item in (row.layout().itemAt(k) for k in range(row.layout().count()))
                if item.widget() is not None]

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Message scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("chatScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.viewport().setObjectName("chatViewport")
        self.scroll_area.viewport().installEventFilter(self)  # its width bounds the bubbles

        self.messages_container = QWidget()
        self.messages_container.setObjectName("chatMessages")
        self.messages_layout = QVBoxLayout(self.messages_container)
        self.messages_layout.setContentsMargins(12, 12, 12, 12)
        self.messages_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.messages_layout.setSpacing(10)

        # Add stretch at bottom to push messages up
        self.messages_layout.addStretch()

        self.scroll_area.setWidget(self.messages_container)
        layout.addWidget(self.scroll_area, stretch=1)

    def _build_composer(self) -> QFrame:
        """The column's foot (#composer): the three options, the input, Send and the icon
        buttons, the context meter and the validator dot."""
        self.board_data_checkbox = BoardDataCheckBox()
        self.force_mode_checkbox = QCheckBox("Force: resend all")
        self.force_mode_checkbox.setToolTip(
            "Send all artifacts even if not modified (useful for debugging)"
        )
        # Auto-correct checkbox: drives the validator-in-the-loop FSM.
        # Defaults to ON when validators are available (set by
        # set_validator_status); greyed-out + tooltip explained otherwise.
        # Persisted per-project in <project>/config/config.json under
        # the ``validator_loop`` section — read by switch_context, written
        # by the toggled-handler.
        self.auto_correct_checkbox = QCheckBox("Auto-correct")
        self.auto_correct_checkbox.setChecked(True)
        self.auto_correct_checkbox.setToolTip(_TOOLTIP_AUTO_CORRECT_AVAILABLE)
        self.auto_correct_checkbox.toggled.connect(self._on_auto_correct_toggled)
        options = QVBoxLayout()
        options.setSpacing(6)
        options.addLayout(ui_kit.hbox(self.board_data_checkbox, self.force_mode_checkbox, None))
        options.addLayout(ui_kit.hbox(self.auto_correct_checkbox, None))
        options.itemAt(0).layout().setSpacing(14)

        # Input area - multi-line (Enter to send, Shift+Enter for newline)
        self.input_field = QPlainTextEdit()
        self.input_field.setObjectName("composerInput")
        self.input_field.setPlaceholderText("Ask a question or give instructions…")
        self.input_field.setFixedHeight(58)
        self.input_field.installEventFilter(self)

        self.send_btn = QPushButton("Send")
        ui_kit.set_primary(self.send_btn)
        self.send_btn.clicked.connect(self._on_send)
        self.cancel_btn = ui_kit.icon_button("stop", "Cancel the current LLM request")
        self.cancel_btn.setEnabled(False)  # Disabled until request starts
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        self.reset_btn = ui_kit.icon_button("trash", "Reset the LLM session")
        self.reset_btn.clicked.connect(self._on_reset)
        self.compact_btn = ui_kit.icon_button(
            "compact", "Compact the LLM session (summarize history to free up context when it gets long)")
        self.compact_btn.clicked.connect(self.compact_requested.emit)
        buttons = ui_kit.hbox(self.send_btn, None, self.cancel_btn, self.reset_btn, self.compact_btn)
        buttons.setSpacing(6)

        # The context meter, and the validator-status dot: green = deterministic path
        # active, grey = unavailable. Operators read it to know whether the auto-correct
        # loop / Quick Parse buttons will do anything. set_validator_status() (called by the
        # tabs on project load / activation) shows it and owns its tooltips.
        self.context_label = ui_kit.note(wrap=False)
        self.context_label.setObjectName("contextText")
        self.context_bar = QProgressBar()
        self.context_bar.setObjectName("contextMeter")
        self.context_bar.setRange(0, _METER_STEPS)
        self.context_bar.setTextVisible(False)
        self.validator_dot = ui_kit.note("●", wrap=False)
        self.validator_dot.setObjectName("statusDot")
        validator_word = ui_kit.note("validator", wrap=False)
        validator_word.setObjectName("contextText")
        self.validator_status = QWidget()
        self.validator_status.setLayout(ui_kit.hbox(self.validator_dot, validator_word))
        self.validator_status.layout().setSpacing(4)
        self.validator_status.setVisible(False)
        meter = ui_kit.hbox(self.context_label, self.context_bar, self.validator_status)
        meter.setStretch(1, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        column = QVBoxLayout(composer)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(8)
        for row in (options, buttons, meter):
            column.addLayout(row)
        column.insertWidget(1, self.input_field)
        return composer

    def eventFilter(self, obj, event):
        """Enter in the input sends (Shift+Enter inserts a newline); the chat's width bounds
        the messages. Told apart by object name: no state read before it is needed."""
        if event.type() == QEvent.Type.KeyPress and obj.objectName() == "composerInput":
            if event.key() == Qt.Key.Key_Return and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._on_send()
                return True
        elif event.type() == QEvent.Type.Resize and obj.objectName() == "chatViewport":
            for message in self._messages():
                self._fit(message)
        return super().eventFilter(obj, event)

    def _on_send(self):
        """Handle send button click."""
        text = self.input_field.toPlainText().strip()
        if not text:
            return

        self.input_field.clear()
        # Note: Don't add message here - main_window.run_llm_task handles it

        # Emit signal for main window to handle
        self.message_sent.emit(text)

    def _on_reset(self):
        """Handle reset button click."""
        self.clear_messages()
        self._cumulative_tokens = self._latest_total_tokens = 0
        self._update_context_label()
        self.reset_requested.emit()

    def get_force_mode(self) -> bool:
        """Get the current force mode state."""
        return self.force_mode_checkbox.isChecked()

    def get_auto_correct_enabled(self) -> bool:
        """Whether the validator-in-the-loop auto-retry is active.

        Always returns the checkbox state — when validators are
        unavailable the checkbox is greyed out (set in
        :meth:`set_validator_status`) so its checked state effectively
        no-ops. The mixin further short-circuits via
        ``deterministic_path_available`` before reaching this method.
        """
        return self.auto_correct_checkbox.isChecked()

    def set_auto_correct_enabled(self, enabled: bool) -> None:
        """Persist the operator's stored preference into the checkbox
        without firing the toggled signal. Called on project load to
        restore the per-project setting from ``config.json``.

        The user's intent is also remembered in ``_stored_auto_correct``
        so :meth:`set_validator_status` can force the checkbox visually
        unchecked when the validator is unavailable / disabled, while
        preserving the preference for the next time the validator
        becomes available.
        """
        self._stored_auto_correct = bool(enabled)
        self.auto_correct_checkbox.blockSignals(True)
        try:
            self.auto_correct_checkbox.setChecked(bool(enabled))
        finally:
            self.auto_correct_checkbox.blockSignals(False)

    def _refresh_validator_ui_for_context(self, tab_context) -> None:
        """Probe validator availability for the tab's project and update
        the chat-panel surfaces in lockstep:

          1. Validator-status indicator dot (green/grey + tooltip).
          2. Auto-correct checkbox enabled state.
          3. Restore the persisted per-project auto-correct preference.

        Steps 1–2 happen via :meth:`set_validator_status` (greying the
        checkbox out when unavailable); step 3 happens after, so a
        project pinned to a validator-less ruleset never briefly flashes
        the checkbox enabled. The validator_dispatch import is lazy so
        chat_panel.py doesn't drag rules_packager_base into every
        editor startup."""
        try:
            from ..llm.validator_dispatch import is_loop_available
            project_root = self._current_project_root(tab_context)
            available, reason = is_loop_available(project_root)
            self.set_validator_status(available, reason)
        except Exception:
            log.exception("validator-status probe failed; hiding indicator")
            self.validator_status.setVisible(False)
        self._load_validator_loop_for_context(tab_context)

    def _on_auto_correct_toggled(self, checked: bool) -> None:
        """Persist the toggle change to ``<project>/config/config.json``.

        Writes ``validator_loop.auto_correct`` (NOT ``enabled`` — that
        key is the project-level master toggle owned by Settings →
        Validator; Phase 4.6 split the two concepts apart).
        """
        # Update the stored preference even if no project is open —
        # ensures a later switch_context restores the right state.
        self._stored_auto_correct = bool(checked)
        project_root = self._current_project_root()
        if project_root is None:
            return
        from ..llm.validator_loop_settings import save_setting
        save_setting(project_root, "auto_correct", bool(checked))

    def _load_validator_loop_for_context(self, tab_context) -> None:
        """Read the persisted ``validator_loop`` section from the active
        project's ``config.json`` and reflect it in the checkbox state."""
        project_root = self._current_project_root(tab_context)
        if project_root is None:
            return
        from ..llm.validator_loop_settings import load_settings
        section = load_settings(project_root)
        if "auto_correct" in section:
            self.set_auto_correct_enabled(bool(section["auto_correct"]))
        else:
            # No persisted preference: default to True so a freshly-loaded
            # project with the validator available enters the loop by
            # default. Operators can opt out via the checkbox.
            self.set_auto_correct_enabled(True)

    def _current_project_root(self, tab_context=None):
        """Resolve the active project root from the supplied context (or
        fall back to ``self._current_tab_context``). Returns ``None``
        when no project is bound — callers must treat that as a no-op,
        not an error."""
        ctx = tab_context if tab_context is not None else self._current_tab_context
        if ctx is None:
            return None
        return getattr(getattr(ctx, "project_manager", None), "project_root", None)

    def add_message(self, role: str, content: str, full_prompt: Optional[str] = None, full_response: Optional[str] = None, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0, tone: str = "neutral"):
        """Add a message to the chat (and to the current TabContext's history).

        Args:
            role: 'user', 'assistant', or 'system'
            content: Display text for the message
            full_prompt: Full prompt sent to LLM (for debugging)
            full_response: Full raw response from LLM (for debugging)
            prompt_tokens: Number of tokens in prompt
            completion_tokens: Number of tokens in completion
            total_tokens: Total tokens for this message
            tone: a system line's pill tone (success / warning / danger / neutral)
        """

        if role.lower() == "assistant" and total_tokens > 0:
            self._cumulative_tokens += total_tokens
            self._latest_total_tokens = total_tokens
            # Keep TabContext.cumulative_tokens in step so the count survives a tab switch.
            if self._current_tab_context is not None:
                self._current_tab_context.cumulative_tokens = self._cumulative_tokens
            self._update_context_label()

        chat_message = ChatMessage(
            role=role,
            content=content,
            full_prompt=full_prompt,
            full_response=full_response,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tone=tone,
        )
        if self._current_tab_context is not None:  # stored there for persistence
            self._current_tab_context.messages.append(chat_message)
        self._add_message_widget(chat_message)

    def _on_message_double_clicked(self, msg_id: str):
        """Show a message's full prompt and response (Details…, or a double-click)."""
        if self._current_tab_context is None:
            return
        msg = next((m for m in self._current_tab_context.messages if getattr(m, 'msg_id', None) == msg_id), None)
        if msg:
            dialog = MessageDetailDialog(getattr(msg, 'full_prompt', None), getattr(msg, 'full_response', None), self)
            dialog.exec()

    def add_system_message(self, content: str, full_prompt: Optional[str] = None, tone: str = "neutral"):
        """Add a system line, a pill in *tone* (success / warning / danger / neutral)."""
        self.add_message("system", content, full_prompt=full_prompt, tone=tone)

    def add_thinking_message(self):
        """Show the streaming bubble ("💭 Thinking… 0:00"); the reply's chunks arrive through
        :meth:`append_thinking_text` / :meth:`append_response_text`."""
        self.remove_thinking_message()
        self._streaming = StreamingBubble()
        self._insert(self._streaming)

    def append_thinking_text(self, text: str):
        """Append streaming thinking/reasoning text (SSE reasoning chunks, main thread)."""
        if self._streaming is not None:
            self._streaming.append_thinking(text)
            self._scroll_to_bottom()

    def append_response_text(self, text: str):
        """Append streaming response text (SSE text chunks, main thread), so the user isn't
        staring at silence after thinking completes."""
        if self._streaming is not None:
            self._streaming.append_response(text)
            self._scroll_to_bottom()

    def remove_thinking_message(self):
        """Remove the streaming bubble."""
        if self._streaming is not None:
            self._streaming.stop()
            row = self._streaming.parentWidget()
            self.messages_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
            self._streaming = None

    def set_llm_active(self, active: bool):
        """Enable/disable controls based on LLM request state."""
        self.send_btn.setEnabled(not active)
        self.input_field.setEnabled(not active)
        self.cancel_btn.setEnabled(active)

    def set_validator_status(self, available: bool, reason: str = "") -> None:
        """Update the validator-status indicator AND the auto-correct
        checkbox.

        Visual contract (Phase 4.6 fix):

        * available → checkbox enabled, restored to the operator's
          stored preference (``_stored_auto_correct``).
        * unavailable → checkbox forcibly **unchecked AND disabled** so
          the UI never shows "auto-correct on but greyed" (which read
          as "running but I can't stop it"). The stored preference is
          preserved internally so the next time the validator becomes
          available the checkbox returns to it.

        The toggled signal is blocked while we mutate the checkbox
        state so the persistence write isn't fired for a forced visual
        reset.
        """
        ui_kit.set_state(self.validator_dot, "on", available)
        self.validator_status.setToolTip(
            _TOOLTIP_STATUS_AVAILABLE if available
            else _with_reason(_TOOLTIP_STATUS_UNAVAILABLE, reason))
        self.auto_correct_checkbox.setToolTip(
            _TOOLTIP_AUTO_CORRECT_AVAILABLE if available
            else _with_reason(_TOOLTIP_AUTO_CORRECT_UNAVAILABLE, reason))
        # Force unchecked when unavailable WITHOUT firing the toggled signal — this visual
        # reset must not corrupt the persisted preference (``_stored_auto_correct``).
        self.auto_correct_checkbox.blockSignals(True)
        try:
            self.auto_correct_checkbox.setChecked(self._stored_auto_correct if available else False)
        finally:
            self.auto_correct_checkbox.blockSignals(False)
        self.auto_correct_checkbox.setEnabled(available)
        self.validator_status.setVisible(True)

    def _update_context_label(self):
        """The context meter, from the LATEST turn's total tokens: "Context 18,646 / 400,000"
        and its bar; the share in the tooltip (``context_usage.format_context_usage``, shared
        with the skill chat). Driven by ``_latest_total_tokens``, NOT the running
        ``_cumulative_tokens`` sum, so it stays correct across a compaction."""
        used, limit = self._latest_total_tokens, max(1, int(self._context_limit))
        text, colour = format_context_usage(used, limit)
        self.context_label.setText(f"Context {used:,} / {limit:,}")
        self.context_label.setToolTip(text)
        self.context_bar.setToolTip(text)
        self.context_bar.setValue(min(_METER_STEPS, used * _METER_STEPS // limit))
        ui_kit.set_state(self.context_bar, "full", bool(colour))

    def clear_messages(self):
        """Clear all messages (the streaming bubble included)."""
        self.remove_thinking_message()
        ui_kit.clear_layout(self.messages_layout)
        self.messages_layout.addStretch()
