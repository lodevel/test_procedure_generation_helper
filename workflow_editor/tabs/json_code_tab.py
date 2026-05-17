"""
JSON-Code Tab - Paired editors for JSON↔Code transformation.

Left: JSON editor (procedure.json) + preview
Right: Code editor (test.py) + step markers

Actions support bidirectional transformation.
"""

import logging
import json
from typing import Any
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
        # Phase 5.x: step list is sourced from procedure_json["steps"]
        # (canonical upstream of code). This dict maps each JSON step's
        # `n` to its code-side StepBlock (if any) so click jumps can
        # position the code editor. Repopulated by _update_step_markers
        # on every re-render.
        self._code_blocks_by_n: dict[int, Any] = {}
    
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
        """Show or hide the Quick Code button based on whether the
        rules_packager_base wheel imports cleanly in the project venv."""
        from ..llm import pack_parsers
        project_root = getattr(self.project_manager, "project_root", None)
        available, _ = pack_parsers.is_available(project_root)
        self.quick_code_btn.setVisible(available)

    def _on_quick_code(self):
        """Deterministic JSON → Code without LLM (rule-based, instant)."""
        from ..llm import pack_parsers
        json_text = self.json_editor.toPlainText().strip()
        if not json_text:
            self.show_warning("No Content", "JSON editor is empty. Add JSON before generating code.")
            return
        try:
            procedure = json.loads(json_text)
        except json.JSONDecodeError as e:
            self.show_error("Invalid JSON", f"Cannot parse procedure JSON:\n\n{e}")
            return

        try:
            code_str, warnings = pack_parsers.generate_code(
                procedure,
                getattr(self.project_manager, "project_root", None),
            )
        except pack_parsers.ParserUnavailable as e:
            self.show_warning("Code Generator Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Quick Code: codegen raised")
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
        """Click a step → jump in BOTH editors (JSON always; code when
        a matching block exists).

        The item's UserRole carries the step's ``n`` (int). Code blocks
        are looked up via the per-render cache populated by
        :meth:`_update_step_markers`. Per the v2.0.1 invariant (JSON
        upstream of code), the step is always in JSON; the only
        possible mismatch is "JSON has step, code doesn't yet" — the
        code jump is skipped for those rows.
        """
        step_num = item.data(Qt.UserRole)
        if not isinstance(step_num, int):
            return
        # Code editor — only if codegen has produced a block for this n.
        block = self._code_blocks_by_n.get(step_num)
        if block:
            cursor = self.code_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            for _ in range(block.start_line - 1):
                cursor.movePosition(QTextCursor.MoveOperation.Down)
            self.code_editor.setTextCursor(cursor)
            self.code_editor.centerCursor()
        # JSON editor — always. The step exists in JSON by construction.
        self._focus_json_step(step_num)

    def _focus_json_step(self, step_num: int) -> None:
        """Scroll the JSON editor to the step's ``"n": <num>`` line and
        select it. The step is guaranteed to be in JSON (it was sourced
        from JSON); the only way to miss is an operator manually
        reformatting the JSON off the canonical ``indent=2`` shape, in
        which case we log debug and no-op rather than guess."""
        import re
        json_text = self.json_editor.toPlainText()
        # Line-anchored match on the indented JSON shape produced by
        # `json.dumps(..., indent=2)`. Word-boundary on the number
        # prevents matching `"n": 12` when looking for step 1.
        pattern = re.compile(rf'^\s*"n":\s*{step_num}\b', re.MULTILINE)
        m = pattern.search(json_text)
        if m is None:
            log.debug(
                "Could not locate step %d in JSON editor (non-canonical "
                "indent?); no-op'ing the jump.",
                step_num,
            )
            return
        line_start = json_text.rfind("\n", 0, m.start()) + 1
        cursor = self.json_editor.textCursor()
        cursor.setPosition(line_start)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine,
            QTextCursor.MoveMode.KeepAnchor,
        )
        self.json_editor.setTextCursor(cursor)
        self.json_editor.centerCursor()

    def _update_step_markers(self):
        """Rebuild the step list from procedure.json's ``steps[]`` (the
        canonical upstream of code). Code markers are consulted only to
        annotate which steps codegen has produced.

        Status semantics:
        - ✓ Step N: <text>  — JSON has it AND code has a matching
          ``# STEP_BEGIN N`` block.
        - ✗ Step N: <text>  — JSON has it; code doesn't (regenerate
          test.py to fill in).
        """
        self.step_list.clear()
        self._code_blocks_by_n = {}

        # JSON is the source of truth for the step list.
        json_steps: list[Any] = []
        try:
            json_content = self.json_editor.toPlainText()
            if json_content.strip():
                json_data = json.loads(json_content)
                json_steps = json_data.get("steps", []) or []
        except (json.JSONDecodeError, Exception):
            pass
        if not json_steps:
            self.step_status.setText("No steps in JSON")
            return

        # Secondary: code-marker map for click→code-jump.
        code = self.code_editor.toPlainText()
        if code:
            for block in self._parser.parse(code):
                self._code_blocks_by_n[block.step_number] = block

        # Display-text lookup from procedure_text.md.
        from ..llm.pack_parsers import step_texts_from_canonical
        proc_text = self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT)
        canonical_lookup = step_texts_from_canonical(proc_text or "")

        with_code = 0
        for step in json_steps:
            if not isinstance(step, dict):
                continue
            n = step.get("n")
            if not isinstance(n, int):
                continue

            step_text = canonical_lookup.get(n) or step.get("text", "") or ""
            block = self._code_blocks_by_n.get(n)
            if block:
                with_code += 1

            if step_text:
                trimmed = f"{step_text[:40]}{'...' if len(step_text) > 40 else ''}"
                body = f"Step {n}: {trimmed}"
            else:
                body = f"Step {n}"
            prefix = "✓" if block else "✗"

            item = QListWidgetItem(f"{prefix} {body}")
            item.setData(Qt.UserRole, n)
            if not block:
                item.setForeground(Qt.red)
            tooltip_parts: list[str] = []
            if step_text:
                tooltip_parts.append(step_text)
            if block:
                tooltip_parts.append(f"Code lines {block.start_line}-{block.end_line}")
            else:
                tooltip_parts.append("(no code block — regenerate test.py)")
            item.setToolTip("\n\n".join(tooltip_parts))
            self.step_list.addItem(item)

        total = len(json_steps)
        self.step_status.setText(f"{total} steps in JSON ({with_code} with code)")
    
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
