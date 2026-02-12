"""
Chat Panel - LLM conversation interface.

Implements Section 10.1 of the spec.
"""

import json
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QLineEdit, QPushButton, QLabel, QFrame, QDialog, QPlainTextEdit,
    QCheckBox
)
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QFont, QMouseEvent
from typing import TYPE_CHECKING, List, Tuple, Optional

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..llm import TabContext


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
        self.accept_btn.setStyleSheet("background-color: #c8e6c9;")
        self.accept_btn.clicked.connect(self._on_accept)
        self.reject_btn = QPushButton("✗ Reject")
        self.reject_btn.setStyleSheet("background-color: #ffcdd2;")
        self.reject_btn.clicked.connect(self._on_reject)
        self.diff_btn = QPushButton("View Diff")
        self.diff_btn.clicked.connect(self._on_view_diff)
        
        btn_layout.addWidget(self.accept_btn)
        btn_layout.addWidget(self.reject_btn)
        btn_layout.addWidget(self.diff_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # Style
        self.setStyleSheet("""
            ProposalWidget {
                background-color: #e8f5e9;
                border: 1px solid #c8e6c9;
                border-radius: 5px;
            }
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
        self.setStyleSheet("""
            ProposalWidget {
                background-color: #f5f5f5;
                border: 1px solid #e0e0e0;
                border-radius: 5px;
            }
        """)
        # Update header with status
        header = self.layout().itemAt(0).widget()
        if header:
            header.setText(f"📄 {self._artifact_name}: {status}")


class MessageWidget(QFrame):
    """A single chat message display."""
    
    # Signal emitted when message is double-clicked
    double_clicked = Signal(str)  # msg_id (UUID string)
    
    def __init__(self, role: str, content: str, msg_id: str = "", parent=None):
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
            self.setStyleSheet("""
                MessageWidget {
                    background-color: #e3f2fd;
                    border: 1px solid #bbdefb;
                    border-radius: 5px;
                }
            """)
        elif role.lower() == "assistant":
            self.setStyleSheet("""
                MessageWidget {
                    background-color: #f5f5f5;
                    border: 1px solid #e0e0e0;
                    border-radius: 5px;
                }
            """)
        elif role.lower() == "system":
            self.setStyleSheet("""
                MessageWidget {
                    background-color: #fff3e0;
                    border: 1px solid #ffe0b2;
                    border-radius: 5px;
                }
            """)
    
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Handle double-click to show message details."""
        print(f"[DEBUG] MessageWidget.mouseDoubleClickEvent: msg_id={self._msg_id}")
        if self._msg_id:
            print(f"[DEBUG] Emitting double_clicked signal for msg_id={self._msg_id}")
            self.double_clicked.emit(self._msg_id)
        super().mouseDoubleClickEvent(event)
    
    def eventFilter(self, obj, event):
        """Catch double-clicks on child widgets (especially the content label)."""
        if event.type() == QEvent.Type.MouseButtonDblClick:
            print(f"[DEBUG] eventFilter caught double-click on {obj.__class__.__name__}, msg_id={self._msg_id}")
            if self._msg_id:
                print(f"[DEBUG] eventFilter emitting double_clicked signal for msg_id={self._msg_id}")
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
    
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._messages: List[Tuple[str, str]] = []
        # self._chat_history removed: chat history is now per-tab only
        self._cumulative_tokens = 0  # Track total tokens used in conversation
        self._context_limit = 16384  # Default, will be updated by main_window
        self._current_tab_context: Optional["TabContext"] = None  # Currently displayed tab context
        
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
        print(f"[DEBUG] switch_context called with tab_context={'None' if tab_context is None else tab_context.tab_id}")
        self._current_tab_context = tab_context
        
        # Clear current display
        self.clear_messages()
        
        if tab_context is None:
            self._cumulative_tokens = 0
            self._update_context_label()
            return
        
        # Load messages from tab context
        for msg in tab_context.messages:
            # Add message without storing in history (already stored in TabContext)
            self._add_message_widget(
                msg.role, msg.content, msg.msg_id, msg.full_prompt, msg.full_response,
                msg.prompt_tokens, msg.completion_tokens, msg.total_tokens
            )
        
        # Update token counter
        print(f"[DEBUG] switch_context: Loading cumulative_tokens={tab_context.cumulative_tokens} from TabContext")
        self._cumulative_tokens = tab_context.cumulative_tokens
        self._update_context_label()
    
    def _add_message_widget(self, role: str, content: str, msg_id: str = "", full_prompt: Optional[str] = None, full_response: Optional[str] = None, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0):
        """Add a message widget without modifying TabContext (used when loading history)."""
        # Add token usage to content for assistant messages (but don't update cumulative - caller handles that)
        if role.lower() == "assistant" and total_tokens > 0:
            token_info = f"\n\n<span style='color: gray; font-size: 9px;'>📊 Tokens: {total_tokens} ({prompt_tokens} prompt + {completion_tokens} completion)</span>"
            content = content + token_info
        
        # Create message widget with msg_id from TabContext
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
        
        # Input area
        input_layout = QHBoxLayout()
        
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Ask a question or give instructions...")
        self.input_field.returnPressed.connect(self._on_send)
        input_layout.addWidget(self.input_field, stretch=1)
        
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        input_layout.addWidget(self.send_btn)
        
        self.cancel_btn = QPushButton("⏹️")
        self.cancel_btn.setToolTip("Cancel Current LLM Request")
        self.cancel_btn.setMaximumWidth(35)
        self.cancel_btn.setEnabled(False)  # Disabled until request starts
        self.cancel_btn.clicked.connect(self._on_cancel)
        input_layout.addWidget(self.cancel_btn)
        
        self.reset_btn = QPushButton("🗑️")
        self.reset_btn.setToolTip("Reset LLM Session")
        self.reset_btn.setMaximumWidth(35)
        self.reset_btn.clicked.connect(self._on_reset)
        input_layout.addWidget(self.reset_btn)
        
        layout.addLayout(input_layout)
        
        # Context indicator
        self.context_label = QLabel("")
        self.context_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.context_label)
    
    def _on_send(self):
        """Handle send button click."""
        text = self.input_field.text().strip()
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
    
    def get_force_mode(self) -> bool:
        """Get the current force mode state."""
        return self.force_mode_checkbox.isChecked()
    
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
            token_info = f"\n\n<span style='color: gray; font-size: 9px;'>📊 Tokens: {total_tokens} ({prompt_tokens} prompt + {completion_tokens} completion)</span>"
            content = content + token_info
            
            # CRITICAL FIX: Also update TabContext.cumulative_tokens to keep them synchronized
            # This ensures token count persists across tab switches
            if self._current_tab_context is not None:
                self._current_tab_context.cumulative_tokens = self._cumulative_tokens
                print(f"[DEBUG] Synchronized TabContext.cumulative_tokens = {self._current_tab_context.cumulative_tokens}")
            
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
        print(f"[DEBUG] _on_message_double_clicked called with msg_id={msg_id}")
        if self._current_tab_context is None:
            print("[DEBUG] No TabContext available")
            return
        msg = next((m for m in self._current_tab_context.messages if getattr(m, 'msg_id', None) == msg_id), None)
        print(f"[DEBUG] Retrieved message: {msg is not None}")
        if msg:
            print(f"[DEBUG] Creating MessageDetailDialog")
            dialog = MessageDetailDialog(getattr(msg, 'full_prompt', None), getattr(msg, 'full_response', None), self)
            dialog.exec()
    
    def add_system_message(self, content: str, full_prompt: Optional[str] = None):
        """Add a system message."""
        self.add_message("system", content, full_prompt=full_prompt)
    
    def add_thinking_message(self):
        """Add a temporary 'thinking' message that can be updated."""
        # Create a special message widget for thinking animation
        self._thinking_widget = MessageWidget("assistant", "Thinking...")
        
        # Insert before the stretch
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1,
            self._thinking_widget
        )
        
        # Scroll to bottom
        self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        )
    
    def update_thinking_message(self, dots: str):
        """Update the thinking message with animated dots."""
        if hasattr(self, '_thinking_widget') and self._thinking_widget:
            self._thinking_widget.content_label.setText(f"Thinking{dots}")
    
    def remove_thinking_message(self):
        """Remove the temporary thinking message."""
        if hasattr(self, '_thinking_widget') and self._thinking_widget:
            try:
                # Try to remove widget (may fail if C++ object deleted)
                self.messages_layout.removeWidget(self._thinking_widget)
                self._thinking_widget.deleteLater()
            except RuntimeError:
                # C++ object already deleted during tab switch, just log
                pass
            finally:
                # Always clear reference
                self._thinking_widget = None
    
    def set_llm_active(self, active: bool):
        """Enable/disable controls based on LLM request state."""
        self.send_btn.setEnabled(not active)
        self.input_field.setEnabled(not active)
        self.cancel_btn.setEnabled(active)
    
    def _update_context_label(self):
        """Update context label with cumulative token count."""
        print(f"[DEBUG] _update_context_label called: tokens={self._cumulative_tokens}, limit={self._context_limit}")
        if self._cumulative_tokens > 0:
            context_limit = self._context_limit
            percentage = (self._cumulative_tokens / context_limit) * 100
            
            # Color based on usage
            if percentage >= 95:
                color = "red"
                icon = "🔴"
            elif percentage >= 90:
                color = "orange"
                icon = "🔶"
            elif percentage >= 80:
                color = "#ff9800"
                icon = "⚠️"
            else:
                color = "gray"
                icon = "📊"
            
            label_text = f"{icon} <span style='color: {color};'>Tokens: {self._cumulative_tokens}/{context_limit} ({percentage:.1f}%)</span>"
            self.context_label.setText(label_text)
            print(f"[DEBUG] Context label set to: {label_text}")
        else:
            self.context_label.setText("")
            print(f"[DEBUG] Context label cleared (no tokens)")
    
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
