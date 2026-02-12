"""
JSON-Code Tab - Paired editors for JSON↔Code transformation.

Left: JSON editor (procedure.json) + preview
Right: Code editor (test.py) + step markers

Actions support bidirectional transformation.
"""

import logging
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QLabel, QPlainTextEdit,
    QListWidget, QListWidgetItem, QWidget
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor

from .base_tab import BaseTab
from .json_tab import JsonSyntaxHighlighter
from .code_tab import PythonSyntaxHighlighter
from ..core import ArtifactType, JsonValidator, CodeValidator, StepMarkerParser
from ..llm import TabContext, LLMTask, ChatMessage
from ..llm.prompt_builder import PromptBuilder
from ..llm.output_contracts import get_contract_for_tab
from ..dialogs import DiffViewer
import json
from datetime import datetime

log = logging.getLogger(__name__)

class JsonCodeTab(BaseTab):
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
        self._json_validator = JsonValidator()
        self._code_validator = CodeValidator()
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
        
        # Format/Validate buttons
        format_row = QHBoxLayout()
        self.format_json_btn = self.create_button("Format JSON", self._on_format_json,
            tooltip="Auto-format JSON with proper indentation")
        self.validate_json_btn = self.create_button("Validate JSON", self._on_validate_json,
            tooltip="Run local JSON schema validation")
        self.check_syntax_btn = self.create_button("Check Python Syntax", self._on_check_syntax,
            tooltip="Check Python code for syntax errors")
        format_row.addWidget(self.format_json_btn)
        format_row.addWidget(self.validate_json_btn)
        format_row.addWidget(self.check_syntax_btn)
        format_row.addStretch()
        file_layout.addLayout(format_row)
        
        layout.addWidget(file_group)
        
        # LLM Actions Group — built dynamically from BaseTab
        layout.addWidget(self._create_llm_action_group())
        
        return layout
    
    # Event handlers
    def _on_json_changed(self):
        """Handle JSON editor changes."""
        self._json_dirty = True
        self._update_json_status()
        self.tab_context.mark_artifact_modified("procedure_json")
        self.content_changed.emit()
    
    def _on_code_changed(self):
        """Handle code editor changes."""
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
            self.tab_context.mark_artifact_modified("procedure_json")
            self._json_dirty = False
            self._update_json_status()
            self.status_message.emit("JSON saved successfully")
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_save_code(self):
        """Save code artifact."""
        try:
            content = self.code_editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.TEST_CODE, content)
            self.artifact_manager.save_artifact(ArtifactType.TEST_CODE)
            self.tab_context.mark_artifact_modified("test_code")
            self._code_dirty = False
            self._update_code_status()
            self.status_message.emit("Code saved successfully")
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
    
    def _on_validate_json(self):
        """Run local JSON validation."""
        content = self.json_editor.toPlainText()
        result = self._json_validator.validate(content)
        
        # Update findings in dock
        self.main_window.dock.show_validation_result(result)
        
        if result.is_valid and not result.has_warnings:
            self.show_info("Validation", "JSON is valid!")
        elif result.is_valid:
            self.show_warning("Validation", f"JSON is valid but has {len(result.issues)} warnings.")
        else:
            self.show_error("Validation", f"JSON has {len(result.issues)} issues.")
    
    def _on_check_syntax(self):
        """Check Python syntax."""
        content = self.code_editor.toPlainText()
        result = self._code_validator.validate(content)
        
        # Update findings in dock
        self.main_window.dock.show_validation_result(result)
        
        if result.is_valid and not result.has_warnings:
            self.show_info("Syntax Check", "Python syntax is valid!")
        elif result.is_valid:
            self.show_warning("Syntax Check", f"Syntax is valid but has {len(result.issues)} warnings.")
        else:
            self.show_error("Syntax Check", f"Syntax has {len(result.issues)} errors.")
    
    def _on_generate_code(self):
        """JSON → Code transformation (strict mode)."""
        if not self.artifact_manager.procedure_json.content:
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True)
    
    def _on_force_generate_code(self):
        """JSON → Code transformation (force mode)."""
        if not self.artifact_manager.procedure_json.content:
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False)
    
    def _on_derive_json(self):
        """Code → JSON transformation (strict mode)."""
        if not self.artifact_manager.test_code.content:
            self.show_warning("No Code", "Code editor is empty. Write code first.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_CODE, strict_mode=True)
    
    def _on_force_derive_json(self):
        """Code → JSON transformation (force mode)."""
        if not self.artifact_manager.test_code.content:
            self.show_warning("No Code", "Code editor is empty. Write code first.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_CODE, strict_mode=False)
    
    def _on_review_json(self):
        """Review JSON with LLM."""
        if not self.artifact_manager.procedure_json.content:
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_JSON)
    
    def _on_review_code(self):
        """Review code with LLM."""
        if not self.artifact_manager.test_code.content:
            self.show_warning("No Code", "Code editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_CODE)
    
    def _on_check_coherence(self):
        """Check JSON↔Code coherence."""
        if not self.artifact_manager.procedure_json.content:
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        if not self.artifact_manager.test_code.content:
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
    
    def _run_task_async(self, task: LLMTask, **kwargs):
        """Run LLM task asynchronously in worker thread."""
        from workflow_editor.main_window import LLMWorker
        
        # Sync current editor content to artifact manager
        # This ensures LLM sees current editor state, not just last saved state
        json_content = self.json_editor.toPlainText()
        code_content = self.code_editor.toPlainText()
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, json_content)
        self.artifact_manager.set_content(ArtifactType.TEST_CODE, code_content)
        
        # Get force mode from chat panel
        force_mode = self.main_window.dock.chat_panel.get_force_mode()
        
        request = self.tab_context._build_request(task, force=force_mode, **kwargs)
        
        # Build the full prompt for storage
        prompt_builder = PromptBuilder(
            task_config_manager=self.task_config_manager,
            tab_id=self.tab_id
        )
        contract = get_contract_for_tab(self.tab_context.tab_id)
        full_prompt = prompt_builder.build(request, output_contract_override=contract)
        
        # Cancel any existing worker
        if hasattr(self, '_worker') and self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait()
        
        # Add user message to chat before starting worker
        # Extract user_message from kwargs if present (for AD_HOC_CHAT)
        user_msg_text = kwargs.get('user_message', None)
        custom_task_id = kwargs.get('custom_task_id', None)
        user_message = ChatMessage(
            role="user",
            content=self._get_task_description(task, user_msg_text, custom_task_id=custom_task_id),
            full_prompt=full_prompt
        )
        self.tab_context.messages.append(user_message)
        self.main_window.dock.chat_panel.switch_context(self.tab_context)
        self.main_window.dock.chat_panel.add_thinking_message()
        
        # Create and start new worker
        self._worker = LLMWorker(self.tab_context.backend, request, parent=self)
        self._worker.finished.connect(self._handle_llm_response)
        self._worker.error.connect(self._handle_llm_error)
        self._worker.start()
        
        self.status_message.emit(f"Running {task.name}...")
    
    def _validate_output_contract(self, parsed: dict) -> list[str]:
        """Validate parsed response against expected output contract.
        
        Returns list of validation issues (empty if valid).
        """
        issues = []
        
        # Check required fields - assistant_message must exist (but can be empty)
        if "assistant_message" not in parsed:
            issues.append("Missing required field: assistant_message")
        
        # Validate open_questions structure
        if "open_questions" not in parsed:
            issues.append("Missing field: open_questions (will default to [])")
        elif not isinstance(parsed["open_questions"], list):
            issues.append(f"Field 'open_questions' must be list, got {type(parsed['open_questions'])}")
        elif not all(isinstance(q, str) for q in parsed["open_questions"]):
            issues.append("All elements in 'open_questions' must be strings")
        
        # Check propose_update type
        if "propose_update" in parsed and not isinstance(parsed["propose_update"], bool):
            issues.append(f"Field 'propose_update' must be boolean, got {type(parsed['propose_update'])}")
        
        # Logical validation - relaxed for propose_update
        if parsed.get("propose_update") is True:
            # Check if at least one artifact field is populated
            artifact_fields = self._get_expected_artifact_fields()
            has_artifact = any(
                parsed.get(field) and str(parsed.get(field)).strip()
                for field in artifact_fields
            )
            if not has_artifact:
                # Allow if assistant provided explanation (arbitrary 20-char threshold)
                msg = parsed.get("assistant_message", "")
                if len(msg) < 20:
                    issues.append(
                        f"propose_update=True but no artifact fields populated "
                        f"and no explanation given (expected one of: {', '.join(artifact_fields)})"
                    )
        
        return issues
    
    def _get_expected_artifact_fields(self) -> list[str]:
        """Get expected artifact fields for this tab."""
        # For json_code_tab: procedure_json and test_code
        return ["procedure_json", "test_code"]
    
    def _parse_response_to_dict(self, response) -> dict:
        """Parse LLMResponse object into dict format for validation.
        
        Args:
            response: LLMResponse object from TabContext
            
        Returns:
            Dict with fields expected by _validate_output_contract()
        """
        parsed = {
            "assistant_message": response.assistant_message,
            "open_questions": response.session_delta.get("open_questions", []),
        }
        
        # Determine if we have a proposal
        parsed["propose_update"] = False
        
        # Check for procedure_json proposal
        if response.procedure_json and response.procedure_json.mode:
            parsed["propose_update"] = True
            parsed["procedure_json"] = response.procedure_json.content
        
        # Check for test_code proposal
        if response.test_code and response.test_code.mode:
            parsed["propose_update"] = True
            parsed["test_code"] = response.test_code.content
        
        return parsed
    
    def _handle_llm_response(self, response):
        """Handle LLM response from TabContext."""
        # Remove thinking message first
        self.main_window.dock.chat_panel.remove_thinking_message()
        
        # ALWAYS show raw response (even on validation failure, for debugging)
        self.main_window.dock.raw_viewer.show_response(response.raw_response)
        
        # Parse response into dict for validation
        try:
            parsed = self._parse_response_to_dict(response)
        except Exception as e:
            # CRITICAL FAILURE: Can't parse at all
            self._handle_parse_failure(response, e)
            return
        
        # Validate output contract (tab-specific validation)
        validation_issues = self._validate_output_contract(parsed)
        
        # Create assistant message with validation metadata
        assistant_msg = self._create_assistant_message(parsed, response, validation_issues)
        
        # Add to conversation history
        from ..llm.tab_context import ChatMessage
        chat_message = ChatMessage(
            role="assistant",
            content=assistant_msg["content"],
            full_response=response.raw_response,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens
        )
        self.tab_context.messages.append(chat_message)
        self.tab_context.cumulative_tokens += response.total_tokens
        
        # Update chat panel with latest messages
        self.main_window.dock.chat_panel.switch_context(self.tab_context)
        
        # Handle validation issues
        contract_issues = validation_issues
        
        # If contract validation found issues, display them
        if contract_issues:
            # Convert string issues to ValidationIssue format for display
            from ..llm.backend_base import ValidationIssue
            contract_validation_issues = [
                ValidationIssue(
                    message=issue,
                    severity="warning",  # Use warning severity for contract issues
                    location="response_structure",
                    code="OUTPUT_CONTRACT",
                    suggested_fix=""
                )
                for issue in contract_issues
            ]
            # Add to response issues
            response.issues.extend(contract_validation_issues)
        
        # Show validation issues BEFORE success check (so users see findings even when validation fails)
        if response.has_issues:
            issues_as_dicts = [
                {
                    "message": issue.message,
                    "severity": issue.severity,
                    "location": issue.location,
                    "code": issue.code,
                    "suggested_fix": issue.suggested_fix
                }
                for issue in response.issues
            ]
            self.main_window.dock.show_validation_result_from_list(issues_as_dicts)
        
        # Now check success (may be False due to validation failure)
        if not response.success:
            # Add system message to chat for visibility
            self.main_window.dock.chat_panel.add_message("system", f"❌ {response.error_message}")
            
            # Also show error dialog
            self.show_error("LLM Error", response.error_message)
            return
        
        # Handle proposals (only if validation passed)
        if response.procedure_json and response.procedure_json.mode:
            self._handle_json_proposal(response.procedure_json)
        
        if response.test_code and response.test_code.mode:
            self._handle_code_proposal(response.test_code)
        
        self.status_message.emit("Task completed successfully")
        self.status_message.emit(f"LLM task completed ({response.total_tokens} tokens)")
    
    def _handle_parse_failure(self, response, error: Exception):
        """Handle catastrophic parse failures (can't parse response at all)."""
        from ..llm.backend_base import ValidationIssue
        from ..llm.tab_context import ChatMessage
        from datetime import datetime
        
        # Create error message for user
        error_msg = {
            "severity": "error",
            "location": "response_parsing",
            "code": "PARSE_FAILURE",
            "message": f"Failed to parse LLM response: {str(error)}"
        }
        
        # Add to response issues
        response.issues.append(ValidationIssue(**error_msg))
        
        # Display error (convert ValidationIssue to dict for display)
        issue_dict = {
            "message": response.issues[-1].message,
            "severity": response.issues[-1].severity,
            "location": response.issues[-1].location,
            "code": response.issues[-1].code,
            "suggested_fix": response.issues[-1].suggested_fix
        }
        self.main_window.dock.show_validation_result_from_list([issue_dict])
        
        # Create assistant message with parse failure metadata
        error_content = f"⚠️ Parse Error: {str(error)}"
        chat_message = ChatMessage(
            role="assistant",
            content=error_content,
            full_response=response.raw_response,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens
        )
        self.tab_context.messages.append(chat_message)
        self.tab_context.cumulative_tokens += response.total_tokens
        
        # Update chat panel to show the failure
        self.main_window.dock.chat_panel.switch_context(self.tab_context)
    
    def _handle_llm_error(self, error_message: str):
        """Handle LLM error from worker thread."""
        self.main_window.dock.chat_panel.remove_thinking_message()
        self.show_error("LLM Error", error_message)
    
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
            
            # Show diff dialog for user to accept/reject
            current_content = self.code_editor.toPlainText()
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
            import json
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
            self.json_status.setStyleSheet("color: orange;")
        else:
            self.json_status.setText("✓ Saved")
            self.json_status.setStyleSheet("color: green;")
    
    def _update_code_status(self):
        """Update code status label."""
        if self._code_dirty:
            self.code_status.setText("● Modified")
            self.code_status.setStyleSheet("color: orange;")
        else:
            self.code_status.setText("✓ Saved")
            self.code_status.setStyleSheet("color: green;")
    
    def _create_assistant_message(
        self, 
        parsed: dict,
        response,
        validation_issues: list
    ) -> dict:
        """Create assistant message with validation metadata.
        
        Args:
            parsed: Parsed response dictionary
            response: LLMResponse object
            validation_issues: List of validation issue strings
            
        Returns:
            Message dict with role, content, and metadata
        """
        message = {
            "role": "assistant",
            "content": parsed.get("assistant_message", ""),
            "metadata": {
                "validation_issues": validation_issues,
                "contract_violated": len(validation_issues) > 0,
                "timestamp": datetime.now().isoformat()
            }
        }
        
        # Include session delta info if present
        if hasattr(response, 'session_delta') and response.session_delta:
            if hasattr(response.session_delta, 'open_questions'):
                message["metadata"]["open_questions"] = response.session_delta.open_questions
        
        return message
    
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
