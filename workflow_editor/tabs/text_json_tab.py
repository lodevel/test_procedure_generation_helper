"""
Text-JSON Tab - Paired editors for Text↔JSON transformation.

Left: Text editor (procedure_text.md)
Right: JSON editor (procedure.json) + preview

Actions support bidirectional transformation.
"""

import logging
import json

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QLabel, QPlainTextEdit, QWidget
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from .json_tab import JsonSyntaxHighlighter
from ..core import ArtifactType, JsonValidator
from ..llm import TabContext, LLMTask
from ..dialogs import DiffViewer
from ..theme import status_modified, status_saved

log = logging.getLogger(__name__)

class TextJsonTab(LLMTabMixin, BaseTab):
    """
    Text-JSON tab showing both artifacts side-by-side.
    
    LEFT: Text editor (procedure_text.md)
    RIGHT: JSON editor (procedure.json) + preview panel
    
    Actions:
    - Text → JSON: Primary direction (derive structured JSON from natural language)
    - JSON → Text: Reverse direction (render human-readable text from JSON)
    - Coherence checks between both artifacts
    """
    
    content_changed = Signal()
    
    # Tab identifier for button label management
    tab_id = "text_json"
    
    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        
        # Initialize TabContext for this tab
        self.tab_context = TabContext(
            tab_id="text_json",
            backend_factory=main_window.backend_factory,
            project_manager=main_window.project_manager,
            artifact_manager=main_window.artifact_manager,
            session_state=main_window.session_state
        )
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Main splitter: Text | JSON
        splitter = QSplitter(Qt.Horizontal)
        
        # LEFT: Text editor panel
        text_widget = self._create_text_panel()
        splitter.addWidget(text_widget)
        
        # RIGHT: JSON editor + preview panel
        json_widget = self._create_json_panel()
        splitter.addWidget(json_widget)
        
        splitter.setSizes([400, 600])  # Favor JSON side slightly
        layout.addWidget(splitter, stretch=1)
        
        # Actions row
        actions_layout = self._create_actions()
        layout.addLayout(actions_layout)
        
        # Initialize
        self._text_dirty = False
        self._json_dirty = False
        self._validator = JsonValidator()
    
    def _create_text_panel(self):
        """Create text editor panel (left side)."""
        text_group = QGroupBox("Procedure Text (Draft)")
        layout = QVBoxLayout(text_group)
        
        self.text_editor = QPlainTextEdit()
        self.text_editor.setFont(QFont("Consolas", 10))
        self.text_editor.setPlaceholderText(
            "Write your test procedure here in natural language...\n\n"
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
        self.text_editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.text_editor)
        
        self.text_status = QLabel("")
        layout.addWidget(self.text_status)
        
        return text_group
    
    def _create_json_panel(self):
        """Create JSON editor panel (right side)."""
        json_group = QGroupBox("procedure.json")
        json_layout = QVBoxLayout(json_group)
        
        self.json_editor = QPlainTextEdit()
        self.json_editor.setFont(QFont("Consolas", 10))
        self.json_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.json_editor.textChanged.connect(self._on_json_changed)
        
        # Syntax highlighting
        self.json_highlighter = JsonSyntaxHighlighter(self.json_editor.document())
        
        json_layout.addWidget(self.json_editor)
        
        self.json_status = QLabel("")
        json_layout.addWidget(self.json_status)
        
        return json_group
    
    # Task callback maps for dynamic LLM button building (see BaseTab._build_llm_buttons)
    
    def _get_task_callback_map(self) -> dict:
        """Return mapping of task_id -> (callback, tooltip)."""
        return {
            LLMTask.DERIVE_JSON_FROM_TEXT.value: (self._on_derive_json, "Generate structured JSON from procedure text"),
            LLMTask.RENDER_TEXT_FROM_JSON.value: (self._on_render_text, "Generate human-readable text from JSON"),
            LLMTask.REVIEW_TEXT_PROCEDURE.value: (self._on_review_text, "Review procedure text for quality and completeness"),
            LLMTask.REVIEW_JSON.value: (self._on_review_json, "Review JSON structure and content"),
            LLMTask.REVIEW_TEXT_VS_JSON.value: (self._on_check_coherence, "Check coherence between text and JSON artifacts"),
        }
    
    def _get_force_callback_map(self) -> dict:
        """Return mapping of task_id -> (force_callback, tooltip) for force-mode buttons."""
        return {
            LLMTask.DERIVE_JSON_FROM_TEXT.value: (self._on_force_derive_json, "Force generate JSON (bypass validation checks)"),
        }
    
    def _create_actions(self):
        """Create action buttons organized into functional groups."""
        layout = QHBoxLayout()
        
        # File Operations Group (light blue)
        file_group = self.create_action_group("File Operations", "file")
        file_layout = QVBoxLayout(file_group)
        
        # Save buttons
        save_row = QHBoxLayout()
        self.save_text_btn = self.create_button("Save Text", self._on_save_text,
            tooltip="Save procedure text to disk")
        self.save_json_btn = self.create_button("Save JSON", self._on_save_json,
            tooltip="Save procedure JSON to disk")
        self.save_both_btn = self.create_button("Save Both", self._on_save_both,
            tooltip="Save both text and JSON artifacts")
        save_row.addWidget(self.save_text_btn)
        save_row.addWidget(self.save_json_btn)
        save_row.addWidget(self.save_both_btn)
        save_row.addStretch()
        file_layout.addLayout(save_row)
        
        # Format/Validate buttons
        format_row = QHBoxLayout()
        self.format_json_btn = self.create_button("Format JSON", self._on_format_json,
            tooltip="Auto-format JSON with proper indentation")
        self.validate_json_btn = self.create_button("Validate JSON", self._on_validate_json,
            tooltip="Run local JSON schema validation")
        format_row.addWidget(self.format_json_btn)
        format_row.addWidget(self.validate_json_btn)
        format_row.addStretch()
        file_layout.addLayout(format_row)

        # Quick Parse row — hidden until project provides config/text_parser.py
        parse_row = QHBoxLayout()
        self.quick_parse_btn = self.create_button(
            "⚡ Quick Parse", self._on_quick_parse,
            tooltip="Parse structured text to JSON without LLM (rule-based, instant)"
        )
        self.quick_parse_btn.setVisible(False)
        parse_row.addWidget(self.quick_parse_btn)
        parse_row.addStretch()
        file_layout.addLayout(parse_row)
        
        layout.addWidget(file_group)
        
        # LLM Actions Group — built dynamically from BaseTab
        layout.addWidget(self._create_llm_action_group())
        
        return layout
    
    # Event handlers
    def _on_text_changed(self):
        """Handle text editor changes."""
        self._text_dirty = True
        self._update_text_status()
        self.tab_context.mark_artifact_modified("procedure_text")
        self.content_changed.emit()
    
    def _on_json_changed(self):
        """Handle JSON editor changes."""
        self._json_dirty = True
        self._update_json_status()
        self.tab_context.mark_artifact_modified("procedure_json")
        self.content_changed.emit()
    
    def _on_save_text(self):
        """Save text artifact."""
        try:
            content = self.text_editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.PROCEDURE_TEXT, content)
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_TEXT)
            self.artifact_manager.procedure_text.mark_clean()
            self._text_dirty = False
            self._update_text_status()
            self.status_message.emit("Text saved successfully")
            self.artifact_saved.emit()
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_save_json(self):
        """Save JSON artifact."""
        try:
            content = self.json_editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, content)
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_JSON)
            self.artifact_manager.procedure_json.mark_clean()
            self._json_dirty = False
            self._update_json_status()
            self.status_message.emit("JSON saved successfully")
            self.artifact_saved.emit()
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_save_both(self):
        """Save both artifacts."""
        self._on_save_text()
        self._on_save_json()
    
    def sync_editors_to_artifacts(self):
        """Sync editor content to ArtifactManager without saving to disk."""
        if not self.artifact_manager:
            return
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText()
        )
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText()
        )
    
    def save_all_artifacts(self):
        """Save both text and JSON artifacts (sync + save + reset dirty)."""
        self._on_save_both()
    
    def has_unsaved_changes(self) -> bool:
        """Check if either editor has been modified since last save."""
        return self._text_dirty or self._json_dirty
    
    def _on_format_json(self):
        """Format JSON with proper indentation."""
        try:
            content = self.json_editor.toPlainText()
            parsed = json.loads(content)
            formatted = json.dumps(parsed, indent=2)
            self.json_editor.setPlainText(formatted)
            self.status_message.emit("JSON formatted successfully")
        except json.JSONDecodeError as e:
            self.show_error("Format Failed", f"Invalid JSON: {e}")
        except Exception as e:
            self.show_error("Format Failed", str(e))
    
    def _on_validate_json(self):
        """Run local JSON validation."""
        content = self.json_editor.toPlainText()
        result = self._validator.validate(content)
        
        # Update findings in dock
        self.main_window.dock.show_validation_result(result)
        
        if result.is_valid and not result.has_warnings:
            self.show_info("Validation", "JSON is valid!")
        elif result.is_valid:
            self.show_warning("Validation", f"JSON is valid but has {len(result.issues)} warnings.")
        else:
            self.show_error("Validation", f"JSON has {len(result.issues)} issues.")
    
    def refresh_parser_button(self):
        """Show or hide the Quick Parse button based on project's config/text_parser.py."""
        has_parser = self.project_manager.get_text_parser() is not None
        self.quick_parse_btn.setVisible(has_parser)

    def _on_quick_parse(self):
        """Deterministic Text → JSON without LLM (rule-based, instant)."""
        parser = self.project_manager.get_text_parser()
        if parser is None:
            self.show_warning(
                "No Parser",
                "No text_parser.py found in config/.\nAdd one to enable Quick Parse."
            )
            return
        text = self.text_editor.toPlainText().strip()
        if not text:
            self.show_warning("No Content", "Procedure text is empty. Add text before parsing.")
            return

        # The parser module is user-supplied (per-project plugin); any
        # exception it raises is a parser bug, not a workflow-editor bug.
        # Surface it cleanly instead of dying silently.
        try:
            parse_result = parser.parse(text)
        except Exception as e:
            log.exception("Quick Parse: parser raised")
            self.show_error(
                "Parser Error",
                f"The parser raised an exception while processing the text:\n\n{e}\n\n"
                "See the log for the full traceback."
            )
            return

        # Contract: parse() -> tuple[dict, list[str]].
        if (not isinstance(parse_result, tuple)) or len(parse_result) != 2:
            self.show_error(
                "Parser Error",
                "Parser returned an unexpected value. Expected a "
                "(procedure_dict, warnings_list) tuple."
            )
            return
        result, warnings = parse_result
        if not isinstance(result, dict):
            self.show_error(
                "Parser Error",
                f"Parser returned a non-dict procedure "
                f"({type(result).__name__}); cannot apply."
            )
            return
        if not isinstance(warnings, list):
            log.warning(
                "Parser returned non-list warnings (%s); coercing to [].",
                type(warnings).__name__,
            )
            warnings = []

        result_str = json.dumps(result, indent=2)

        current_json = self.json_editor.toPlainText().strip()
        if current_json:
            # Non-empty JSON — show diff so user can review
            accepted, final_content = DiffViewer.show_diff(
                current_json,
                result_str,
                "Review Changes: procedure.json (Quick Parse)",
                self,
            )
            if not accepted:
                self.main_window.dock.chat_panel.add_system_message(
                    "✗ Quick Parse — changes rejected."
                )
                return
            result_str = final_content
        else:
            # Empty JSON — apply directly
            pass

        self.json_editor.setPlainText(result_str)
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, result_str)
        self._json_dirty = True
        self._update_json_status()

        if warnings:
            warn_lines = "\n".join(f"  • {w}" for w in warnings)
            self.main_window.dock.chat_panel.add_system_message(
                f"⚡ Quick Parse complete — {len(warnings)} warning(s):\n{warn_lines}"
            )
        else:
            self.main_window.dock.chat_panel.add_system_message(
                "⚡ Quick Parse complete — no warnings."
            )
        self.status_message.emit("⚡ Quick Parse complete")

    def _on_derive_json(self):
        """Text → JSON transformation (strict mode)."""
        if not self.artifact_manager.procedure_text.content:
            self.show_warning("No Content", "Procedure text is empty. Add text before generating JSON.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_TEXT, strict_mode=True)
    
    def _on_force_derive_json(self):
        """Text → JSON transformation (force mode)."""
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_TEXT, strict_mode=False)
    
    def _on_render_text(self):
        """JSON → Text transformation."""
        if not self.artifact_manager.procedure_json.content:
            self.show_warning(
                "No JSON",
                "JSON editor is empty. Write JSON first or load a test with existing JSON."
            )
            return
        self._run_task_async(LLMTask.RENDER_TEXT_FROM_JSON)
    
    def _on_review_text(self):
        """Review text with LLM."""
        if not self.artifact_manager.procedure_text.content:
            self.show_warning("No Text", "Text editor is empty. Write text first.")
            return
        self._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)
    
    def _on_review_json(self):
        """Review JSON with LLM."""
        if not self.artifact_manager.procedure_json.content:
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.REVIEW_JSON)
    
    def _on_check_coherence(self):
        """Check Text↔JSON coherence."""
        if not self.artifact_manager.procedure_text.content:
            self.show_warning("No Text", "Text editor is empty.")
            return
        if not self.artifact_manager.procedure_json.content:
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_TEXT_VS_JSON)
    
    def _get_task_description(self, task: LLMTask, user_message: str = None, custom_task_id: str = None) -> str:
        """Generate user-facing task description.
        
        Args:
            task: The LLM task being executed
            user_message: Optional user-provided message (for AD_HOC_CHAT)
            custom_task_id: Optional custom task ID for looking up task name
            
        Returns:
            Human-readable description of the task
        """
        # For custom tasks, look up the task name from config
        if custom_task_id:
            manager = self.task_config_manager
            if manager:
                task_config = manager.get_task_config(self.tab_id, custom_task_id)
                if task_config:
                    return f"Run: {task_config.name}"
        
        # For ad-hoc chat, use the user's actual message
        if task == LLMTask.AD_HOC_CHAT and user_message:
            return user_message
        
        task_descriptions = {
            LLMTask.DERIVE_JSON_FROM_TEXT: "Generate structured JSON from procedure text",
            LLMTask.RENDER_TEXT_FROM_JSON: "Generate human-readable text from JSON",
            LLMTask.REVIEW_TEXT_PROCEDURE: "Review procedure text for quality",
            LLMTask.REVIEW_JSON: "Review JSON structure and content",
            LLMTask.REVIEW_TEXT_VS_JSON: "Check coherence between text and JSON",
            LLMTask.AD_HOC_CHAT: "General assistance"
        }
        return task_descriptions.get(task, f"Run {task.name}")

    def _sync_editors_for_llm(self):
        """Sync editor widgets to artifact manager before LLM task."""
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText())
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText())

    def _apply_proposals(self, response):
        """Dispatch per-artifact proposals from LLM response."""
        if response.procedure_text and response.procedure_text.mode:
            self._handle_text_proposal(response.procedure_text)
        if response.procedure_json and response.procedure_json.mode:
            self._handle_json_proposal(response.procedure_json)

    def _get_expected_artifact_fields(self) -> list[str]:
        """Get expected artifact fields for this tab."""
        return ["text_procedure", "procedure_json"]

    def _parse_response_to_dict(self, response) -> dict:
        """Parse LLMResponse into the dict format expected by _validate_output_contract."""
        parsed = {
            "assistant_message": response.assistant_message,
            "open_questions": response.session_delta.get("open_questions", []),
        }
        parsed["propose_update"] = False
        if response.procedure_text and response.procedure_text.mode:
            parsed["propose_update"] = True
            parsed["text_procedure"] = response.procedure_text.content
        if response.procedure_json and response.procedure_json.mode:
            parsed["propose_update"] = True
            parsed["procedure_json"] = response.procedure_json.content
        return parsed

    def _handle_text_proposal(self, proposal):
        """Handle procedure_text proposal."""
        if proposal.mode == "replace":
            # Serialize dict to string if needed
            if isinstance(proposal.content, dict):
                content_str = json.dumps(proposal.content, indent=2)
            else:
                content_str = str(proposal.content)
            
            # Show diff dialog for user to accept/reject
            current_content = self.text_editor.toPlainText()
            accepted, final_content = DiffViewer.show_diff(
                current_content,
                content_str,
                "Review Changes: procedure_text.md",
                self
            )
            
            if accepted:
                self.text_editor.setPlainText(final_content)
                self.artifact_manager.procedure_text.content = final_content
                self._text_dirty = True
                self._update_text_status()
                self.main_window.dock.chat_panel.add_system_message("✓ Applied changes to procedure_text.md")
            else:
                self.main_window.dock.chat_panel.add_system_message("✗ Rejected changes to procedure_text.md")
    
    def _handle_json_proposal(self, proposal):
        """Handle procedure_json proposal."""
        if proposal.mode == "replace":
            # Serialize dict to JSON string if needed
            if isinstance(proposal.content, dict):
                content_str = json.dumps(proposal.content, indent=2)
            else:
                content_str = str(proposal.content)
            
            # Show diff dialog for user to accept/reject
            current_content = self.json_editor.toPlainText()
            accepted, final_content = DiffViewer.show_diff(
                current_content,
                content_str,
                "Review Changes: procedure.json",
                self
            )
            
            if accepted:
                self.json_editor.setPlainText(final_content)
                self.artifact_manager.procedure_json.content = final_content
                self._json_dirty = True
                self._update_json_status()
                self.main_window.dock.chat_panel.add_system_message("✓ Applied changes to procedure.json")
            else:
                self.main_window.dock.chat_panel.add_system_message("✗ Rejected changes to procedure.json")
    
    def _update_text_status(self):
        """Update text status label."""
        if self._text_dirty:
            self.text_status.setText("● Modified")
            self.text_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.text_status.setText("✓ Saved")
            self.text_status.setStyleSheet(f"color: {status_saved()};")
    
    def _update_json_status(self):
        """Update JSON status label."""
        if self._json_dirty:
            self.json_status.setText("● Modified")
            self.json_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.json_status.setText("✓ Saved")
            self.json_status.setStyleSheet(f"color: {status_saved()};")
    
    def load_content(self):
        """Load both artifacts into editors."""
        if not self.artifact_manager:
            return
        
        # Load text
        text_content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT)
        self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(text_content)
        self.text_editor.blockSignals(False)
        self._text_dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_TEXT)
        self._update_text_status()
        
        # Load JSON
        json_content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON)
        self.json_editor.blockSignals(True)
        self.json_editor.setPlainText(json_content)
        self.json_editor.blockSignals(False)
        self._json_dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_JSON)
        self._update_json_status()
    
    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh both artifacts."""
        self.load_content()
    
