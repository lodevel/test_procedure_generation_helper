"""
JSON-Code Tab - Paired editors for JSON↔Code transformation.

Left: JSON editor (procedure.json) + preview
Right: Code editor (test.py) + step markers

Actions support bidirectional transformation.
"""

import logging
import json
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QLabel, QPlainTextEdit,
    QListWidget, QListWidgetItem, QWidget
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from .json_tab import JsonSyntaxHighlighter
from .code_tab import PythonSyntaxHighlighter
from ..core import ArtifactType, StepMarkerParser
from ..llm import TabContext, LLMTask
from ..dialogs import DiffViewer
from ..theme import status_modified, status_saved

log = logging.getLogger(__name__)

class JsonCodeTab(LLMTabMixin, BaseTab):
    """
    JSON-Code tab showing both artifacts side-by-side.
    
    LEFT: JSON editor (procedure.json) + preview
    RIGHT: Code editor (test.py) + step markers
    
    Actions:
    - JSON → Code: Primary direction (generate executable test from procedure)
    - Code → JSON: Reverse direction (reverse-engineer JSON from existing code)
    - Coherence checks between artifacts
    """
    
    content_changed = Signal()
    
    # Tab identifier for button label management
    tab_id = "json_code"
    
    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        
        # Initialize TabContext for this tab
        self.tab_context = TabContext(
            tab_id="json_code",
            backend_factory=main_window.backend_factory,
            project_manager=main_window.project_manager,
            artifact_manager=main_window.artifact_manager,
            task_config_manager=main_window.task_config_manager,
            session_state=main_window.session_state
        )
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Main splitter: JSON | Code
        splitter = QSplitter(Qt.Horizontal)
        
        # LEFT: JSON editor + preview
        json_widget = self._create_json_panel()
        splitter.addWidget(json_widget)
        
        # RIGHT: Code editor + step markers
        code_widget = self._create_code_panel()
        splitter.addWidget(code_widget)
        
        splitter.setSizes([400, 600])
        layout.addWidget(splitter, stretch=1)
        
        # Actions row
        actions_layout = self._create_actions()
        layout.addLayout(actions_layout)
        
        # Initialize
        self._json_dirty = False
        self._code_dirty = False
        self._parser = StepMarkerParser()
    
    def _create_json_panel(self):
        """Create JSON editor (left side)."""
        json_group = QGroupBox("procedure.json")
        json_layout = QVBoxLayout(json_group)
        
        self.json_editor = QPlainTextEdit()
        self.json_editor.setFont(QFont("Consolas", 10))
        self.json_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.json_editor.textChanged.connect(self._on_json_changed)
        
        self.json_highlighter = JsonSyntaxHighlighter(self.json_editor.document())
        
        json_layout.addWidget(self.json_editor)
        
        self.json_status = QLabel("")
        json_layout.addWidget(self.json_status)
        
        return json_group
    
    def _create_code_panel(self):
        """Create code editor + step markers (right side)."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Step markers sidebar
        sidebar_group = QGroupBox("Steps")
        sidebar_layout = QVBoxLayout(sidebar_group)
        
        self.step_list = QListWidget()
        self.step_list.itemClicked.connect(self._on_step_clicked)
        sidebar_layout.addWidget(self.step_list)
        
        self.step_status = QLabel("No steps")
        sidebar_layout.addWidget(self.step_status)
        
        sidebar_group.setMaximumWidth(150)
        layout.addWidget(sidebar_group)
        
        # Code editor
        code_group = QGroupBox("test.py")
        code_layout = QVBoxLayout(code_group)
        
        self.code_editor = QPlainTextEdit()
        self.code_editor.setFont(QFont("Consolas", 10))
        self.code_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.code_editor.textChanged.connect(self._on_code_changed)
        
        self.code_highlighter = PythonSyntaxHighlighter(self.code_editor.document())
        
        code_layout.addWidget(self.code_editor)
        
        self.code_status = QLabel("")
        code_layout.addWidget(self.code_status)
        
        layout.addWidget(code_group)
        
        return container
    
    # Task callback maps for dynamic LLM button building (see BaseTab._build_llm_buttons)
    
    def _get_task_callback_map(self) -> dict:
        """Return mapping of task_id -> (callback, tooltip)."""
        return {
            LLMTask.GENERATE_CODE_FROM_JSON.value: (self._on_generate_code, "Generate executable test code from JSON procedure"),
            LLMTask.DERIVE_JSON_FROM_CODE.value: (self._on_derive_json, "Extract JSON structure from existing test code"),
            LLMTask.REVIEW_JSON.value: (self._on_review_json, "Review JSON structure and content"),
            LLMTask.REVIEW_CODE.value: (self._on_review_code, "Review test code quality and structure"),
            LLMTask.REVIEW_CODE_VS_JSON.value: (self._on_check_coherence, "Check coherence between JSON and code artifacts"),
        }
    
    def _get_force_callback_map(self) -> dict:
        """Return mapping of task_id -> (force_callback, tooltip) for force-mode buttons."""
        return {
            LLMTask.GENERATE_CODE_FROM_JSON.value: (self._on_force_generate_code, "Force generate code (bypass validation checks)"),
            LLMTask.DERIVE_JSON_FROM_CODE.value: (self._on_force_derive_json, "Force derive JSON (bypass validation checks)"),
        }
    
    def _create_actions(self):
        """Create action buttons organized into visual groups."""
        layout = QHBoxLayout()
        
        # File Operations Group (light blue)
        file_group = self.create_action_group("File Operations", "file")
        file_layout = QVBoxLayout(file_group)
        
        # Save buttons
        save_row = QHBoxLayout()
        self.save_json_btn = self.create_button("Save JSON", self._on_save_json,
            tooltip="Save procedure JSON to disk")
        self.save_code_btn = self.create_button("Save Code", self._on_save_code,
            tooltip="Save test code to disk")
        self.save_both_btn = self.create_button("Save Both", self._on_save_both,
            tooltip="Save both JSON and code artifacts")
        save_row.addWidget(self.save_json_btn)
        save_row.addWidget(self.save_code_btn)
        save_row.addWidget(self.save_both_btn)
        save_row.addStretch()
        file_layout.addLayout(save_row)
        
        # Format + validator buttons. Format is hard-coded (tab-local
        # behavior, no registry involvement); validator buttons come
        # from the registry via TaskConfigManager (Phase 2/3).
        format_row = QHBoxLayout()
        self.format_json_btn = self.create_button("Format JSON", self._on_format_json,
            tooltip="Auto-format JSON with proper indentation")
        format_row.addWidget(self.format_json_btn)
        self._build_validator_buttons(format_row)
        format_row.addStretch()
        file_layout.addLayout(format_row)

        # Quick Code row — hidden until project provides a code_parser variant
        code_row = QHBoxLayout()
        self.quick_code_btn = self.create_button(
            "⚡ Quick Code", self._on_quick_code,
            tooltip="Generate test code from JSON without LLM (rule-based, instant)"
        )
        self.quick_code_btn.setVisible(False)
        code_row.addWidget(self.quick_code_btn)
        code_row.addStretch()
        file_layout.addLayout(code_row)
        
        layout.addWidget(file_group)
        
        # LLM Actions Group — built dynamically from BaseTab
        layout.addWidget(self._create_llm_action_group())
        
        return layout
    
    # Event handlers
    def _on_json_changed(self):
        """Handle JSON editor changes."""
        # Mirror live editor into artifact_manager — see text_only_tab
        # for the rationale.
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText()
        )
        self._json_dirty = True
        self._update_json_status()
        self.tab_context.mark_artifact_modified("procedure_json")
        self.content_changed.emit()

    def _on_code_changed(self):
        """Handle code editor changes."""
        self.artifact_manager.set_content(
            ArtifactType.TEST_CODE, self.code_editor.toPlainText()
        )
        self._code_dirty = True
        self._update_code_status()
        self._update_step_markers()
        self.tab_context.mark_artifact_modified("test_code")
        self.content_changed.emit()
    
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
    
    def _on_save_code(self):
        """Save code artifact."""
        try:
            content = self.code_editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.TEST_CODE, content)
            self.artifact_manager.save_artifact(ArtifactType.TEST_CODE)
            self.artifact_manager.test_code.mark_clean()
            self._code_dirty = False
            self._update_code_status()
            self.status_message.emit("Code saved successfully")
            self.artifact_saved.emit()
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_save_both(self):
        """Save both artifacts."""
        self._on_save_json()
        self._on_save_code()
    
    def sync_editors_to_artifacts(self):
        """Sync editor content to ArtifactManager without saving to disk."""
        if not self.artifact_manager:
            return
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText()
        )
        self.artifact_manager.set_content(
            ArtifactType.TEST_CODE, self.code_editor.toPlainText()
        )
    
    def save_all_artifacts(self):
        """Save both JSON and code artifacts (sync + save + reset dirty)."""
        self._on_save_both()
    
    def has_unsaved_changes(self) -> bool:
        """Check if either editor has been modified since last save."""
        return self._json_dirty or self._code_dirty
    
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
    
    def _get_artifact_for_validation(self, name: str) -> str | None:
        """Override the base implementation to read live editor content."""
        if name == "procedure_json":
            return self.json_editor.toPlainText() or None
        if name == "test_code":
            return self.code_editor.toPlainText() or None
        return super()._get_artifact_for_validation(name)


    def _on_generate_code(self):
        """JSON → Code transformation (strict mode)."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True)

    def _on_force_generate_code(self):
        """JSON → Code transformation (force mode)."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False)

    def _on_derive_json(self):
        """Code → JSON transformation (strict mode)."""
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty. Write code first.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_CODE, strict_mode=True)

    def _on_force_derive_json(self):
        """Code → JSON transformation (force mode)."""
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty. Write code first.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_CODE, strict_mode=False)

    def _on_review_json(self):
        """Review JSON with LLM."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_JSON)

    def _on_review_code(self):
        """Review code with LLM."""
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_CODE)

    def _on_check_coherence(self):
        """Check JSON↔Code coherence."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_CODE_VS_JSON)
    
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
            LLMTask.GENERATE_CODE_FROM_JSON: "Generate executable test code from JSON",
            LLMTask.DERIVE_JSON_FROM_CODE: "Extract JSON structure from existing code",
            LLMTask.REVIEW_JSON: "Review JSON structure and content",
            LLMTask.REVIEW_CODE: "Review test code quality and structure",
            LLMTask.REVIEW_CODE_VS_JSON: "Check coherence between JSON and code",
            LLMTask.AD_HOC_CHAT: "General assistance"
        }
        return task_descriptions.get(task, f"Run {task.name}")

    def _sync_editors_for_llm(self):
        """Sync editor widgets to artifact manager before LLM task."""
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText())
        self.artifact_manager.set_content(ArtifactType.TEST_CODE, self.code_editor.toPlainText())

    def _apply_proposals(self, response):
        """Dispatch per-artifact proposals from LLM response."""
        if response.procedure_json and response.procedure_json.mode:
            self._handle_json_proposal(response.procedure_json)
        if response.test_code and response.test_code.mode:
            self._handle_code_proposal(response.test_code)

    def _get_expected_artifact_fields(self) -> list[str]:
        """Get expected artifact fields for this tab."""
        return ["procedure_json", "test_code"]

    def _parse_response_to_dict(self, response) -> dict:
        """Parse LLMResponse into the dict format expected by _validate_output_contract."""
        parsed = {
            "assistant_message": response.assistant_message,
            "open_questions": response.session_delta.get("open_questions", []),
        }
        parsed["propose_update"] = False
        if response.procedure_json and response.procedure_json.mode:
            parsed["propose_update"] = True
            parsed["procedure_json"] = response.procedure_json.content
        if response.test_code and response.test_code.mode:
            parsed["propose_update"] = True
            parsed["test_code"] = response.test_code.content
        return parsed

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
    
    def _handle_code_proposal(self, proposal):
        """Handle test_code proposal."""
        if proposal.mode == "replace":
            # Serialize dict to string if needed
            if isinstance(proposal.content, dict):
                content_str = json.dumps(proposal.content, indent=2)
            else:
                content_str = str(proposal.content)

            current_content = self.code_editor.toPlainText()

            # Preserve operator-pinned bench-identification constants
            # in the proposed code. The LLM doesn't know the bench's
            # real VISA/COM addresses; if it produces defaults, we
            # substitute the operator's existing values before showing
            # the diff so the operator only sees real-content changes.
            if current_content.strip():
                try:
                    json_str = self.artifact_manager.procedure_json.content or ""
                    procedure = json.loads(json_str) if json_str.strip() else {}
                    equipment_ids = [
                        eq.get("id") for eq in (procedure.get("equipment") or [])
                        if isinstance(eq, dict) and eq.get("id")
                    ]
                except (json.JSONDecodeError, AttributeError):
                    equipment_ids = []
                if equipment_ids:
                    from ..llm.code_constants_merge import preserve_bench_constants
                    content_str, replaced = preserve_bench_constants(
                        content_str, current_content, equipment_ids,
                    )
                    if replaced:
                        log.info(
                            "test.py proposal: preserved %d operator-pinned "
                            "constant(s): %s",
                            len(replaced), ", ".join(replaced),
                        )

            # Show diff dialog for user to accept/reject
            accepted, final_content = DiffViewer.show_diff(
                current_content,
                content_str,
                "Review Changes: test.py",
                self
            )

            if accepted:
                self.code_editor.setPlainText(final_content)
                self.artifact_manager.test_code.content = final_content
                self._code_dirty = True
                self._update_code_status()
                self._update_step_markers()
                self.main_window.dock.chat_panel.add_system_message("✓ Applied changes to test.py")
            else:
                self.main_window.dock.chat_panel.add_system_message("✗ Rejected changes to test.py")

    def refresh_code_parser_button(self):
        """Show or hide the Quick Code button based on project's code_parser variant."""
        has_parser = self.project_manager.get_code_parser() is not None
        self.quick_code_btn.setVisible(has_parser)

    def _on_quick_code(self):
        """Deterministic JSON → Code without LLM (rule-based, instant)."""
        parser = self.project_manager.get_code_parser()
        if parser is None:
            self.show_warning(
                "No Parser",
                "No code_parser variant selected.\n"
                "Configure one via Project Config → Parsers to enable Quick Code."
            )
            return
        json_text = self.json_editor.toPlainText().strip()
        if not json_text:
            self.show_warning("No Content", "JSON editor is empty. Add JSON before generating code.")
            return
        try:
            procedure = json.loads(json_text)
        except json.JSONDecodeError as e:
            self.show_error("Invalid JSON", f"Cannot parse procedure JSON:\n\n{e}")
            return

        # The parser is a user-supplied plugin; structured ParseError /
        # codegen failures route through the shared validator-error
        # dialog so the operator sees the same rendering across surfaces.
        try:
            parse_result = parser.parse(procedure)
        except Exception as e:
            log.exception("Quick Code: parser raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e,
                title="Quick Code — validator findings",
                intro=(
                    "The deterministic JSON→Code generator rejected the input. "
                    "Fix the issues below or fall back to the LLM workflow."
                ),
                parent=self,
            )
            return

        # Contract: parse() -> tuple[str, list[str]].
        if (not isinstance(parse_result, tuple)) or len(parse_result) != 2:
            self.show_error(
                "Parser Error",
                "Parser returned an unexpected value. Expected a "
                "(code_str, warnings_list) tuple."
            )
            return
        code_str, warnings = parse_result
        if not isinstance(code_str, str):
            self.show_error(
                "Parser Error",
                f"Parser returned a non-string code "
                f"({type(code_str).__name__}); cannot apply."
            )
            return
        if not isinstance(warnings, list):
            log.warning(
                "Code parser returned non-list warnings (%s); coercing to [].",
                type(warnings).__name__,
            )
            warnings = []

        current_code = self.code_editor.toPlainText().strip()

        # Preserve operator-pinned bench-identification module constants
        # from the existing test.py. Per the v2.0.x design (2026-04-28
        # operator directive), bench fields (visa/port/baud/timeout/etc.)
        # live ONLY in test.py — and codegen's defaults (`ASRL1::INSTR`,
        # `COM1`) would otherwise clobber the operator's real bench
        # values on every regen.
        if current_code:
            from ..llm.code_constants_merge import preserve_bench_constants
            equipment_ids = [
                eq.get("id") for eq in (procedure.get("equipment") or [])
                if isinstance(eq, dict) and eq.get("id")
            ]
            code_str, replaced = preserve_bench_constants(
                code_str, current_code, equipment_ids,
            )
            if replaced:
                log.info(
                    "Quick Code: preserved %d operator-pinned constant(s): %s",
                    len(replaced), ", ".join(replaced),
                )

        if current_code:
            accepted, final_content = DiffViewer.show_diff(
                current_code,
                code_str,
                "Review Changes: test.py (Quick Code)",
                self,
            )
            if not accepted:
                self.main_window.dock.chat_panel.add_system_message(
                    "✗ Quick Code — changes rejected."
                )
                return
            code_str = final_content

        self.code_editor.setPlainText(code_str)
        self.artifact_manager.set_content(ArtifactType.TEST_CODE, code_str)
        self._code_dirty = True
        self._update_code_status()
        self._update_step_markers()

        if warnings:
            warn_lines = "\n".join(f"  • {w}" for w in warnings)
            self.main_window.dock.chat_panel.add_system_message(
                f"⚡ Quick Code complete — {len(warnings)} warning(s):\n{warn_lines}"
            )
        else:
            self.main_window.dock.chat_panel.add_system_message(
                "⚡ Quick Code complete — no warnings."
            )
        self.status_message.emit("⚡ Quick Code complete")
    
    def _on_step_clicked(self, item: QListWidgetItem):
        """Jump to step marker in code editor."""
        block = item.data(Qt.UserRole)
        if block:
            cursor = self.code_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            for _ in range(block.start_line - 1):
                cursor.movePosition(QTextCursor.MoveOperation.Down)
            self.code_editor.setTextCursor(cursor)
            self.code_editor.centerCursor()
    
    def _update_step_markers(self):
        """Update step markers sidebar from current code, with step text from JSON."""
        self.step_list.clear()
        code = self.code_editor.toPlainText()
        
        # Get step texts from JSON
        json_steps = []
        try:
            json_content = self.json_editor.toPlainText()
            if json_content.strip():
                json_data = json.loads(json_content)
                json_steps = json_data.get("steps", [])
        except (json.JSONDecodeError, Exception):
            pass
        
        if code:
            blocks = self._parser.parse(code)
            if blocks:
                for block in blocks:
                    step_num = block.step_number
                    
                    # Get step text from JSON if available
                    step_text = ""
                    if json_steps and step_num <= len(json_steps):
                        step_data = json_steps[step_num - 1]
                        if isinstance(step_data, dict):
                            step_text = step_data.get("text", "")
                        else:
                            step_text = str(step_data)
                    
                    # Display truncated text in list
                    if step_text:
                        display_text = f"Step {step_num}: {step_text[:40]}{'...' if len(step_text) > 40 else ''}"
                    else:
                        display_text = f"Step {step_num}"
                    
                    item = QListWidgetItem(display_text)
                    item.setData(Qt.UserRole, block)
                    # Full text in tooltip
                    tooltip = f"Lines {block.start_line}-{block.end_line}"
                    if step_text:
                        tooltip = f"{step_text}\n\n{tooltip}"
                    item.setToolTip(tooltip)
                    self.step_list.addItem(item)
                self.step_status.setText(f"{len(blocks)} steps")
            else:
                self.step_status.setText("No steps detected")
        else:
            self.step_status.setText("No code")
    
    def _update_json_status(self):
        """Update JSON status label."""
        if self._json_dirty:
            self.json_status.setText("● Modified")
            self.json_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.json_status.setText("✓ Saved")
            self.json_status.setStyleSheet(f"color: {status_saved()};")
    
    def _update_code_status(self):
        """Update code status label."""
        if self._code_dirty:
            self.code_status.setText("● Modified")
            self.code_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.code_status.setText("✓ Saved")
            self.code_status.setStyleSheet(f"color: {status_saved()};")
    
    def load_content(self):
        """Load both artifacts into editors."""
        if not self.artifact_manager:
            return
        
        # Load JSON
        json_content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON)
        self.json_editor.blockSignals(True)
        self.json_editor.setPlainText(json_content)
        self.json_editor.blockSignals(False)
        self._json_dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_JSON)
        self._update_json_status()
        
        # Load code
        code_content = self.artifact_manager.get_content(ArtifactType.TEST_CODE)
        self.code_editor.blockSignals(True)
        self.code_editor.setPlainText(code_content)
        self.code_editor.blockSignals(False)
        self._code_dirty = self.artifact_manager.is_dirty(ArtifactType.TEST_CODE)
        self._update_code_status()
        self._update_step_markers()
    
    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh both artifacts."""
        self.load_content()
