"""
Raw Response Viewer - Debug view for LLM responses.

Implements Section 10.4 of the spec.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QCheckBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..llm.tab_context import TabContext


class RawResponseViewer(QWidget):
    """
    Raw response viewer for debugging LLM responses.
    
    Features:
    - Show raw LLM response text
    - Toggle auto-show on response
    - Copy to clipboard
    """
    
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._current_tab_context: Optional["TabContext"] = None
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Header
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Raw LLM Response</b>"))
        header.addStretch()
        
        self.auto_show_cb = QCheckBox("Auto-show")
        self.auto_show_cb.setToolTip("Automatically switch to this tab on new responses")
        header.addWidget(self.auto_show_cb)
        
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self._on_copy)
        header.addWidget(self.copy_btn)
        
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        header.addWidget(self.clear_btn)
        
        layout.addLayout(header)
        
        # Text view
        self.text_view = QPlainTextEdit()
        self.text_view.setFont(QFont("Consolas", 9))
        self.text_view.setReadOnly(True)
        self.text_view.setPlaceholderText("LLM responses will appear here...")
        layout.addWidget(self.text_view, stretch=1)
        
        # Stats
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.stats_label)
    
    def switch_context(self, tab_context: Optional["TabContext"]):
        """Switch to a different tab's raw responses."""
        self._current_tab_context = tab_context
        
        # Clear current display
        self._on_clear()
        
        # Load responses from new context
        if tab_context and tab_context.raw_responses:
            for response in tab_context.raw_responses:
                self._append_response(response)
    
    def show_response(self, raw_text: str):
        """Show a raw response (called when LLM returns)."""
        # Append to display
        self._append_response(raw_text)
        
        # Save to TabContext for per-tab persistence
        if self._current_tab_context:
            self._current_tab_context.raw_responses.append(raw_text)
    
    def _append_response(self, raw_text: str):
        """Append raw text to display (internal)."""
        # Append with separator
        if self.text_view.toPlainText():
            self.text_view.appendPlainText("\n" + "=" * 50 + "\n")
        
        self.text_view.appendPlainText(raw_text)
        
        # Scroll to end
        scrollbar = self.text_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        
        # Update stats
        char_count = len(raw_text)
        line_count = raw_text.count('\n') + 1
        self.stats_label.setText(f"Latest: {char_count} chars, {line_count} lines")
    
    def _on_copy(self):
        """Copy content to clipboard."""
        from PySide6.QtWidgets import QApplication
        
        clipboard = QApplication.clipboard()
        clipboard.setText(self.text_view.toPlainText())
        
        self.stats_label.setText("Copied to clipboard!")
    
    def _on_clear(self):
        """Clear the view."""
        self.text_view.clear()
        self.stats_label.setText("")
    
    def should_auto_show(self) -> bool:
        """Check if auto-show is enabled."""
        return self.auto_show_cb.isChecked()
