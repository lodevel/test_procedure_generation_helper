"""Shared LLM async pipeline for editor tabs.

Provides _run_task_async, response handling, validation, and error recovery
in a single mixin consumed by JsonCodeTab and TextJsonTab.

Tabs that use this mixin must implement:
  - _sync_editors_for_llm()              — sync editor widgets → artifact manager
  - _apply_proposals(response)           — dispatch per-artifact proposals
  - _get_expected_artifact_fields()      — list of artifact field names for contract check
  - _parse_response_to_dict(response)    — map LLMResponse → validated dict
  - _get_task_description(task, user_message, custom_task_id) -> str

Instance attributes expected on ``self`` (provided by BaseTab / __init__):
  tab_id, tab_context, task_config_manager, main_window, status_message
"""

import logging
from datetime import datetime

from ..llm import LLMTask, ChatMessage
from ..llm.prompt_builder import PromptBuilder
from ..llm.output_contracts import get_contract_for_tab
from ..llm.worker import LLMWorker

log = logging.getLogger(__name__)


class LLMTabMixin:
    """Mixin providing the shared LLM async pipeline for editor tabs."""

    # ------------------------------------------------------------------ #
    # Task dispatch                                                        #
    # ------------------------------------------------------------------ #

    def _run_task_async(self, task: LLMTask, **kwargs):
        """Run LLM task asynchronously in a worker thread."""
        # Sync current editor content to artifact manager so the LLM sees
        # the latest unsaved state, not just the last saved version.
        self._sync_editors_for_llm()

        force_mode = self.main_window.dock.chat_panel.get_force_mode()
        request = self.tab_context._build_request(task, force=force_mode, **kwargs)
        self._pending_request = request

        prompt_builder = PromptBuilder(
            task_config_manager=self.task_config_manager,
            tab_id=self.tab_id,
        )
        contract = get_contract_for_tab(self.tab_context.tab_id)
        full_prompt = prompt_builder.build(request, output_contract_override=contract)

        # Cancel any running worker before starting a new one.
        if hasattr(self, '_worker') and self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait()

        user_msg_text = kwargs.get('user_message', None)
        custom_task_id = kwargs.get('custom_task_id', None)
        user_message = ChatMessage(
            role="user",
            content=self._get_task_description(task, user_msg_text, custom_task_id=custom_task_id),
            full_prompt=full_prompt,
        )
        self.tab_context.messages.append(user_message)
        self.main_window.dock.chat_panel.switch_context(self.tab_context)
        self.main_window.dock.chat_panel.add_thinking_message()

        self._worker = LLMWorker(self.tab_context.backend, request, parent=self)
        self._worker.finished.connect(self._handle_llm_response)
        self._worker.error.connect(self._handle_llm_error)
        self._worker.thinking_chunk.connect(self.main_window.dock.chat_panel.append_thinking_text)
        self._worker.text_chunk.connect(self.main_window.dock.chat_panel.append_response_text)
        self._worker.start()

        self.main_window.dock.chat_panel.set_llm_active(True)
        self.status_message.emit(f"Running {task.name}...")

    # ------------------------------------------------------------------ #
    # Response handling                                                   #
    # ------------------------------------------------------------------ #

    def _handle_llm_response(self, response):
        """Handle LLM response from the worker thread."""
        self.main_window._play_notification_sound()
        is_active = self._is_active_tab()

        if is_active:
            self.main_window.dock.chat_panel.set_llm_active(False)
            self.main_window.dock.chat_panel.remove_thinking_message()
            self.main_window.dock.raw_viewer.show_response(response.raw_response)

        try:
            parsed = self._parse_response_to_dict(response)
        except Exception as e:
            self._handle_parse_failure(response, e)
            return

        validation_issues = self._validate_output_contract(parsed)
        assistant_msg = self._create_assistant_message(parsed, response, validation_issues)

        from ..llm.tab_context import ChatMessage as _ChatMessage
        chat_message = _ChatMessage(
            role="assistant",
            content=assistant_msg["content"],
            full_response=response.raw_response,
            thinking_content=getattr(response, 'thinking_content', ''),
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
        )
        self.tab_context.messages.append(chat_message)
        self.tab_context.cumulative_tokens += response.total_tokens

        if response.success and hasattr(self, '_pending_request') and self._pending_request:
            self.tab_context.confirm_request_delivered(self._pending_request)
            self._pending_request = None

        if is_active:
            self.main_window.dock.chat_panel.switch_context(self.tab_context)

        if validation_issues:
            from ..llm.backend_base import ValidationIssue
            response.issues.extend([
                ValidationIssue(
                    message=issue,
                    severity="warning",
                    location="response_structure",
                    code="OUTPUT_CONTRACT",
                    suggested_fix="",
                )
                for issue in validation_issues
            ])

        if response.has_issues and is_active:
            self.main_window.dock.show_validation_result_from_list([
                {
                    "message": issue.message,
                    "severity": issue.severity,
                    "location": issue.location,
                    "code": issue.code,
                    "suggested_fix": issue.suggested_fix,
                }
                for issue in response.issues
            ])

        if not response.success:
            if is_active:
                self.main_window.dock.chat_panel.add_message("system", f"❌ {response.error_message}")
                self.show_error("LLM Error", response.error_message)
            return

        self._apply_proposals(response)
        self.status_message.emit(f"LLM task completed ({response.total_tokens} tokens)")

    def _handle_parse_failure(self, response, error: Exception):
        """Handle catastrophic parse failures (cannot parse LLM response at all)."""
        from ..llm.backend_base import ValidationIssue
        from ..llm.tab_context import ChatMessage as _ChatMessage

        response.issues.append(ValidationIssue(
            severity="error",
            location="response_parsing",
            code="PARSE_FAILURE",
            message=f"Failed to parse LLM response: {str(error)}",
        ))
        issue = response.issues[-1]
        self.main_window.dock.show_validation_result_from_list([{
            "message": issue.message,
            "severity": issue.severity,
            "location": issue.location,
            "code": issue.code,
            "suggested_fix": issue.suggested_fix,
        }])

        chat_message = _ChatMessage(
            role="assistant",
            content=f"⚠️ Parse Error: {str(error)}",
            full_response=response.raw_response,
            thinking_content=getattr(response, 'thinking_content', ''),
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
        )
        self.tab_context.messages.append(chat_message)
        self.tab_context.cumulative_tokens += response.total_tokens

        if self._is_active_tab():
            self.main_window.dock.chat_panel.switch_context(self.tab_context)

    def _handle_llm_error(self, error_message: str):
        """Handle LLM error from the worker thread."""
        self._pending_request = None
        is_active = self._is_active_tab()

        if is_active:
            self.main_window.dock.chat_panel.set_llm_active(False)
            self.main_window.dock.chat_panel.remove_thinking_message()

        if error_message == "Request cancelled by user":
            return

        if is_active:
            self.show_error("LLM Error", error_message)

    # ------------------------------------------------------------------ #
    # Contract validation                                                 #
    # ------------------------------------------------------------------ #

    def _validate_output_contract(self, parsed: dict) -> list[str]:
        """Validate parsed response against expected output contract.

        Returns a list of validation issue strings (empty if valid).
        """
        issues = []

        if "assistant_message" not in parsed:
            issues.append("Missing required field: assistant_message")

        if "open_questions" not in parsed:
            issues.append("Missing field: open_questions (will default to [])")
        elif not isinstance(parsed["open_questions"], list):
            issues.append(
                f"Field 'open_questions' must be list, got {type(parsed['open_questions'])}"
            )
        elif not all(isinstance(q, str) for q in parsed["open_questions"]):
            issues.append("All elements in 'open_questions' must be strings")

        if "propose_update" in parsed and not isinstance(parsed["propose_update"], bool):
            issues.append(
                f"Field 'propose_update' must be boolean, got {type(parsed['propose_update'])}"
            )

        if parsed.get("propose_update") is True:
            artifact_fields = self._get_expected_artifact_fields()
            has_artifact = any(
                parsed.get(field) and str(parsed.get(field)).strip()
                for field in artifact_fields
            )
            if not has_artifact:
                msg = parsed.get("assistant_message", "")
                if len(msg) < 20:
                    issues.append(
                        f"propose_update=True but no artifact fields populated "
                        f"and no explanation given (expected one of: {', '.join(artifact_fields)})"
                    )

        return issues

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _is_active_tab(self) -> bool:
        """Return True if this tab is the currently visible tab."""
        return self.main_window.tab_widget.currentWidget() is self

    def _create_assistant_message(
        self,
        parsed: dict,
        response,
        validation_issues: list,
    ) -> dict:
        """Build assistant message dict with validation metadata."""
        message = {
            "role": "assistant",
            "content": parsed.get("assistant_message", ""),
            "metadata": {
                "validation_issues": validation_issues,
                "contract_violated": len(validation_issues) > 0,
                "timestamp": datetime.now().isoformat(),
            },
        }

        if hasattr(response, 'session_delta') and response.session_delta:
            if hasattr(response.session_delta, 'open_questions'):
                message["metadata"]["open_questions"] = response.session_delta.open_questions

        return message
