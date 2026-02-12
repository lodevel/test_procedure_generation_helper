"""
Code Tab - Edit test.py with step marker sidebar.

Implements Section 9.3 of the spec.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox, 
    QPushButton, QLabel, QPlainTextEdit, QListWidget, QListWidgetItem
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QSyntaxHighlighter, QTextCharFormat, QColor

from .base_tab import BaseTab
from ..core import ArtifactType, CodeValidator, StepMarkerParser


class PythonSyntaxHighlighter(QSyntaxHighlighter):
    """Simple Python syntax highlighter."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Keywords
        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(QColor("#0000cc"))
        self.keyword_format.setFontWeight(QFont.Bold)
        
        # Strings
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(QColor("#008800"))
        
        # Comments
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(QColor("#888888"))
        self.comment_format.setFontItalic(True)
        
        # Step markers
        self.step_format = QTextCharFormat()
        self.step_format.setForeground(QColor("#cc6600"))
        self.step_format.setFontWeight(QFont.Bold)
        self.step_format.setBackground(QColor("#fff8e0"))
        
        # Functions
        self.function_format = QTextCharFormat()
        self.function_format.setForeground(QColor("#660066"))
        
        self.keywords = [
            "def", "class", "if", "elif", "else", "for", "while", "try",
            "except", "finally", "with", "as", "import", "from", "return",
            "yield", "raise", "pass", "break", "continue", "and", "or",
            "not", "in", "is", "True", "False", "None", "async", "await"
        ]
    
    def highlightBlock(self, text: str):
        import re
        
        # Comments (before other patterns)
        comment_match = re.search(r'#.*$', text)
        if comment_match:
            # Check if it's a step marker
            step_match = re.match(r'^\s*#\s*Step\s+\d+', text, re.IGNORECASE)
            if step_match:
                self.setFormat(0, len(text), self.step_format)
            else:
                self.setFormat(comment_match.start(), len(text) - comment_match.start(), self.comment_format)
        
        # Keywords
        for keyword in self.keywords:
            pattern = rf'\b{keyword}\b'
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)
        
        # Strings (simple version)
        for match in re.finditer(r'(["\'])(?:(?!\1).)*\1', text):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        
        # Function definitions
        for match in re.finditer(r'\bdef\s+(\w+)', text):
            self.setFormat(match.start(1), match.end(1) - match.start(1), self.function_format)


class CodeTab(BaseTab):
    """
    Code tab for editing test.py.
    
    Features:
    - Python editor with syntax highlighting
    - Step marker sidebar
    - Validation buttons
    - Derivation actions
    """
    
    content_changed = Signal()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Splitter: sidebar | editor
        splitter = QSplitter(Qt.Horizontal)
        
        # Step sidebar
        sidebar_widget = QGroupBox("Step Markers")
        sidebar_layout = QVBoxLayout(sidebar_widget)
        
        self.step_list = QListWidget()
        self.step_list.itemClicked.connect(self._on_step_clicked)
        sidebar_layout.addWidget(self.step_list)
        
        self.step_status = QLabel("No steps detected")
        sidebar_layout.addWidget(self.step_status)
        
        splitter.addWidget(sidebar_widget)
        
        # Editor panel
        editor_widget = QGroupBox("test.py")
        editor_layout = QVBoxLayout(editor_widget)
        
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)  # Enable horizontal scrolling
        self.editor.textChanged.connect(self._on_text_changed)
        
        # Add syntax highlighter
        self.highlighter = PythonSyntaxHighlighter(self.editor.document())
        
        editor_layout.addWidget(self.editor)
        
        # Editor buttons
        btn_layout = QHBoxLayout()
        
        self.save_btn = self.create_button("Save Code", self._on_save)
        self.compile_btn = self.create_button("Local Check", self._on_compile)
        self.review_btn = self.create_button("LLM Review Code", self._on_llm_review)
        self.coherence_btn = self.create_button("Check ↔ JSON", self._on_check_coherence)
        
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.compile_btn)
        btn_layout.addWidget(self.review_btn)
        btn_layout.addWidget(self.coherence_btn)
        btn_layout.addStretch()
        
        editor_layout.addLayout(btn_layout)
        
        # Derivation buttons
        derive_layout = QHBoxLayout()
        
        self.derive_btn = self.create_button("Derive JSON", self._on_derive_json)
        self.force_derive_btn = self.create_button("Force Derive JSON", self._on_force_derive_json)
        
        derive_layout.addWidget(self.derive_btn)
        derive_layout.addWidget(self.force_derive_btn)
        derive_layout.addStretch()
        
        editor_layout.addLayout(derive_layout)
        
        # Status
        self.status_label = QLabel("")
        editor_layout.addWidget(self.status_label)
        
        splitter.addWidget(editor_widget)
        splitter.setSizes([150, 600])
        
        layout.addWidget(splitter)
        
        # Initialize
        self._validator = CodeValidator()
        self._parser = StepMarkerParser()
        self._dirty = False
    
    def _on_text_changed(self):
        """Handle text changes."""
        self._dirty = True
        self._update_step_list()
        self._update_status()
        self.content_changed.emit()
    
    def _update_step_list(self):
        """Update the step marker list."""
        self.step_list.clear()
        
        content = self.editor.toPlainText()
        blocks = self._parser.parse(content)
        
        if not blocks:
            self.step_status.setText("No steps detected")
            return
        
        self.step_status.setText(f"{len(blocks)} steps detected")
        
        for block in blocks:
            item = QListWidgetItem(f"Step {block.step_number}")
            item.setData(Qt.UserRole, block)
            item.setToolTip(f"Lines {block.start_line}-{block.end_line}")
            self.step_list.addItem(item)
    
    def _on_step_clicked(self, item: QListWidgetItem):
        """Jump to a step in the editor."""
        from PySide6.QtGui import QTextCursor
        block = item.data(Qt.UserRole)
        if block:
            # Move cursor to the step line
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            for _ in range(block.start_line - 1):
                cursor.movePosition(QTextCursor.MoveOperation.Down)
            self.editor.setTextCursor(cursor)
            self.editor.centerCursor()
    
    def _update_status(self):
        """Update the status label."""
        if self._dirty:
            self.status_label.setText("● Modified")
            self.status_label.setStyleSheet("color: orange;")
        else:
            self.status_label.setText("✓ Saved")
            self.status_label.setStyleSheet("color: green;")
    
    def _on_save(self):
        """Save the code file."""
        try:
            content = self.editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.TEST_CODE, content)
            self.artifact_manager.save_artifact(ArtifactType.TEST_CODE)
            self._dirty = False
            self._update_status()
            self.status_message.emit("Code saved successfully")
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_compile(self):
        """Run py_compile check."""
        content = self.editor.toPlainText()
        result = self._validator.validate(content)
        
        # Update findings in dock
        self.main_window.dock.show_validation_result(result)
        
        if result.is_valid:
            self.show_info("Compile Check", "Code compiles successfully!")
        else:
            self.show_error("Compile Check", f"Code has syntax errors:\n{result.issues[0].message}")
    
    def _on_llm_review(self):
        """Request LLM review of the code."""
        self.main_window.run_llm_task("review_code")
    
    def _on_check_coherence(self):
        """Check coherence between code and JSON."""
        self.main_window.run_llm_task("review_code_vs_json")
    
    def _on_derive_json(self):
        """Derive JSON from code (strict mode)."""
        self.main_window.run_llm_task("derive_json_from_code", strict=True)
    
    def _on_force_derive_json(self):
        """Force derive JSON from code."""
        self.main_window.run_llm_task("derive_json_from_code", strict=False)
    
    def load_content(self):
        """Load content from artifact manager."""
        content = self.artifact_manager.get_content(ArtifactType.TEST_CODE)
        
        # Block signals to prevent triggering text_changed
        self.editor.blockSignals(True)
        self.editor.setPlainText(content)
        self.editor.blockSignals(False)
        
        self._dirty = self.artifact_manager.is_dirty(ArtifactType.TEST_CODE)
        self._update_step_list()
        self._update_status()
    
    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh the content."""
        self.load_content()
