"""
Text Tab - Iterative procedure authoring with LLM assistance.

Implements Section 9.5 of the spec.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGroupBox, 
    QPushButton, QLabel, QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from .base_tab import BaseTab
from ..core import ArtifactType


class TextTab(BaseTab):
    """
    Text tab for iterative procedure authoring.
    
    Features:
    - Free-form text editor
    - LLM review and feedback
    - JSON generation
    - Text derivation from JSON
    """
    
    content_changed = Signal()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Editor panel
        editor_group = QGroupBox("Procedure Text (Draft)")
        editor_layout = QVBoxLayout(editor_group)
        
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setPlaceholderText(
            "Write your test procedure here in natural language.\n\n"
            "Example:\n"
            "Test Name: DC Voltage Measurement\n\n"
            "Equipment needed:\n"
            "- DMM (Digital Multimeter)\n"
            "- Power supply\n\n"
            "Steps:\n"
            "1. Connect DMM to test point TP1\n"
            "2. Apply 5V from power supply\n"
            "3. Measure voltage with DMM\n\n"
            "Expected:\n"
            "- Voltage reading: 5.0V ± 0.1V"
        )
        self.editor.textChanged.connect(self._on_text_changed)
        editor_layout.addWidget(self.editor)
        
        # Status
        self.status_label = QLabel("")
        editor_layout.addWidget(self.status_label)
        
        layout.addWidget(editor_group, stretch=1)
        
        # Buttons section
        buttons_group = QGroupBox("Actions")
        buttons_layout = QVBoxLayout(buttons_group)
        
        # Row 1: Save and Review
        row1 = QHBoxLayout()
        self.save_btn = self.create_button("Save Text", self._on_save)
        self.review_btn = self.create_button("Review Text with LLM", self._on_review)
        self.coherence_btn = self.create_button("Check ↔ JSON", self._on_check_coherence)
        
        row1.addWidget(self.save_btn)
        row1.addWidget(self.review_btn)
        row1.addWidget(self.coherence_btn)
        row1.addStretch()
        buttons_layout.addLayout(row1)
        
        # Row 2: Generation
        row2 = QHBoxLayout()
        self.generate_btn = self.create_button("Generate JSON from Text", self._on_generate_json)
        self.force_generate_btn = self.create_button("Force Generate JSON", self._on_force_generate_json)
        self.derive_btn = self.create_button("Derive Text from JSON", self._on_derive_text)
        
        row2.addWidget(self.generate_btn)
        row2.addWidget(self.force_generate_btn)
        row2.addWidget(self.derive_btn)
        row2.addStretch()
        buttons_layout.addLayout(row2)
        
        layout.addWidget(buttons_group)
        
        # Help text
        help_label = QLabel(
            "<i>Tip: Use the chat panel on the right to ask questions or provide context "
            "(e.g., 'Use VISA address tcpip0::192.168.1.1')</i>"
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        
        # Initialize
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
        """Save the text file."""
        try:
            content = self.editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.PROCEDURE_TEXT, content)
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_TEXT)
            self._dirty = False
            self._update_status()
            self.status_message.emit("Text saved successfully")
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_review(self):
        """Request LLM review of the text."""
        self.main_window.run_llm_task("review_text_procedure")
    
    def _on_check_coherence(self):
        """Check coherence between text and JSON."""
        if not self.artifact_manager.procedure_json.exists_on_disk:
            self.show_warning(
                "No JSON",
                "No procedure.json exists to compare with.\n"
                "Generate JSON first, or open a test with existing JSON."
            )
            return
        self.main_window.run_llm_task("review_text_vs_json")
    
    def _on_generate_json(self):
        """Generate JSON from text (strict mode)."""
        self.main_window.run_llm_task("derive_json_from_text", strict=True)
    
    def _on_force_generate_json(self):
        """Force generate JSON from text."""
        self.main_window.run_llm_task("derive_json_from_text", strict=False)
    
    def _on_derive_text(self):
        """Derive text from existing JSON."""
        if not self.artifact_manager.procedure_json.exists_on_disk:
            self.show_warning(
                "No JSON",
                "No procedure.json exists to derive text from.\n"
                "Open a test with existing JSON first."
            )
            return
        self.main_window.run_llm_task("render_text_from_json")
    
    def load_content(self):
        """Load content from artifact manager."""
        content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT)
        
        # Block signals to prevent triggering text_changed
        self.editor.blockSignals(True)
        self.editor.setPlainText(content)
        self.editor.blockSignals(False)
        
        self._dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_TEXT)
        self._update_status()
    
    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh the content."""
        self.load_content()
