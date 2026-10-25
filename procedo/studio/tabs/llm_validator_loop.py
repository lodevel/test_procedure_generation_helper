"""The validator-in-the-loop of a view's LLM run: the deterministic check of a reply (on a
worker), the FSM step its outcome drives (apply, retry with the findings, or halt), and the
chat's per-attempt messages. Held by an LLM view (``LLMTabMixin.validator_loop``)."""

import functools
import logging
from typing import Callable

from ..core.validation_outcome import ValidationOutcome
from ..llm.run_state import LLMRunState, RunStateKind
from ..llm.validator_dispatch import (
    format_validator_feedback,
    validate_response,
)
from ..pack_ops import engine_async

log = logging.getLogger(__name__)


class ValidatorLoop:
    """Checks each reply of a view's run and drives its :class:`LLMRunState` with the outcome."""

    def __init__(
        self,
        *,
        main_window,
        tab_context,
        run_state: LLMRunState,
        status: Callable[[str], None],
        section_override: Callable,
        drop_late_reply: Callable[[], bool],
        is_active: Callable[[], bool],
        show_error: Callable[[str, str], None],
        finish: Callable,
        run_task: Callable,
    ) -> None:
        """*main_window* gives the dock (findings, chat) and the loading cue; *tab_context* the
        project and the artifacts; *run_state* is the view's run.
        The view's own steps: *status* shows a line, *section_override* is the task's section
        ownership, *drop_late_reply* drops an answer for a closed test (True when dropped),
        *is_active* says whether the view is shown, *show_error* opens an error dialog,
        *finish* (response, decision) applies the proposals, *run_task* asks the LLM again
        (``_run_task_async``)."""
        self._main_window = main_window
        self._tab_context = tab_context
        self._run_state = run_state
        self._status = status
        self._section_override = section_override
        self._drop_late_reply = drop_late_reply
        self._is_active = is_active
        self._show_error = show_error
        self._finish = finish
        self._run_task = run_task
        #: The token of the check still awaited (None: none is).
        self._awaited = None
        #: The once-per-tab "validator unavailable" message was posted (or the operator opted out).
        self._unavailable_warned = False

    def drop(self) -> None:
        """A check still running belongs to the run replaced now: its answer is dropped."""
        if self._awaited is not None:
            self._awaited = None
            self._main_window.dock.chat_panel.set_llm_active(False)

    def _is_auto_correct_enabled(self) -> bool:
        """Operator's runtime toggle — read from the chat-panel checkbox.

        Defaults to True if the chat panel is missing the getter (older
        builds, custom main windows). The chat panel's checkbox is greyed
        out when ``deterministic_path_available()`` returns False, so a
        True return here doesn't necessarily mean the loop will fire —
        :meth:`_drive` short-circuits earlier on
        ``outcome.skipped``.
        """
        getter = getattr(self._main_window.dock.chat_panel, "get_auto_correct_enabled", None)
        if getter is None:
            return True
        try:
            return bool(getter())
        except Exception:
            log.exception("get_auto_correct_enabled() raised; defaulting to ON")
            return True

    def start(self, response) -> None:
        """Start the deterministic check of *response* on a worker; the chat stays locked (no
        new message, nothing to cancel) until :meth:`_on_answer` resumes. Without a
        run in LLM_REQUESTED (defensive: a request fired outside the normal flow) the FSM is
        reset and the proposals are applied unchecked."""
        run_state = self._run_state
        if run_state.state != RunStateKind.LLM_REQUESTED:
            log.debug("FSM in %s when handling a response: reset, apply-only", run_state.state.name)
            run_state.reset_to_idle()
            self._finish(response, "continue")
            return
        run_state.begin_validating(response)
        # The inputs are read here, on the GUI thread; the worker only reads its copies.
        check = self._check_call(response)
        token = self._awaited = object()
        self._main_window.dock.chat_panel.set_llm_active(True, cancellable=False)
        self._status("Checking the proposal…")
        engine_async.call(
            getattr(self._tab_context.project_manager, "project_root", None),
            check,
            lambda answer: self._on_answer(token, answer),
            cue=self._main_window.loading,
            cue_label="Checking the proposal\u2026",
        )

    def _claim(self, token) -> bool:
        """True when *token*'s check is the one still awaited (it no longer is, the chat unlocks);
        False for a late answer: a newer run, or a closed test, dropped the check."""
        if token is not self._awaited:
            log.info("Dropped a late proposal-check result: its run was replaced")
            return False
        self._awaited = None
        self._main_window.dock.chat_panel.set_llm_active(False)
        return True

    def _on_answer(self, token, answer) -> None:
        """The check answered: continue the FSM with its outcome. A check that raised ends the
        run, its error shown as an LLM error is."""
        if not self._claim(token) or self._drop_late_reply():
            return
        try:
            outcome = answer()
        except Exception as exc:  # noqa: BLE001 — told to the operator
            log.error("The proposal check failed: %s", exc, exc_info=True)
            self._run_state.reset_to_idle()
            if self._is_active():
                self._show_error("Validator Error", str(exc))
            return
        response = self._run_state.last_response
        self._finish(response, self._drive(response, outcome))

    def _drive(self, response, outcome: ValidationOutcome) -> str:
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
        run_state = self._run_state
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
            self._main_window.dock.show_validation_result_from_list([])
            self._main_window.dock.chat_panel.add_message(
                "system",
                self._format_pass_message(run_state),
                tone="success",
            )
            run_state.on_validator_pass(outcome)
            return "continue"

        # Failure path: surface issues through the existing dock panel
        # so the layout is identical to output-contract violations.
        self._main_window.dock.show_validation_result_from_list(
            [issue.to_dock_dict() for issue in outcome.issues]
        )

        # All-operator-only short-circuit: every error is in a field the
        # LLM rewriter does not touch by design (test ID, description,
        # pack versions). Retrying cannot fix any of them.
        if outcome.all_operator_only:
            run_state.give_up_now(outcome)
            self._main_window.dock.chat_panel.add_message(
                "system",
                "Validator rejected the response, but every error is in an "
                "operator-only field (test ID, description, or pack version) "
                "that the LLM rewriter does not modify. Edit the original "
                "procedure to fix the flagged lines, then retry.",
                tone="warning",
            )
            return "halt_for_operator"

        # Operator-toggle: when auto-correct is off, treat any failure
        # as an immediate give-up — fall through to operator review.
        if not self._is_auto_correct_enabled():
            run_state.give_up_now(outcome)
            self._main_window.dock.chat_panel.add_message(
                "system",
                self._format_give_up_message(run_state, auto_correct_off=True),
                tone="warning",
            )
            return "continue"

        decision = run_state.on_validator_fail(outcome)
        if decision == "retry":
            self._main_window.dock.chat_panel.add_message(
                "system",
                self._format_retry_message(run_state),
                tone="warning",
            )
            self._spawn_retry(outcome)
            return "retry"

        # Out of retries — fall through to operator review with banner.
        self._main_window.dock.chat_panel.add_message(
            "system",
            self._format_give_up_message(run_state, auto_correct_off=False),
            tone="warning",
        )
        return "continue"

    def _check_call(self, response):
        """The no-argument call that builds the :class:`ValidationOutcome` for *response*, with
        its inputs read now.

        Single point of contact with the dispatcher — keeps the FSM driver
        independent of the artifact-manager layout."""
        project_root = getattr(self._tab_context.project_manager, "project_root", None)
        artifacts = self._tab_context.artifact_manager
        run_state = self._run_state
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
        override = self._section_override(run_state.task)
        return functools.partial(
            validate_response, response, current, project_root, task_override=override
        )

    def _spawn_retry(self, outcome: ValidationOutcome) -> None:
        """Spawn a new LLM worker carrying the validator feedback as the
        retry's user-role message. The FSM is already in
        ``VALIDATOR_FAIL_RETRYING``; we transition to ``LLM_REQUESTED``
        via :meth:`LLMRunState.begin_retry` here."""
        run_state = self._run_state
        feedback = format_validator_feedback(
            outcome,
            attempt=run_state.attempt,
            max_attempts=run_state.max_attempts,
        )
        run_state.begin_retry()
        # Re-enter the worker-spawn path as a retry: no start_run, so the
        # attempt counter (and the run's test) are preserved.
        self._run_task(run_state.task, retry=True, user_message=feedback)

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
    def _format_give_up_message(run_state: LLMRunState, *, auto_correct_off: bool) -> str:
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
        if self._unavailable_warned:
            return
        if "disabled in project settings" in reason:
            # User opt-out → don't nag.
            self._unavailable_warned = True
            return
        self._unavailable_warned = True
        self._main_window.dock.chat_panel.add_message(
            "system",
            f"ℹ Deterministic validator unavailable ({reason}). "
            f"Falling back to operator review only — your Accept on its proposal remains "
            f"the gate before any LLM-proposed change is applied.",
        )
