"""Shared LLM async pipeline for editor tabs.

Provides _run_task_async, response handling, validation, and error recovery
in a single mixin consumed by JsonCodeTab and TextJsonTab.

Tabs that use this mixin must implement:
  - _sync_editors_for_llm()              — sync editor widgets → artifact manager
  - _apply_proposals(response, task)     — dispatch per-artifact proposals
                                           (task drives the per-task section override)
  - _get_expected_artifact_fields()      — list of artifact field names for contract check
  - _parse_response_to_dict(response)    — map LLMResponse → validated dict
  - _get_task_description(task, user_message, custom_task_id) -> str

Instance attributes expected on ``self`` (provided by BaseTab / __init__):
  tab_id, tab_context, task_config_manager, main_window, status_message
"""

import json
import logging
from datetime import datetime
from typing import Optional

from ..llm import LLMTask, ChatMessage
from ..llm.prompt_builder import PromptBuilder, TaskPromptNotDeclaredError
from ..llm.output_contracts import get_contract_for_tab
from ..llm.reconstruction import pipeline_ownership
from ..core.task_config import DEFAULT_MAX_VALIDATOR_ATTEMPTS
from ..llm.backend_base import ValidationIssue
from ..llm.run_state import LLMRunState, RunStateKind
from ..llm.tab_context import ChatMessage as _ChatMessage
from ..llm.validator_dispatch import (
    ValidationOutcome,
    format_validator_feedback,
    validate_response,
)
from ..llm.worker import LLMWorker
from ..llm.transcript_replay import serialize_transcript

log = logging.getLogger(__name__)


class LLMTabMixin:
    """Mixin providing the shared LLM async pipeline for editor tabs."""

    # ------------------------------------------------------------------ #
    # Deterministic one-click pipeline (shared by Text tabs)              #
    # ------------------------------------------------------------------ #

    def _run_deterministic_parse_and_generate(self):
        """Strict one-click: Text → JSON → test.py with diff review.

        Shared by :class:`TextOnlyTab` and :class:`TextJsonTab`. Reads the
        live text editor, parses to JSON and generates code deterministically
        (no LLM), aborts on any parser/codegen warning, shows a DiffViewer for
        each artifact whose current content is non-empty, then writes
        ``procedure_text`` (if dirty), ``procedure.json`` and ``test.py``.

        Diff bases come from the ArtifactManager. The text and JSON editors
        mirror their content there on every keystroke, so those bases are
        fresh. The ``test_code`` base is whatever was last loaded/saved (no
        Text tab has a code editor to mirror from) — the diff preview for
        test.py may therefore lag an out-of-band edit, but the write itself
        is always correct.
        """
        from ..core import ArtifactType
        from ..llm import pack_parsers
        from ..llm.media_extraction import populate_media_on_steps
        from ..dialogs import DiffViewer

        text = self.text_editor.toPlainText().strip()
        if not text:
            self.show_warning("No Content", "Procedure text is empty. Add text first.")
            return

        project_root = getattr(self.project_manager, "project_root", None)

        def _abort(reason: str):
            self.main_window.dock.chat_panel.add_system_message(reason)

        # --- Text → JSON (strict) ---
        try:
            procedure, parse_warnings = pack_parsers.parse_text(
                text, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as e:
            self.show_warning("Parser Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Parse + Generate: parse_text raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e, title="Parse + Generate — parser findings",
                intro="The deterministic Text→JSON parser rejected the input.",
                parent=self,
            )
            return

        if parse_warnings:
            warn_lines = "\n".join(f"  • {w}" for w in parse_warnings)
            _abort(f"✗ Parse + Generate aborted — parser produced {len(parse_warnings)} warning(s).")
            self.show_warning(
                "Parse + Generate — strict",
                f"Parser produced {len(parse_warnings)} warning(s); aborting "
                f"(strict mode):\n\n{warn_lines}",
            )
            return

        populate_media_on_steps(procedure, project_root)
        # ensure_ascii=False matches the LLM JSON-proposal write path so the
        # diff base (artifact_manager content) doesn't show spurious \uXXXX vs
        # literal-unit-symbol noise (µ, ±, Ω are common in this domain).
        procedure_str = json.dumps(procedure, indent=2, ensure_ascii=False)

        # --- JSON → code (strict) ---
        try:
            code, gen_warnings = pack_parsers.generate_code(
                procedure, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as e:
            self.show_warning("Parser Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Parse + Generate: generate_code raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e, title="Parse + Generate — codegen findings",
                intro="The deterministic JSON→Code generator failed.",
                parent=self,
            )
            return

        if gen_warnings:
            warn_lines = "\n".join(f"  • {w}" for w in gen_warnings)
            _abort(f"✗ Parse + Generate aborted — codegen produced {len(gen_warnings)} warning(s).")
            self.show_warning(
                "Parse + Generate — strict",
                f"Codegen produced {len(gen_warnings)} warning(s); aborting "
                f"before writing files (strict mode):\n\n{warn_lines}",
            )
            return

        # --- Review diffs against current artifacts before writing ---
        current_json = (self.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON) or "").strip()
        if current_json:
            accepted, procedure_str = DiffViewer.show_diff(
                current_json, procedure_str,
                "Review Changes: procedure.json (Parse + Generate)",
                self,
            )
            if not accepted:
                _abort("✗ Parse + Generate — JSON changes rejected.")
                return

        current_code = (self.artifact_manager.get_content(ArtifactType.TEST_CODE) or "").strip()
        # Preserve operator-pinned bench-identification constants (VISA/COM,
        # baud, timeout, remote, channel) from the existing test.py. Codegen
        # emits inventory defaults (ASRL1::INSTR, COM1) that would otherwise
        # clobber the operator's real bench addresses on every regen. Same
        # merge json_code_tab's Quick Code applies.
        if current_code:
            from ..llm.code_constants_merge import preserve_bench_constants
            equipment_ids = [
                eq.get("id") for eq in (procedure.get("equipment") or [])
                if isinstance(eq, dict) and eq.get("id")
            ]
            code, replaced = preserve_bench_constants(code, current_code, equipment_ids)
            if replaced:
                log.info(
                    "Parse + Generate: preserved %d operator-pinned constant(s): %s",
                    len(replaced), ", ".join(replaced),
                )
        if current_code:
            accepted, code = DiffViewer.show_diff(
                current_code, code,
                "Review Changes: test.py (Parse + Generate)",
                self,
            )
            if not accepted:
                _abort("✗ Parse + Generate — code changes rejected.")
                return

        # --- Write: text (if dirty) → JSON → code ---
        if getattr(self, "_text_dirty", False):
            self.artifact_manager.set_content(
                ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText(),
            )
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_TEXT)
            self.artifact_manager.procedure_text.mark_clean()
            self._text_dirty = False
            if hasattr(self, "_update_text_status"):
                self._update_text_status()

        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, procedure_str)
        self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_JSON)
        self.artifact_manager.procedure_json.mark_clean()

        self.artifact_manager.set_content(ArtifactType.TEST_CODE, code)
        self.artifact_manager.save_artifact(ArtifactType.TEST_CODE)
        self.artifact_manager.test_code.mark_clean()

        # Reflect the new JSON in the tab's editor widget if it has one.
        if hasattr(self, "json_editor"):
            self.json_editor.setPlainText(procedure_str)
            self._json_dirty = False
            if hasattr(self, "_update_json_status"):
                self._update_json_status()

        self.main_window.dock.chat_panel.add_system_message(
            "⚡ Parse + Generate complete — procedure.json and test.py saved."
        )
        self.status_message.emit("⚡ Parse + Generate complete")
        self.artifact_saved.emit()

    # ------------------------------------------------------------------ #
    # Task dispatch                                                        #
    # ------------------------------------------------------------------ #

    def _run_task_async(self, task: LLMTask, **kwargs):
        """Run LLM task asynchronously in a worker thread."""
        # FSM bootstrap. Two entry paths:
        #   (a) Operator click — FSM is in IDLE/APPLIED/REJECTED/etc., or
        #       lingering AWAITING_REVIEW from a prior run the operator
        #       didn't explicitly accept/reject. Reset and start_run.
        #   (b) Auto-retry — FSM is in LLM_REQUESTED already (set by
        #       ``begin_retry``); leave it alone, attempt counter stays.
        run_state = self._ensure_run_state()
        if run_state.state != RunStateKind.LLM_REQUESTED:
            run_state.reset_to_idle()
            run_state.start_run(task, self._resolve_max_attempts(task))
        # Sync current editor content to artifact manager so the LLM sees
        # the latest unsaved state, not just the last saved version.
        self._sync_editors_for_llm()

        # Resolve the per-task section-ownership override once and thread the
        # SAME value into both prompt-build paths so the emit-list (here) and
        # _build_request's contract resolution never diverge.
        override = self._task_section_override(task)

        force_mode = self.main_window.dock.chat_panel.get_force_mode()
        request = self.tab_context._build_request(
            task, force=force_mode, section_override=override, **kwargs
        )
        self._pending_request = request

        # Carry a compact, text-only replay of the conversation SO FAR on the
        # request so the backend can self-heal a lost server session: if the
        # OpenCode server is replaced mid-chat it mints a fresh session and
        # replays this preamble instead of failing the turn. Built from the
        # in-memory transcript BEFORE the current user turn is appended below,
        # and ONLY when there is prior history (skips the window fetch on the
        # first send). Bounded to ~25% of the model's context window.
        # A review/rewrite is a ONE-SHOT on the current procedure, not a chat.
        # Replaying prior turns feeds the model earlier review turns + its own
        # "I preserved the steps" replies, which it then imitates. Retry feedback
        # rides request.user_message separately, so retries keep working.
        if self.tab_context.messages and task != LLMTask.REVIEW_TEXT_PROCEDURE:
            try:
                window = self.tab_context.backend.get_context_window()
            except Exception:
                window = None
            char_budget = int((window or 16000) * 4 * 0.25)
            request.conversation_preamble = serialize_transcript(
                self.tab_context.messages, char_budget
            )

        prompt_builder = PromptBuilder(
            task_config_manager=self.task_config_manager,
            tab_id=self.tab_id,
        )
        ownership = pipeline_ownership(
            self.tab_context.project_manager.project_root, override
        )
        contract = get_contract_for_tab(self.tab_context.tab_id, ownership=ownership)
        # Effective id: bundle-declared CUSTOM tasks route via AD_HOC_CHAT
        # but resolve THEIR OWN prompt_template (threaded on the request by
        # _build_request; the builder reads request.custom_task_id).
        custom_task_id = kwargs.get('custom_task_id', None)
        try:
            full_prompt = prompt_builder.build(request, output_contract_override=contract)
        except TaskPromptNotDeclaredError as exc:
            # Capability gate (task #22): buttons for undeclared tasks are
            # greyed out, so this only fires on programmatic invocation.
            # Fail loudly — never silently substitute prompt text.
            log.error("Refusing to run task '%s': %s", custom_task_id or task.value, exc)
            self.status_message.emit(str(exc))
            return
        # Attach the config-aware prompt so the backend sends THIS text
        # (its own builder is config-less and must not rebuild the prompt).
        request.prebuilt_prompt = full_prompt

        # Cancel any running worker before starting a new one.
        if hasattr(self, '_worker') and self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait()

        user_msg_text = kwargs.get('user_message', None)
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
        """Handle LLM response from the worker thread.

        Linear flow:
          1. UI cleanup (cancel-was-pressed early-return; remove thinking message; show raw).
          2. Parse the response (catastrophic parse failure short-circuits).
          3. Append the response as an assistant ChatMessage; update tokens.
          4. Publish output-contract issues to the dock findings panel.
          5. Short-circuit on response.success=False with operator error dialog.
          6. Drive the validator-in-the-loop FSM; on "retry" return without apply.
          7. Apply proposals; emit completion status.

        Most steps are extracted to private helpers to keep this body
        readable. The FSM-state side-effects of step 6 also reset to IDLE
        on terminal transitions; the caller-side proposal handlers don't
        need to touch the FSM.
        """
        self.main_window._play_notification_sound()
        is_active = self._is_active_tab()

        # 1. Cancel-was-pressed early-return.
        run_state = self._ensure_run_state()
        if run_state.cancelled:
            log.info("Dropping LLM response — run was cancelled.")
            self._cleanup_thinking_ui(response, is_active)
            run_state.reset_to_idle()
            return

        # Each new response (whether the original or a retry) starts the
        # findings panel from a clean slate. Without this, prior-attempt
        # issues piled up across retries — the operator saw an
        # accumulated soup at the end (~137 items in the worst report)
        # instead of the final attempt's residue. The two later panel
        # writes (``_publish_response_issues`` and the FSM driver) then
        # populate the panel with this attempt's issues only.
        if is_active:
            self.main_window.dock.show_validation_result_from_list([])

        self._cleanup_thinking_ui(response, is_active)

        # 2. Parse.
        try:
            parsed = self._parse_response_to_dict(response)
        except Exception as e:
            self._handle_parse_failure(response, e)
            run_state.reset_to_idle()
            return

        # 3. Build + append the assistant ChatMessage.
        validation_issues = self._validate_output_contract(parsed)
        self._record_assistant_message(parsed, response, validation_issues, is_active)

        # 4. Publish output-contract / response issues.
        self._publish_response_issues(response, validation_issues, is_active)

        # 5. Short-circuit on transport-level failure.
        if not response.success:
            # Special case: the backend recovered a lost server session this
            # turn, but replaying the prior conversation overflowed the context
            # window. The session IS valid now — guide the operator to resend a
            # smaller message instead of showing a scary hard-failure dialog.
            if (
                getattr(response, "session_rehydrated", False)
                and getattr(response, "context_exceeded", False)
                and is_active
            ):
                self.main_window.dock.chat_panel.add_message(
                    "system",
                    "Reconnected to a new session (the previous one was lost), "
                    "but replaying the prior conversation overflowed the context "
                    "window. The session is ready — please resend your message.",
                )
            else:
                self._handle_unsuccessful_response(response, is_active)
            run_state.reset_to_idle()
            return

        # 5b. If the backend transparently recovered a lost server session for
        # this turn, tell the operator plainly: the reply is genuine but any
        # earlier server-side tool results (netlist/BOM) were NOT replayed.
        if getattr(response, "session_rehydrated", False) and is_active:
            self.main_window.dock.chat_panel.add_message(
                "system",
                "Reconnected to a new session (the previous one was lost). "
                "Earlier tool results (netlist/BOM) were not replayed — "
                "text only.",
            )

        # 6. Drive the validator-in-the-loop FSM.
        #
        #   VALIDATING → AWAITING_REVIEW (validator passed / skipped)
        #     → operator DiffViewer → APPLIED / REJECTED → IDLE
        #   VALIDATING → VALIDATOR_FAIL_RETRYING (auto-retry)
        #     → re-spawn worker; this method re-enters on the next response.
        #   VALIDATING → FAILED_OUT_OF_RETRIES (toggle off / out of attempts)
        #     → operator DiffViewer with banner → APPLIED / REJECTED → IDLE
        fsm_decision = self._drive_validator_fsm(response)
        if fsm_decision == "retry":
            return
        if fsm_decision == "halt_for_operator":
            # Validator surfaced only operator-only errors (test ID,
            # description, pack versions). Retrying would waste turns —
            # surface findings to the operator without applying.
            return

        # 7. Apply proposals and finish. Thread the task so the apply-path
        # reconstruction uses the same per-task section override as the
        # prompt emit-list and the before-validate reconstruction.
        self._apply_proposals(response, task=run_state.task)
        self.status_message.emit(f"LLM task completed ({response.total_tokens} tokens)")

    # ------------------------------------------------------------------ #
    # _handle_llm_response sub-steps (extracted for readability)          #
    # ------------------------------------------------------------------ #

    def _cleanup_thinking_ui(self, response, is_active: bool) -> None:
        """Remove the chat panel's thinking-spinner and surface the raw
        response in the raw viewer. The chat panel is a shared dock so its
        re-enable must run unconditionally; only the active-tab-specific UI
        (thinking-message, raw viewer) is guarded."""
        self.main_window.dock.chat_panel.set_llm_active(False)
        if not is_active:
            return
        self.main_window.dock.chat_panel.remove_thinking_message()
        self.main_window.dock.raw_viewer.show_response(response.raw_response)

    def _record_assistant_message(
        self, parsed: dict, response, validation_issues: list, is_active: bool,
    ) -> None:
        """Build the assistant :class:`ChatMessage` from the parsed
        response, append to ``tab_context.messages``, refresh the chat
        panel if active, and confirm request delivery to the session
        state."""
        assistant_msg = self._create_assistant_message(parsed, response, validation_issues)
        chat_message = _ChatMessage(
            role="assistant",
            content=assistant_msg["content"],
            full_response=response.raw_response,
            thinking_content=getattr(response, "thinking_content", ""),
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
        )
        self.tab_context.messages.append(chat_message)
        self.tab_context.cumulative_tokens += response.total_tokens

        if response.success and getattr(self, "_pending_request", None):
            self.tab_context.confirm_request_delivered(self._pending_request)
            self._pending_request = None

        if is_active:
            # Prefer the active model's REAL context window (from the now-running
            # OpenCode server) over the static common_llm.context_window setting,
            # which is wrong for modern models (e.g. gpt-5.x: 272k+). Resolved
            # lazily here (the server is up post-response) and cached per backend;
            # leaves the static fallback in place when it can't be resolved.
            try:
                window = self.tab_context.backend.get_context_window()
            except Exception:
                window = None
            if isinstance(window, int) and window > 0:
                self.main_window.dock.chat_panel.set_context_limit(window)
            self.main_window.dock.chat_panel.switch_context(self.tab_context)

    def _publish_response_issues(
        self, response, validation_issues: list, is_active: bool,
    ) -> None:
        """Promote contract-validation strings to ``ValidationIssue``
        objects on the response, then push the merged list into the
        dock's findings panel. The validator-in-the-loop step adds its
        own issues to the same panel via :meth:`_drive_validator_fsm`,
        so the operator sees one consolidated finding list per turn."""
        if validation_issues:
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

    def _handle_unsuccessful_response(self, response, is_active: bool) -> None:
        """Surface a transport-level LLM failure (response.success=False)
        to the operator without further processing."""
        if is_active:
            self.main_window.dock.chat_panel.add_message(
                "system", f"❌ {response.error_message}"
            )
            self.show_error("LLM Error", response.error_message)

    def _handle_parse_failure(self, response, error: Exception):
        """Handle catastrophic parse failures (cannot parse LLM response at all)."""
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

        # Re-enable the shared chat panel unconditionally — gating it on the
        # originating tab still being active left the input permanently
        # disabled when the operator switched tabs mid-run.
        self.main_window.dock.chat_panel.set_llm_active(False)
        if is_active:
            self.main_window.dock.chat_panel.remove_thinking_message()

        # FSM transition: any LLM error (including cancel) terminates the
        # current run. Cancel routes through the cancel() helper so the
        # state goes to CANCELLED; other errors reset to IDLE so the
        # next operator-initiated task starts cleanly.
        run_state = self._ensure_run_state()
        if error_message == "Request cancelled by user":
            run_state.cancel()
            run_state.reset_to_idle()
            return

        run_state.reset_to_idle()
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
    # Validator-in-the-loop FSM driver                                    #
    # ------------------------------------------------------------------ #

    def _ensure_run_state(self) -> LLMRunState:
        """Lazily attach the per-tab :class:`LLMRunState`. Lazy creation
        keeps the data structure out of __init__ so existing tabs don't
        need to thread the import."""
        if not hasattr(self, "_run_state"):
            self._run_state = LLMRunState()
        return self._run_state

    def _task_section_override(self, task) -> Optional[list[str]]:
        """The task's per-button section-ownership override
        (``TaskConfig.llm_owned_sections``), or None to use the bundle
        default. None task → None.

        The ONLY place the TaskConfig lookup happens (DRY); the prompt
        emit-list, before-validate reconstruction, and at-apply
        reconstruction all resolve the override through here so a given
        task/button stays consistent across the pipeline.
        """
        if task is None or self.task_config_manager is None:
            return None
        cfg = self.task_config_manager.get_task_config(self.tab_id, task.value)
        return cfg.llm_owned_sections if cfg is not None else None

    def _resolve_max_attempts(self, task: LLMTask) -> int:
        """Determine the retry budget for this run.

        Resolution order (first-match wins):
          1. **Per-task TaskConfig override** — ``max_validator_attempts``
             on the task's TaskConfig. No UI surface today; reserved for
             future per-task tuning.
          2. **Per-project setting** — ``validator_loop.max_attempts`` in
             the project's ``config/config.json``. Surfaced via the
             Settings dialog's Validator tab (Phase 4).
          3. **Built-in default** — :data:`DEFAULT_MAX_VALIDATOR_ATTEMPTS`.

        Each layer is read defensively; transient I/O glitches at one
        layer fall through to the next rather than failing the run.
        """
        # 1. Per-task override (rare — no UI today).
        cfg = self.task_config_manager
        if cfg is not None:
            try:
                task_cfg = cfg.get_task_config(self.tab_id, task.value)
                override = getattr(task_cfg, "max_validator_attempts", None) if task_cfg else None
                if override is not None:
                    return max(1, int(override))
            except Exception:
                log.debug("Couldn't read max_validator_attempts from TaskConfig", exc_info=True)

        # 2. Per-project setting from Validator-tab persistence.
        project_root = getattr(self.tab_context.project_manager, "project_root", None)
        if project_root is not None:
            try:
                from ..llm.validator_loop_settings import load_settings
                section = load_settings(project_root)
                project_value = section.get("max_attempts")
                if project_value is not None:
                    return max(1, int(project_value))
            except Exception:
                log.debug("Couldn't read validator_loop.max_attempts", exc_info=True)

        # 3. Hardcoded default.
        return DEFAULT_MAX_VALIDATOR_ATTEMPTS

    def _is_auto_correct_enabled(self) -> bool:
        """Operator's runtime toggle — read from the chat-panel checkbox.

        Defaults to True if the chat panel is missing the getter (older
        builds, custom main windows). The chat panel's checkbox is greyed
        out when ``deterministic_path_available()`` returns False, so a
        True return here doesn't necessarily mean the loop will fire —
        :meth:`_drive_validator_fsm` short-circuits earlier on
        ``outcome.skipped``.
        """
        getter = getattr(self.main_window.dock.chat_panel, "get_auto_correct_enabled", None)
        if getter is None:
            return True
        try:
            return bool(getter())
        except Exception:
            log.exception("get_auto_correct_enabled() raised; defaulting to ON")
            return True

    def _drive_validator_fsm(self, response) -> str:
        """Advance the FSM by one step against the LLM response.

        Returns one of:
          - ``"continue"`` — caller should run ``_apply_proposals`` and
            reset the FSM via the proposal handlers.
          - ``"retry"`` — caller should NOT apply; the worker has been
            re-spawned and ``_handle_llm_response`` will fire again.
          - ``"halt_for_operator"`` — caller should NOT apply and NOT
            retry; every error is in an operator-only field that the
            LLM cannot fix by design. The operator must edit the source.

        Always returns; never raises. The FSM-state side-effects encode
        the outcome of this attempt and what happens next.
        """
        run_state = self._ensure_run_state()
        # If the FSM was never started (defensive — e.g. a request fired
        # outside the normal _run_task_async flow), reset to a clean
        # state and fall through to apply. Without the reset, the FSM
        # would linger in a non-terminal state and the next operator
        # task would still be cleared up by ``_run_task_async``'s
        # bootstrap, but an intermediate validator query would see
        # stale data.
        if run_state.state != RunStateKind.LLM_REQUESTED:
            log.debug(
                "FSM unexpectedly in %s when handling response; "
                "resetting and falling back to apply-only.",
                run_state.state.name,
            )
            run_state.reset_to_idle()
            return "continue"

        run_state.begin_validating(response)
        outcome = self._compute_validator_outcome(response)
        run_state.last_outcome = outcome

        if outcome.skipped:
            self._maybe_warn_validator_unavailable(outcome.reason)
            run_state.on_validator_pass(outcome)
            return "continue"

        if outcome.ok:
            # Replace the panel with the validator's empty result.
            # Otherwise the LLM's review-side `validation.issues[]` (which
            # describe the PRE-proposal state) linger after the proposal
            # is applied — the operator accepts a clean proposal and is
            # left staring at "11 findings" that describe a state the
            # accept just overwrote. The validator is the authority on
            # the post-apply state; if it says ok, the panel is empty.
            self.main_window.dock.show_validation_result_from_list([])
            self.main_window.dock.chat_panel.add_message(
                "system",
                self._format_pass_message(run_state),
            )
            run_state.on_validator_pass(outcome)
            return "continue"

        # Failure path: surface issues through the existing dock panel
        # so the layout is identical to output-contract violations.
        self.main_window.dock.show_validation_result_from_list(
            [issue.to_dock_dict() for issue in outcome.issues]
        )

        # All-operator-only short-circuit: every error is in a field the
        # LLM rewriter does not touch by design (test ID, description,
        # pack versions). Retrying cannot fix any of them.
        if outcome.all_operator_only:
            run_state.give_up_now(outcome)
            self.main_window.dock.chat_panel.add_message(
                "system",
                "Validator rejected the response, but every error is in an "
                "operator-only field (test ID, description, or pack version) "
                "that the LLM rewriter does not modify. Edit the original "
                "procedure to fix the flagged lines, then retry.",
            )
            return "halt_for_operator"

        # Operator-toggle: when auto-correct is off, treat any failure
        # as an immediate give-up — fall through to operator review.
        if not self._is_auto_correct_enabled():
            run_state.give_up_now(outcome)
            self.main_window.dock.chat_panel.add_message(
                "system",
                self._format_give_up_message(run_state, auto_correct_off=True),
            )
            return "continue"

        decision = run_state.on_validator_fail(outcome)
        if decision == "retry":
            self.main_window.dock.chat_panel.add_message(
                "system",
                self._format_retry_message(run_state),
            )
            self._spawn_retry_worker(outcome)
            return "retry"

        # Out of retries — fall through to operator review with banner.
        self.main_window.dock.chat_panel.add_message(
            "system",
            self._format_give_up_message(run_state, auto_correct_off=False),
        )
        return "continue"

    def _compute_validator_outcome(self, response) -> ValidationOutcome:
        """Build the :class:`ValidationOutcome` for the current response.

        Single point of contact with the dispatcher — keeps the FSM driver
        independent of the artifact-manager layout."""
        project_root = getattr(self.tab_context.project_manager, "project_root", None)
        artifacts = self.tab_context.artifact_manager
        run_state = self._ensure_run_state()
        current = {
            "text": getattr(artifacts.procedure_text, "content", "") or "",
            "json": getattr(artifacts.procedure_json, "content", "") or "",
            "code": getattr(artifacts.test_code, "content", "") or "",
        }
        # Phase 3 (2026-04-27): dispatch is artifact-shape, not LLMTask.
        # The validator follows whatever the LLM proposed; covers buttons,
        # custom tasks, and ad-hoc chat uniformly. The per-task section
        # override threads through so before-validate reconstruction uses the
        # same LLM-owned set as the prompt emit-list and at-apply path.
        override = self._task_section_override(run_state.task)
        return validate_response(
            response, current, project_root, task_override=override
        )

    def _spawn_retry_worker(self, outcome: ValidationOutcome) -> None:
        """Spawn a new LLM worker carrying the validator feedback as the
        retry's user-role message. The FSM is already in
        ``VALIDATOR_FAIL_RETRYING``; we transition to ``LLM_REQUESTED``
        via :meth:`LLMRunState.begin_retry` here."""
        run_state = self._ensure_run_state()
        feedback = format_validator_feedback(
            outcome,
            attempt=run_state.attempt,
            max_attempts=run_state.max_attempts,
        )
        run_state.begin_retry()
        # Re-enter the worker-spawn path. _run_task_async sees a non-
        # terminal FSM (LLM_REQUESTED) so it does NOT call start_run,
        # preserving the attempt counter.
        self._run_task_async(run_state.task, user_message=feedback)

    # ------------------------------------------------------------------ #
    # Per-attempt chat-panel messages (small helpers, easy to tune)       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_pass_message(run_state: LLMRunState) -> str:
        if run_state.attempt > 1:
            return f"✓ Validator passed on attempt {run_state.attempt}/{run_state.max_attempts}."
        return "✓ Deterministic validator passed."

    @staticmethod
    def _format_retry_message(run_state: LLMRunState) -> str:
        n = len(run_state.last_outcome.issues) if run_state.last_outcome else 0
        # ``run_state.attempt`` was already incremented to the upcoming
        # attempt by ``on_validator_fail`` — show the just-failed one.
        failed_attempt = run_state.attempt - 1
        return (
            f"⟳ Validator rejected attempt {failed_attempt}/{run_state.max_attempts} "
            f"({n} issue{'s' if n != 1 else ''}). Re-prompting…"
        )

    @staticmethod
    def _format_give_up_message(
        run_state: LLMRunState, *, auto_correct_off: bool
    ) -> str:
        n = len(run_state.last_outcome.issues) if run_state.last_outcome else 0
        if auto_correct_off:
            return (
                f"⚠ Validator rejected the response ({n} issue"
                f"{'s' if n != 1 else ''}). Auto-correct is off; opening "
                f"DiffViewer with banner — review and apply, fix manually, "
                f"or reject."
            )
        return (
            f"⚠ Auto-correct exhausted after {run_state.max_attempts} "
            f"attempt{'s' if run_state.max_attempts != 1 else ''} "
            f"({n} residual issue{'s' if n != 1 else ''}). "
            f"DiffViewer is opening with the last response and a banner; "
            f"review and apply, fix manually, or reject."
        )

    def _maybe_warn_validator_unavailable(self, reason: str) -> None:
        """Emit a once-per-tab system message when the deterministic path
        isn't available, then suppress further notifications until the
        tab is recreated. Avoids spamming the chat with the same banner
        on every LLM call.

        Suppressed entirely when the operator explicitly opted out via
        ``validator_loop.enabled=false`` — in that case there's nothing
        to warn about, the user already knows (Phase 4.6).
        """
        if getattr(self.tab_context, "_validator_unavailable_warned", False):
            return
        if "disabled in project settings" in reason:
            # User opt-out → don't nag.
            self.tab_context._validator_unavailable_warned = True
            return
        self.tab_context._validator_unavailable_warned = True
        self.main_window.dock.chat_panel.add_message(
            "system",
            f"ℹ Deterministic validator unavailable ({reason}). "
            f"Falling back to operator review only — the DiffViewer remains "
            f"the gate before any LLM-proposed change is applied.",
        )

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
