"""
Chat Panel - LLM conversation interface.

Implements Section 10.1 of the spec.

Per-message widget classes (``MessageWidget``, ``ProposalWidget``,
``MessageDetailDialog``) live in the sibling ``chat_messages`` module
so this file stays focused on the panel-level coordination
(scrolling, streaming, persistence, validator-status indicator).
"""

import json
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QLineEdit, QPushButton, QLabel, QFrame, QPlainTextEdit,
    QCheckBox
)
from PySide6.QtCore import Qt, Signal, QEvent
from typing import TYPE_CHECKING, List, Tuple, Optional

from .. import theme
from .chat_messages import MessageDetailDialog, MessageWidget, ProposalWidget
from ..llm.context_usage import format_context_usage, latest_message_total


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


def _with_reason(base: str, reason: str) -> str:
    """Append a ``Reason: ...`` line to a tooltip when one was supplied."""
    return f"{base}\nReason: {reason}" if reason else base

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..llm import TabContext


log = logging.getLogger(__name__)



class ChatPanel(QWidget):
    """
    Chat panel for LLM conversation.
    
    Features:
    - Message history display
    - Input field with send button
    - Reset button for clearing session
    - Context-aware prompting
    """
    
    # Signals
    message_sent = Signal(str)  # Emitted when user sends a message
    reset_requested = Signal()  # Emitted when user clicks reset button
    cancel_requested = Signal()  # Emitted when user clicks cancel button
    restart_requested = Signal()  # Emitted when user clicks restart-backend
    
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._messages: List[Tuple[str, str]] = []
        # self._chat_history removed: chat history is now per-tab only
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

        self._setup_ui()
    
    # set_chat_history removed: chat history is now per-tab only
    
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
        
        # Clear current display
        self.clear_messages()
        
        if tab_context is None:
            self._cumulative_tokens = 0
            self._update_context_label()
            return
        
        # Load messages from tab context
        log.info("switch_context: loading %d messages from TabContext '%s'", len(tab_context.messages), tab_context.tab_id)
        for i, msg in enumerate(tab_context.messages):
            thinking = getattr(msg, 'thinking_content', '')
            log.debug("  msg[%d]: role=%s, content_len=%d, thinking_len=%d", i, msg.role, len(msg.content), len(thinking))
            # Add message without storing in history (already stored in TabContext)
            self._add_message_widget(
                msg.role, msg.content, msg.msg_id, msg.full_prompt, msg.full_response,
                msg.prompt_tokens, msg.completion_tokens, msg.total_tokens,
                thinking_content=getattr(msg, 'thinking_content', '')
            )
        
        # Update token counter
        log.debug("switch_context loading cumulative_tokens=%s from TabContext", tab_context.cumulative_tokens)
        self._cumulative_tokens = tab_context.cumulative_tokens
        self._latest_total_tokens = latest_message_total(tab_context.messages)
        self._update_context_label()

        # Refresh the validator UI (status indicator + auto-correct
        # toggle restoration) for the newly active tab.
        self._refresh_validator_ui_for_context(tab_context)
    
    def _add_message_widget(self, role: str, content: str, msg_id: str = "", full_prompt: Optional[str] = None, full_response: Optional[str] = None, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0, thinking_content: str = ""):
        """Add a message widget without modifying TabContext (used when loading history)."""
        # Add token usage to content for assistant messages (but don't update cumulative - caller handles that)
        if role.lower() == "assistant" and total_tokens > 0:
            token_info = f"\n\n<span style='color: {theme.muted_color()}; font-size: 9px;'>📊 Tokens: {total_tokens} ({prompt_tokens} prompt + {completion_tokens} completion)</span>"
            content = content + token_info
        
        # Create message widget with msg_id from TabContext
        msg_widget = MessageWidget(role, content, msg_id=msg_id, thinking_content=thinking_content)
        msg_widget.double_clicked.connect(self._on_message_double_clicked)
        
        # Insert before the stretch
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1,  # Before stretch
            msg_widget
        )
        
        # Scroll to bottom
        self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        )
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Message scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.messages_container = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_container)
        self.messages_layout.setAlignment(Qt.AlignTop)
        self.messages_layout.setSpacing(5)
        
        # Add stretch at bottom to push messages up
        self.messages_layout.addStretch()
        
        self.scroll_area.setWidget(self.messages_container)
        layout.addWidget(self.scroll_area, stretch=1)
        
        # Force Mode checkbox
        self.force_mode_checkbox = QCheckBox("Force (resend all artifacts)")
        self.force_mode_checkbox.setToolTip(
            "Send all artifacts even if not modified (useful for debugging)"
        )
        layout.addWidget(self.force_mode_checkbox)

        # Auto-correct checkbox: drives the validator-in-the-loop FSM.
        # Defaults to ON when validators are available (set by
        # set_validator_status); greyed-out + tooltip explained otherwise.
        # Persisted per-project in <project>/config/config.json under
        # the ``validator_loop`` section — read by switch_context, written
        # by the toggled-handler. No cross-package import needed; the
        # chat panel resolves the project via tab_context.project_manager.
        self.auto_correct_checkbox = QCheckBox("Auto-correct on validator failure")
        self.auto_correct_checkbox.setChecked(True)
        self.auto_correct_checkbox.setToolTip(
            "When the deterministic validator rejects an LLM response, "
            "automatically re-prompt the LLM with the structured errors "
            "(up to N retries) before falling back to operator review. "
            "Greyed out when validators are not available."
        )
        self.auto_correct_checkbox.toggled.connect(self._on_auto_correct_toggled)
        layout.addWidget(self.auto_correct_checkbox)
        
        # Input area - multi-line (Enter to send, Shift+Enter for newline)
        self.input_field = QPlainTextEdit()
        self.input_field.setPlaceholderText("Ask a question or give instructions...")
        self.input_field.setFixedHeight(72)  # ~3 lines
        self.input_field.installEventFilter(self)
        layout.addWidget(self.input_field)
        
        # Buttons row below input
        btn_layout = QHBoxLayout()
        
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        btn_layout.addWidget(self.send_btn)
        
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("⏹️")
        self.cancel_btn.setToolTip("Cancel Current LLM Request")
        self.cancel_btn.setObjectName("iconButton")
        self.cancel_btn.setStyleSheet("QPushButton { padding: 4px 8px; min-width: 0; }")
        self.cancel_btn.setEnabled(False)  # Disabled until request starts
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self.cancel_btn)
        
        self.reset_btn = QPushButton("🗑️")
        self.reset_btn.setToolTip("Reset LLM Session")
        self.reset_btn.setObjectName("iconButton")
        self.reset_btn.setStyleSheet("QPushButton { padding: 4px 8px; min-width: 0; }")
        self.reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(self.reset_btn)
        
        self.restart_btn = QPushButton("🔄")
        self.restart_btn.setToolTip("Restart the backend server (recover a hung / unresponsive backend without relaunching)")
        self.restart_btn.setObjectName("iconButton")
        self.restart_btn.setStyleSheet("QPushButton { padding: 4px 8px; min-width: 0; }")
        self.restart_btn.clicked.connect(self._on_restart)
        btn_layout.addWidget(self.restart_btn)
        
        layout.addLayout(btn_layout)
        
        # Context indicator
        self.context_label = QLabel("")
        self.context_label.setStyleSheet(f"color: {theme.muted_color()}; font-size: 10px;")
        layout.addWidget(self.context_label)

        # Validator-status indicator: green dot = deterministic path active,
        # grey = unavailable. Operators read this to know whether the
        # auto-correct loop / Quick Parse buttons will actually do anything
        # before they invoke them. Updated via set_validator_status() from
        # the tabs on project load / activation. Hidden until first set.
        # The initial tooltip is intentionally absent — set_validator_status
        # owns all three tooltip variants (available / unavailable / hidden).
        self.validator_status_label = QLabel("")
        self.validator_status_label.setStyleSheet("font-size: 10px;")
        self.validator_status_label.setVisible(False)
        layout.addWidget(self.validator_status_label)
    
    def eventFilter(self, obj, event):
        """Handle Enter in input field to send (Shift+Enter inserts newline)."""
        if obj is self.input_field and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Return and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._on_send()
                return True
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
        self._cumulative_tokens = 0
        self._update_context_label()
        self.reset_requested.emit()
    
    def _on_cancel(self):
        """Handle cancel button click."""
        self.cancel_requested.emit()
    
    def _on_restart(self):
        """Handle restart-backend button click."""
        self.restart_requested.emit()
    
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
            self.validator_status_label.setVisible(False)
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
    
    def add_message(self, role: str, content: str, full_prompt: Optional[str] = None, full_response: Optional[str] = None, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0):
        """Add a message to the chat.
        
        Args:
            role: 'user', 'assistant', or 'system'
            content: Display text for the message
            full_prompt: Full prompt sent to LLM (for debugging)
            full_response: Full raw response from LLM (for debugging)
            prompt_tokens: Number of tokens in prompt
            completion_tokens: Number of tokens in completion
            total_tokens: Total tokens for this message
        """
        from ..llm.tab_context import ChatMessage
        
        self._messages.append((role, content))
        
        # Add token usage to content for assistant messages
        if role.lower() == "assistant" and total_tokens > 0:
            self._cumulative_tokens += total_tokens
            self._latest_total_tokens = total_tokens
            token_info = f"\n\n<span style='color: {theme.muted_color()}; font-size: 9px;'>📊 Tokens: {total_tokens} ({prompt_tokens} prompt + {completion_tokens} completion)</span>"
            content = content + token_info
            
            # CRITICAL FIX: Also update TabContext.cumulative_tokens to keep them synchronized
            # This ensures token count persists across tab switches
            if self._current_tab_context is not None:
                self._current_tab_context.cumulative_tokens = self._cumulative_tokens
                log.debug("Synchronized TabContext.cumulative_tokens=%s", self._current_tab_context.cumulative_tokens)
            
            self._update_context_label()
        
        # Create ChatMessage and store in TabContext for persistence
        msg_id = ""
        if self._current_tab_context is not None:
            chat_message = ChatMessage(
                role=role,
                content=content,
                full_prompt=full_prompt,
                full_response=full_response,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens
            )
            self._current_tab_context.messages.append(chat_message)
            msg_id = chat_message.msg_id
        
        # Create message widget with msg_id for double-click
        msg_widget = MessageWidget(role, content, msg_id=msg_id)
        msg_widget.double_clicked.connect(self._on_message_double_clicked)
        
        # Insert before the stretch
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1,  # Before stretch
            msg_widget
        )
        
        # Scroll to bottom
        self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        )
    
    def _on_message_double_clicked(self, msg_id: str):
        """Handle double-click on a message to show details."""
        log.debug("_on_message_double_clicked called with msg_id=%s", msg_id)
        if self._current_tab_context is None:
            log.debug("No TabContext available")
            return
        msg = next((m for m in self._current_tab_context.messages if getattr(m, 'msg_id', None) == msg_id), None)
        log.debug("Retrieved message exists=%s", msg is not None)
        if msg:
            log.debug("Creating MessageDetailDialog")
            dialog = MessageDetailDialog(getattr(msg, 'full_prompt', None), getattr(msg, 'full_response', None), self)
            dialog.exec()
    
    def add_system_message(self, content: str, full_prompt: Optional[str] = None):
        """Add a system message."""
        self.add_message("system", content, full_prompt=full_prompt)
    
    def add_thinking_message(self):
        """Add a temporary 'thinking' message that shows streaming content.
        
        Initially displays "Thinking..." which gets replaced by actual
        reasoning content as SSE events stream in via append_thinking_text().
        """
        # Create a special frame for the thinking display
        self._thinking_widget = QFrame()
        self._thinking_widget.setFrameShape(QFrame.StyledPanel)
        self._thinking_widget.setStyleSheet(f"""
            QFrame {{
                background-color: {theme.message_bg('assistant')};
                border: 1px solid {theme.message_border('assistant')};
                border-radius: 5px;
            }}
        """)
        
        thinking_layout = QVBoxLayout(self._thinking_widget)
        thinking_layout.setContentsMargins(8, 5, 8, 5)
        thinking_layout.setSpacing(3)
        
        # Role header
        role_label = QLabel("ASSISTANT")
        role_label.setStyleSheet("font-weight: bold; font-size: 10px;")
        thinking_layout.addWidget(role_label)
        
        # Thinking header with icon
        self._thinking_header = QLabel("💭 Thinking...")
        self._thinking_header.setStyleSheet(
            f"color: {theme.toggle_color()}; font-style: italic; font-size: 10px; padding: 2px 0;"
        )
        thinking_layout.addWidget(self._thinking_header)
        
        # Streaming thinking content area (hidden until content arrives)
        self._thinking_stream_label = QLabel("")
        self._thinking_stream_label.setWordWrap(True)
        self._thinking_stream_label.setTextFormat(Qt.PlainText)
        self._thinking_stream_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._thinking_stream_label.setStyleSheet(
            f"color: {theme.thinking_fg()}; font-style: italic; font-size: 11px; "
            f"padding: 4px 8px; background-color: {theme.thinking_bg()}; "
            f"border-left: 2px solid {theme.thinking_border()};"
        )
        self._thinking_stream_label.setVisible(False)
        thinking_layout.addWidget(self._thinking_stream_label)
        
        # Track accumulated thinking text
        self._thinking_stream_text = ""
        self._thinking_has_content = False
        
        # --- Response streaming area (hidden until text chunks arrive) ---
        self._response_header = QLabel("✍️ Responding...")
        self._response_header.setStyleSheet(
            f"color: {theme.toggle_hover_color()}; font-style: italic; font-size: 10px; padding: 2px 0;"
        )
        self._response_header.setVisible(False)
        thinking_layout.addWidget(self._response_header)
        
        self._response_stream_label = QLabel("")
        self._response_stream_label.setWordWrap(True)
        self._response_stream_label.setTextFormat(Qt.PlainText)
        self._response_stream_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._response_stream_label.setStyleSheet(
            f"color: {theme.response_fg()}; font-size: 11px; "
            f"padding: 4px 8px; background-color: {theme.response_bg()}; "
            f"border-left: 2px solid {theme.response_border()};"
        )
        self._response_stream_label.setVisible(False)
        thinking_layout.addWidget(self._response_stream_label)
        
        self._response_stream_text = ""
        self._response_has_content = False
        
        # Insert before the stretch
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1,
            self._thinking_widget
        )
        
        # Scroll to bottom
        self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        )
    
    def append_thinking_text(self, text: str):
        """Append streaming thinking/reasoning text to the thinking widget.
        
        Called from the main thread when SSE events deliver reasoning chunks.
        """
        if not hasattr(self, '_thinking_widget') or not self._thinking_widget:
            return
        
        try:
            # On first content, update header and show content area
            if not self._thinking_has_content:
                self._thinking_has_content = True
                self._thinking_header.setText("💭 Thinking...")
                self._thinking_stream_label.setVisible(True)
            
            self._thinking_stream_text += text
            
            # Truncate display if very long (keep last N chars for performance)
            display_text = self._thinking_stream_text
            max_display = 3000
            if len(display_text) > max_display:
                display_text = "..." + display_text[-max_display:]
            
            self._thinking_stream_label.setText(display_text)
            
            # Auto-scroll to bottom to follow the stream
            self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            )
        except RuntimeError:
            # Widget deleted during tab switch
            pass
    
    def update_thinking_message(self, dots: str):
        """Update the thinking message with animated dots."""
        if hasattr(self, '_thinking_widget') and self._thinking_widget:
            try:
                # Only update the dots if we haven't received streaming content yet
                if not getattr(self, '_thinking_has_content', False):
                    self._thinking_header.setText(f"💭 Thinking{dots}")
            except RuntimeError:
                pass
    
    def append_response_text(self, text: str):
        """Append streaming response text to the thinking widget.
        
        Called from the main thread when SSE events deliver response text chunks.
        Shows progressive response content so the user isn't staring at silence
        after thinking completes.
        """
        if not hasattr(self, '_thinking_widget') or not self._thinking_widget:
            return
        
        try:
            # On first response chunk, transition the UI from thinking → responding
            if not self._response_has_content:
                self._response_has_content = True
                # Collapse thinking content (it's done)
                if self._thinking_has_content:
                    self._thinking_header.setText("💭 Thinking (done)")
                    self._thinking_stream_label.setVisible(False)
                else:
                    self._thinking_header.setVisible(False)
                # Show response streaming area
                self._response_header.setVisible(True)
                self._response_stream_label.setVisible(True)
            
            self._response_stream_text += text
            
            # Truncate display if very long (keep last N chars for performance)
            display_text = self._response_stream_text
            max_display = 5000
            if len(display_text) > max_display:
                display_text = "..." + display_text[-max_display:]
            
            self._response_stream_label.setText(display_text)
            
            # Auto-scroll to bottom to follow the stream
            self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            )
        except RuntimeError:
            # Widget deleted during tab switch
            pass
    
    def remove_thinking_message(self):
        """Remove the temporary thinking/streaming message."""
        if hasattr(self, '_thinking_widget') and self._thinking_widget:
            try:
                # Try to remove widget (may fail if C++ object deleted)
                self.messages_layout.removeWidget(self._thinking_widget)
                self._thinking_widget.deleteLater()
            except RuntimeError:
                # C++ object already deleted during tab switch, just log
                pass
            finally:
                # Always clear references
                self._thinking_widget = None
                self._thinking_stream_label = None
                self._thinking_header = None
                self._thinking_stream_text = ""
                self._thinking_has_content = False
                self._response_stream_label = None
                self._response_header = None
                self._response_stream_text = ""
                self._response_has_content = False
    
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
        if available:
            self.validator_status_label.setText("● validator")
            self.validator_status_label.setStyleSheet(
                f"font-size: 10px; color: {theme.success_color()};"
            )
            self.validator_status_label.setToolTip(_TOOLTIP_STATUS_AVAILABLE)
            self.auto_correct_checkbox.setEnabled(True)
            self.auto_correct_checkbox.setToolTip(_TOOLTIP_AUTO_CORRECT_AVAILABLE)
            self.auto_correct_checkbox.blockSignals(True)
            try:
                self.auto_correct_checkbox.setChecked(self._stored_auto_correct)
            finally:
                self.auto_correct_checkbox.blockSignals(False)
        else:
            self.validator_status_label.setText("○ validator")
            self.validator_status_label.setStyleSheet(
                f"font-size: 10px; color: {theme.muted_color()};"
            )
            self.validator_status_label.setToolTip(
                _with_reason(_TOOLTIP_STATUS_UNAVAILABLE, reason)
            )
            self.auto_correct_checkbox.setToolTip(
                _with_reason(_TOOLTIP_AUTO_CORRECT_UNAVAILABLE, reason)
            )
            # Force unchecked WITHOUT firing the toggled signal — we
            # don't want this visual reset to corrupt the persisted
            # preference. The user's stored intent stays in
            # ``_stored_auto_correct``.
            self.auto_correct_checkbox.blockSignals(True)
            try:
                self.auto_correct_checkbox.setChecked(False)
            finally:
                self.auto_correct_checkbox.blockSignals(False)
            self.auto_correct_checkbox.setEnabled(False)
        self.validator_status_label.setVisible(True)
    
    def _update_context_label(self):
        """Update the context readout from the LATEST turn's total tokens.

        Shares ``context_usage.format_context_usage`` with the skill chat so
        the two readouts can't drift. Driven by ``_latest_total_tokens`` (the
        most recent turn's reported total), NOT the running ``_cumulative_
        tokens`` sum, so it stays correct across a compaction instead of
        over-counting."""
        used = self._latest_total_tokens
        log.debug("_update_context_label called: used=%s, limit=%s", used, self._context_limit)
        if used > 0:
            text, colour = format_context_usage(used, self._context_limit)
            # Empty colour from the helper means "use the muted default".
            colour = colour or theme.muted_color()
            self.context_label.setText(
                f"<span style='color: {colour};'>{text}</span>")
            log.debug("Context label set: %s", text)
        else:
            self.context_label.setText("")
            log.debug("Context label cleared (no tokens)")
    
    def clear_messages(self):
        """Clear all messages."""
        self._messages.clear()
        
        # Clear thinking widget reference first (prevents stale reference)
        if hasattr(self, '_thinking_widget'):
            self._thinking_widget = None
        
        # Remove all widgets except stretch
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
    
    def set_context(self, context: str):
        """Set the context indicator."""
        self.context_label.setText(f"Context: {context}")
    
    def set_enabled(self, enabled: bool):
        """Enable/disable the input."""
        self.input_field.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
    
    def get_history(self) -> List[Tuple[str, str]]:
        """Get message history."""
        return self._messages.copy()
    
    def add_proposal(self, artifact_name: str, content: str, artifact_type: str) -> ProposalWidget:
        """Add a proposal widget to the chat.
        
        Args:
            artifact_name: Display name for the proposal (e.g., "JSON" or "Code")
            content: The proposed content
            artifact_type: Type identifier ("json" or "code")
            
        Returns:
            The created ProposalWidget so signals can be connected
        """
        widget = ProposalWidget(artifact_name, content, artifact_type)
        
        # Insert before the stretch
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1,
            widget
        )
        
        # Scroll to bottom
        self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        )
        
        return widget
