"""Shared LLM async pipeline for editor tabs.

Provides _run_task_async, response handling, validation, and error recovery
in a single mixin consumed by JsonCodeTab and TextJsonTab.

Tabs that use this mixin must implement:
  - _apply_proposals(response, task)     — dispatch per-artifact proposals
                                           (task drives the per-task section override)
and declare ``_proposal_fields``: the (LLMResponse attribute, contract field) of each artifact the
tab may propose, in order.

Instance attributes expected on ``self`` (provided by BaseTab / __init__):
  tab_id, tab_context, task_config_manager, main_window, status_message
"""

import functools
import logging
from datetime import datetime
from typing import Optional

from ..core.proposal import current_test
from ..llm import LLMTask, ChatMessage
from ..llm.backend_factory import SERVER_STARTING_TEXT
from ..llm.prompt_builder import PromptBuilder, TaskPromptNotDeclaredError
from ..llm.output_contracts import get_contract_for_tab
from ..llm.reconstruction import pipeline_ownership
from ..llm.backend_base import ValidationIssue
from ..llm.run_state import LLMRunState, RunStateKind
from ..llm.tab_context import ChatMessage as _ChatMessage
from ..core.validation_outcome import ValidationOutcome
from ..llm.validator_dispatch import (
    format_validator_feedback,
    validate_response,
)
from ..llm.worker import LLMWorker
from ..pack_ops import engine_async
from ..llm.transcript_replay import serialize_transcript

log = logging.getLogger(__name__)


class LLMTabMixin:
    """Mixin providing the shared LLM async pipeline for editor tabs."""

    #: The (LLMResponse attribute, contract field) of each artifact this tab may propose.
    _proposal_fields: tuple[tuple[str, str], ...] = ()

    # ------------------------------------------------------------------ #
    # Task dispatch                                                        #
    # ------------------------------------------------------------------ #

    def _run_task_async(self, task: LLMTask, retry: bool = False, **kwargs):
        """Run LLM task asynchronously in a worker thread (*retry*: the auto-correct loop's
        next attempt of the current run). A tab with no backend yet waits for its start
        (:meth:`_await_backend`)."""
        backend = self.tab_context.backend
        if backend is None:
            self._await_backend(task, retry, kwargs)
            return
        self._run_task_on(backend, task, retry, kwargs, self._user_line(task, kwargs))

    def _user_line(self, task: LLMTask, kwargs: dict) -> ChatMessage:
        """The chat's user line for a send (its prompt is added when it goes)."""
        return ChatMessage(role="user", content=self._get_task_description(
            task, kwargs.get('user_message'), custom_task_id=kwargs.get('custom_task_id')))

    def _await_backend(self, task: LLMTask, retry: bool, kwargs: dict) -> None:
        """Queue the send until the tab's backend has started off the GUI thread (the AI
        server's start can take a minute); its line shows in the chat at once, and the chat is
        locked and shows the loading cue meanwhile. A second send meanwhile replaces the queued
        one (the chat says so) and waits for the same start."""
        chat = self.main_window.dock.chat_panel
        line = self._user_line(task, kwargs)
        replaced = getattr(self, "_queued_send", None) is not None
        if replaced:
            what = ("a new message" if task == LLMTask.AD_HOC_CHAT
                    and not kwargs.get('custom_task_id') else line.content)
            chat.post(self.tab_context, ChatMessage(
                role="system", content=f"Not sent: replaced by {what}.", tone="warning"))
        chat.post(self.tab_context, line)
        self._queued_send = (task, retry, kwargs, current_test(self.artifact_manager), line)
        if replaced:
            return
        chat.set_llm_active(True, cancellable=False)
        chat.loading.start(SERVER_STARTING_TEXT)
        self.tab_context.request_backend(self._on_backend_started)

    def _on_backend_started(self, answer) -> None:
        """The backend start answered (GUI thread): the queued send runs on that backend (on a
        fallback ``NoneBackend`` its reply is the server's failure), unless its test was closed
        meanwhile."""
        backend = self.tab_context.adopt_backend(answer)
        chat = self.main_window.dock.chat_panel
        chat.loading.done()
        chat.set_llm_active(False)
        task, retry, kwargs, test, line = self._queued_send
        self._queued_send = None
        if test != current_test(self.artifact_manager):
            log.warning("Dropped the send queued for %s: its test was closed",
                        test[1] if test else None)
            chat.post(self.tab_context, ChatMessage(
                role="system", content="Not sent: the test changed while the AI server was "
                "starting.", tone="warning"))
            return
        self._run_task_on(backend, task, retry, kwargs, line)

    def _run_task_on(self, backend, task: LLMTask, retry: bool, kwargs: dict,
                     line: ChatMessage) -> None:
        """:meth:`_run_task_async` on the tab's started *backend*; *line* is the send's user
        line (already in the chat when the send waited for the backend)."""
        # FSM bootstrap. Two entry paths:
        #   (a) Operator request — whatever the FSM holds (a finished run, or
        #       one a failed request left in LLM_REQUESTED) is reset, and a
        #       new run starts on the test open now.
        #   (b) Auto-retry — ``begin_retry`` put the FSM in LLM_REQUESTED;
        #       the run goes on, attempt counter and test included.
        run_state = self._ensure_run_state()
        if getattr(self, "_validator_check", None) is not None:
            # A check still running belongs to the run replaced now: its answer is dropped.
            self._validator_check = None
            self.main_window.dock.chat_panel.set_llm_active(False)
        if not retry:
            run_state.reset_to_idle()
            run_state.start_run(task, self._resolve_max_attempts(task),
                                current_test(self.artifact_manager))
        # Sync current editor content to artifact manager so the LLM sees
        # the latest unsaved state, not just the last saved version.
        self.sync_editors_to_artifacts()

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
        history = [m for m in self.tab_context.messages if m is not line]
        if history and task != LLMTask.REVIEW_TEXT_PROCEDURE:
            try:
                window = backend.get_context_window()
            except Exception:
                window = None
            char_budget = int((window or 16000) * 4 * 0.25)
            request.conversation_preamble = serialize_transcript(history, char_budget)

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

        line.full_prompt = full_prompt
        if not any(m is line for m in self.tab_context.messages):
            self.tab_context.messages.append(line)
        self.main_window.dock.chat_panel.switch_context(self.tab_context)
        self.main_window.dock.chat_panel.add_thinking_message()

        self._worker = LLMWorker(backend, request, parent=self)
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
          0. A reply whose test is no longer open is dropped whole (:meth:`_drop_late_reply`).
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
        if self._drop_late_reply():
            return
        self.main_window._assistant_controller.play_notification_sound()
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

        # 6. Drive the validator-in-the-loop FSM. The check runs on a worker (~6 engine round
        #    trips); steps 6b-7 resume in :meth:`_on_validator_result` when it answers.
        #   VALIDATING → AWAITING_REVIEW (passed / skipped) → proposal card → APPLIED / REJECTED
        #   VALIDATING → VALIDATOR_FAIL_RETRYING → re-spawn worker; re-enters on its response
        #   VALIDATING → FAILED_OUT_OF_RETRIES (toggle off / out of attempts) → card with warning
        self._start_validator_check(response)

    def _finish_response(self, response, fsm_decision: str) -> None:
        """6b + 7. A retry or an operator-only halt ends here (the retry's reply re-enters the
        handler; on a halt the findings are the operator's to fix); else apply the proposals,
        the task threaded for the apply-path reconstruction's per-task section override."""
        if fsm_decision in ("retry", "halt_for_operator"):
            return
        self._apply_proposals(response, task=self._ensure_run_state().task)
        self.status_message.emit(f"LLM task completed ({response.total_tokens} tokens)")

    # ------------------------------------------------------------------ #
    # _handle_llm_response sub-steps (extracted for readability)          #
    # ------------------------------------------------------------------ #

    def _drop_late_reply(self) -> bool:
        """The worker answered (a reply or an error) for a test that is no longer open: drop it
        whole — nothing validated, recorded or shown for the test open now (its chat is that
        test's), only the log line; the spinner goes and the run ends. True when dropped."""
        run_state = self._ensure_run_state()
        if run_state.test == current_test(self.artifact_manager):
            return False
        log.warning("Dropped the LLM reply for %s: discarded, its test was closed",
                    run_state.test[1] if run_state.test else None)
        self._pending_request = None
        self._end_thinking(self._is_active_tab())
        run_state.reset_to_idle()
        return True

    def _end_thinking(self, is_active: bool) -> None:
        """Re-enable the chat panel and remove its thinking-spinner. The chat panel is a shared
        dock so its re-enable runs unconditionally; the spinner is the active tab's."""
        self.main_window.dock.chat_panel.set_llm_active(False)
        if is_active:
            self.main_window.dock.chat_panel.remove_thinking_message()

    def _cleanup_thinking_ui(self, response, is_active: bool) -> None:
        """End the thinking UI (:meth:`_end_thinking`) and surface the raw response in the raw
        viewer (the active tab's)."""
        self._end_thinking(is_active)
        if is_active:
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
                "system", f"❌ {response.error_message}", tone="danger"
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
        if self._drop_late_reply():
            return
        self._pending_request = None
        is_active = self._is_active_tab()

        # Re-enable the shared chat panel unconditionally — gating it on the
        # originating tab still being active left the input permanently
        # disabled when the operator switched tabs mid-run.
        self._end_thinking(is_active)

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

    def _get_expected_artifact_fields(self) -> list[str]:
        """The contract fields of the artifacts this tab may propose."""
        return [field for _attr, field in self._proposal_fields]

    def _parse_response_to_dict(self, response) -> dict:
        """Parse LLMResponse into the dict format expected by _validate_output_contract: each
        artifact of ``_proposal_fields`` the reply proposes (a mode set) under its field."""
        parsed = {
            "assistant_message": response.assistant_message,
            "open_questions": response.session_delta.get("open_questions", []),
            "propose_update": False,
        }
        for attr, field in self._proposal_fields:
            artifact = getattr(response, attr)
            if artifact and artifact.mode:
                parsed["propose_update"] = True
                parsed[field] = artifact.content
        return parsed

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
          3. **Built-in default** — ``task_config.DEFAULT_MAX_VALIDATOR_ATTEMPTS``.

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

        # 2. Per-project setting from Validator-tab persistence, 3. the default.
        from ..llm.validator_loop_settings import max_attempts
        return max_attempts(getattr(self.tab_context.project_manager, "project_root", None))

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

    def _start_validator_check(self, response) -> None:
        """Start the deterministic check of *response* on a worker; the chat stays locked (no
        new message, nothing to cancel) until :meth:`_on_validator_result` resumes. Without a
        run in LLM_REQUESTED (defensive: a request fired outside the normal flow) the FSM is
        reset and the proposals are applied unchecked."""
        run_state = self._ensure_run_state()
        if run_state.state != RunStateKind.LLM_REQUESTED:
            log.debug("FSM in %s when handling a response: reset, apply-only", run_state.state.name)
            run_state.reset_to_idle()
            self._finish_response(response, "continue")
            return
        run_state.begin_validating(response)
        # The inputs are read here, on the GUI thread; the worker only reads its copies.
        check = self._validator_check_call(response)
        token = self._validator_check = object()
        self.main_window.dock.chat_panel.set_llm_active(True, cancellable=False)
        self.status_message.emit("Checking the proposal…")
        engine_async.call(getattr(self.tab_context.project_manager, "project_root", None), check,
                          lambda answer: self._on_validator_answer(token, answer),
                          cue=self.main_window.loading, cue_label="Checking the proposal\u2026")

    def _claim_validator_check(self, token) -> bool:
        """True when *token*'s check is the one still awaited (it no longer is, the chat unlocks);
        False for a late answer: a newer run, or a closed test, dropped the check."""
        if token is not getattr(self, "_validator_check", None):
            log.info("Dropped a late proposal-check result: its run was replaced")
            return False
        self._validator_check = None
        self.main_window.dock.chat_panel.set_llm_active(False)
        return True

    def _on_validator_answer(self, token, answer) -> None:
        """The check answered: continue the FSM with its outcome. A check that raised ends the
        run, its error shown as an LLM error is."""
        if not self._claim_validator_check(token) or self._drop_late_reply():
            return
        try:
            outcome = answer()
        except Exception as exc:  # noqa: BLE001 — told to the operator
            log.error("The proposal check failed: %s", exc, exc_info=True)
            self._ensure_run_state().reset_to_idle()
            if self._is_active_tab():
                self.show_error("Validator Error", str(exc))
            return
        response = self._ensure_run_state().last_response
        self._finish_response(response, self._drive_validator_fsm(response, outcome))

    def _drive_validator_fsm(self, response, outcome: ValidationOutcome) -> str:
        """Advance the FSM (in VALIDATING) by one step with the check's *outcome* for *response*.

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
                self._format_pass_message(run_state), tone="success",
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
                "procedure to fix the flagged lines, then retry.", tone="warning",
            )
            return "halt_for_operator"

        # Operator-toggle: when auto-correct is off, treat any failure
        # as an immediate give-up — fall through to operator review.
        if not self._is_auto_correct_enabled():
            run_state.give_up_now(outcome)
            self.main_window.dock.chat_panel.add_message(
                "system",
                self._format_give_up_message(run_state, auto_correct_off=True), tone="warning",
            )
            return "continue"

        decision = run_state.on_validator_fail(outcome)
        if decision == "retry":
            self.main_window.dock.chat_panel.add_message(
                "system",
                self._format_retry_message(run_state), tone="warning",
            )
            self._spawn_retry_worker(outcome)
            return "retry"

        # Out of retries — fall through to operator review with banner.
        self.main_window.dock.chat_panel.add_message(
            "system",
            self._format_give_up_message(run_state, auto_correct_off=False), tone="warning",
        )
        return "continue"

    def _validator_check_call(self, response):
        """The no-argument call that builds the :class:`ValidationOutcome` for *response*, with
        its inputs read now.

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
        return functools.partial(
            validate_response, response, current, project_root, task_override=override
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
        # Re-enter the worker-spawn path as a retry: no start_run, so the
        # attempt counter (and the run's test) are preserved.
        self._run_task_async(run_state.task, retry=True, user_message=feedback)

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
                f"{'s' if n != 1 else ''}). Auto-correct is off; it is proposed "
                f"with a warning — review and apply, fix manually, "
                f"or reject."
            )
        return (
            f"⚠ Auto-correct exhausted after {run_state.max_attempts} "
            f"attempt{'s' if run_state.max_attempts != 1 else ''} "
            f"({n} residual issue{'s' if n != 1 else ''}). "
            f"The last response is proposed with a warning; "
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
            f"Falling back to operator review only — your Accept on its proposal remains "
            f"the gate before any LLM-proposed change is applied.",
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _is_active_tab(self) -> bool:
        """Return True if this tab is the currently visible tab."""
        return self.main_window.view_stack.currentWidget() is self

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
