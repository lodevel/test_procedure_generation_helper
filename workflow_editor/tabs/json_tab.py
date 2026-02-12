"""
JSON Tab - Edit procedure.json with preview.

Implements Section 9.2 of the spec.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGroupBox, 
    QPushButton, QLabel, QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QSyntaxHighlighter, QTextCharFormat, QColor

import json

from .base_tab import BaseTab
from ..core import ArtifactType, JsonValidator


class JsonSyntaxHighlighter(QSyntaxHighlighter):
    """Simple JSON syntax highlighter."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Formats
        self.key_format = QTextCharFormat()
        self.key_format.setForeground(QColor("#0066cc"))
        self.key_format.setFontWeight(QFont.Bold)
        
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(QColor("#008800"))
        
        self.number_format = QTextCharFormat()
        self.number_format.setForeground(QColor("#cc6600"))
        
        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(QColor("#cc00cc"))
    
    def highlightBlock(self, text: str):
        import re
        
        # Highlight keys (before colon)
        for match in re.finditer(r'"([^"]+)"\s*:', text):
            self.setFormat(match.start(), match.end() - match.start() - 1, self.key_format)
        
        # Highlight strings (after colon or in arrays)
        for match in re.finditer(r':\s*"([^"]*)"', text):
            self.setFormat(match.start() + 1, match.end() - match.start() - 1, self.string_format)
        
        # Highlight numbers
        for match in re.finditer(r'\b(\d+\.?\d*)\b', text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)
        
        # Highlight keywords
        for match in re.finditer(r'\b(true|false|null)\b', text):
            self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)


class JsonTab(BaseTab):
    """
    JSON tab for editing procedure.json.
    
    Features:
    - JSON editor with syntax highlighting
    - Preview panel showing steps/equipment
    - Validation buttons
    - Generation actions
    """
    
    content_changed = Signal()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Editor panel
        editor_widget = QGroupBox("procedure.json")
        editor_layout = QVBoxLayout(editor_widget)
        
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)  # Enable horizontal scrolling
        self.editor.textChanged.connect(self._on_text_changed)
        
        # Add syntax highlighter
        self.highlighter = JsonSyntaxHighlighter(self.editor.document())
        
        editor_layout.addWidget(self.editor)
        
        # Editor buttons
        btn_layout = QHBoxLayout()
        
        self.save_btn = self.create_button("Save JSON", self._on_save)
        self.format_btn = self.create_button("Format JSON", self._on_format)
        self.validate_btn = self.create_button("Local Validate", self._on_validate)
        self.review_btn = self.create_button("LLM Review JSON", self._on_llm_review)
        self.coherence_text_btn = self.create_button("Check ↔ Text", self._on_check_text_coherence)
        
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.format_btn)
        btn_layout.addWidget(self.validate_btn)
        btn_layout.addWidget(self.review_btn)
        btn_layout.addWidget(self.coherence_text_btn)
        btn_layout.addStretch()
        
        editor_layout.addLayout(btn_layout)
        
        # Generation buttons
        gen_layout = QHBoxLayout()
        
        self.generate_btn = self.create_button("Generate Code", self._on_generate_code)
        self.force_generate_btn = self.create_button("Force Generate Code", self._on_force_generate_code)
        
        gen_layout.addWidget(self.generate_btn)
        gen_layout.addWidget(self.force_generate_btn)
        gen_layout.addStretch()
        
        editor_layout.addLayout(gen_layout)
        
        # Status
        self.status_label = QLabel("")
        editor_layout.addWidget(self.status_label)
        
        layout.addWidget(editor_widget)
        
        # Initialize
        self._validator = JsonValidator()
        self._dirty = False
    
    def _on_text_changed(self):
        """Handle text changes."""
        self._dirty = True
        self._update_status()
        self.content_changed.emit()
    
    def _update_status(self):
        """Update the status label."""
        if self._dirty:
            self.status_label.setText("● Modified")
            self.status_label.setStyleSheet("color: orange;")
        else:
            self.status_label.setText("✓ Saved")
            self.status_label.setStyleSheet("color: green;")
    
    def _on_save(self):
        """Save the JSON file."""
        try:
            content = self.editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, content)
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_JSON)
            self._dirty = False
            self._update_status()
            self.status_message.emit("JSON saved successfully")
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_format(self):
        """Format JSON with proper indentation."""
        try:
            content = self.editor.toPlainText()
            parsed = json.loads(content)
            formatted = json.dumps(parsed, indent=2)
            self.editor.setPlainText(formatted)
            self.status_message.emit("JSON formatted successfully")
        except json.JSONDecodeError as e:
            self.show_error("Format Failed", f"Invalid JSON: {e}")
        except Exception as e:
            self.show_error("Format Failed", str(e))
    
    def _on_validate(self):
        """Run local validation."""
        content = self.editor.toPlainText()
        result = self._validator.validate(content)
        
        # Update findings in dock
        self.main_window.dock.show_validation_result(result)
        
        if result.is_valid and not result.has_warnings:
            self.show_info("Validation", "JSON is valid!")
        elif result.is_valid:
            self.show_warning("Validation", f"JSON is valid but has {len(result.issues)} warnings.")
        else:
            self.show_error("Validation", f"JSON has {len(result.issues)} issues.")
    
    def _on_llm_review(self):
        """Request LLM review of the JSON."""
        self.main_window.run_llm_task("review_json")
    
    def _on_check_text_coherence(self):
        """Check coherence between JSON and text."""
        self.main_window.run_llm_task("review_text_vs_json")
    
    def _on_generate_code(self):
        """Generate code from JSON (strict mode)."""
        self.main_window.run_llm_task("generate_code_from_json", strict=True)
    
    def _on_force_generate_code(self):
        """Force generate code from JSON."""
        self.main_window.run_llm_task("generate_code_from_json", strict=False)
    
    def load_content(self):
        """Load content from artifact manager."""
        content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON)
        
        # Block signals to prevent triggering text_changed
        self.editor.blockSignals(True)
        self.editor.setPlainText(content)
        self.editor.blockSignals(False)
        
        self._dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_JSON)
        self._update_status()
    
    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh the content."""
        self.load_content()
