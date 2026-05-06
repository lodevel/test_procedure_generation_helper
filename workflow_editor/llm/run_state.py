"""Per-tab run-state FSM for the validator-in-the-loop auto-retry.

Replaces the implicit flag-soup (`_pending_request`, `_worker.isRunning()`,
`_dirty`, `tab_context._current_task`) with an explicit state machine.

The FSM is intentionally narrow:
  - it owns *only* the lifecycle of one logical "operator clicked a task
    button" → "result applied / rejected" run, including the auto-retry
    iterations in between;
  - it knows nothing about Qt, LLM backends, or the chat panel — those
    live in :mod:`llm_tab_mixin` and consume the FSM via its public
    transition methods.

Splitting it out means the auto-retry loop's transition logic is one
small testable unit, not buried inside ``_handle_llm_response``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .backend_base import LLMResponse, LLMTask
from .validator_dispatch import ValidationOutcome


class RunStateKind(Enum):
    """Discrete states a single LLM run passes through.

    Each transition method on :class:`LLMRunState` enforces the legal-edges
    diagram below. Illegal transitions raise ``RuntimeError`` so bugs in
    the orchestrator surface fast rather than silently miswiring.

    Diagram::

        IDLE ──start_run──▶ LLM_REQUESTED
                              │
                       on_llm_response
                              ▼
                         VALIDATING
                       /     │      \\
        on_validator_pass    │   on_validator_fail (attempt < max)
              │              │              │
              ▼              │              ▼
        AWAITING_REVIEW      │       VALIDATOR_FAIL_RETRYING
              │              │              │
       review accepted     on_validator_fail (attempt >= max)
              │              │              │
              ▼              ▼              │
           APPLIED   FAILED_OUT_OF_RETRIES  │
                                            │
                                  start_run resets to LLM_REQUESTED
                                  for the next attempt
    """

    IDLE = "idle"
    LLM_REQUESTED = "llm_requested"
    VALIDATING = "validating"
    VALIDATOR_FAIL_RETRYING = "validator_fail_retrying"
    AWAITING_REVIEW = "awaiting_review"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED_OUT_OF_RETRIES = "failed_out_of_retries"
    CANCELLED = "cancelled"


# Terminal states the FSM resets from before starting a new run.
_TERMINAL = frozenset({
    RunStateKind.IDLE,
    RunStateKind.APPLIED,
    RunStateKind.REJECTED,
    RunStateKind.FAILED_OUT_OF_RETRIES,
    RunStateKind.CANCELLED,
})


@dataclass
class LLMRunState:
    """Mutable state for one in-flight LLM run, including auto-retries.

    Lives on ``LLMTabMixin`` as ``self._run_state``. Created on
    ``start_run``; remains attached for the run's lifetime; cleared back
    to ``IDLE`` on any terminal transition.

    The dataclass holds no Qt references on purpose — it is purely the
    truth-state. The orchestrator (mixin) reads these fields to decide
    whether to spawn another worker, show a banner, etc.
    """

    state: RunStateKind = RunStateKind.IDLE
    task: Optional[LLMTask] = None
    attempt: int = 0
    max_attempts: int = 3
    last_response: Optional[LLMResponse] = None
    last_outcome: Optional[ValidationOutcome] = None
    cancelled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    """Free-form storage for the orchestrator (e.g. operator-toggle
    snapshot at run start). Kept here so the dataclass is the single
    place the run's state lives."""

    # ------------------------------------------------------------------ #
    # Transitions                                                         #
    # ------------------------------------------------------------------ #

    def start_run(self, task: LLMTask, max_attempts: int) -> None:
        """Begin a new run. Resets attempt counter; clears prior outcome."""
        self._require_in(_TERMINAL, "start_run")
        self.state = RunStateKind.LLM_REQUESTED
        self.task = task
        self.attempt = 1
        self.max_attempts = max(1, int(max_attempts))
        self.last_response = None
        self.last_outcome = None
        self.cancelled = False
        self.metadata = {}

    def begin_validating(self, response: LLMResponse) -> None:
        """LLM worker returned; about to run the deterministic validator."""
        self._require_in({RunStateKind.LLM_REQUESTED}, "begin_validating")
        self.state = RunStateKind.VALIDATING
        self.last_response = response

    def on_validator_pass(self, outcome: ValidationOutcome) -> None:
        """Validator accepted (or was skipped). Move to operator review."""
        self._require_in({RunStateKind.VALIDATING}, "on_validator_pass")
        self.state = RunStateKind.AWAITING_REVIEW
        self.last_outcome = outcome

    def on_validator_fail(self, outcome: ValidationOutcome) -> Optional[str]:
        """Validator rejected.

        Returns the next state action signal:
          - ``"retry"`` — caller should re-spawn the LLM worker with
            validator feedback as the user-role message.
          - ``"give_up"`` — caller should fall back to operator review on
            the last response with a residual-errors banner.

        The decision is keyed on attempt-count; auto-retry-disabled is
        handled by the caller (it should call ``give_up_now`` directly
        instead of routing through this method when the toggle is off).
        """
        self._require_in({RunStateKind.VALIDATING}, "on_validator_fail")
        self.last_outcome = outcome
        if self.attempt < self.max_attempts and not self.cancelled:
            self.state = RunStateKind.VALIDATOR_FAIL_RETRYING
            self.attempt += 1
            return "retry"
        self.state = RunStateKind.FAILED_OUT_OF_RETRIES
        return "give_up"

    def begin_retry(self) -> None:
        """Caller has spawned the next LLM worker for the retry."""
        self._require_in({RunStateKind.VALIDATOR_FAIL_RETRYING}, "begin_retry")
        self.state = RunStateKind.LLM_REQUESTED

    def give_up_now(self, outcome: Optional[ValidationOutcome] = None) -> None:
        """Force-exit to FAILED_OUT_OF_RETRIES without checking attempt
        count. Used when the operator toggled auto-correct off, or when
        the validator was unavailable so retry would make no sense."""
        self._require_in({RunStateKind.VALIDATING}, "give_up_now")
        if outcome is not None:
            self.last_outcome = outcome
        self.state = RunStateKind.FAILED_OUT_OF_RETRIES

    def on_review_accepted(self) -> None:
        self._require_in({RunStateKind.AWAITING_REVIEW, RunStateKind.FAILED_OUT_OF_RETRIES}, "on_review_accepted")
        self.state = RunStateKind.APPLIED

    def on_review_rejected(self) -> None:
        self._require_in({RunStateKind.AWAITING_REVIEW, RunStateKind.FAILED_OUT_OF_RETRIES}, "on_review_rejected")
        self.state = RunStateKind.REJECTED

    def cancel(self) -> None:
        """Operator hit Cancel. Sets the flag; the FSM transitions to
        ``CANCELLED`` lazily — once the in-flight worker's response
        arrives the orchestrator checks ``cancelled`` and drops it."""
        self.cancelled = True
        # Direct terminal transition only when we're between turns
        # (LLM_REQUESTED) — otherwise the response handler will observe
        # ``cancelled`` and stop the loop.
        if self.state in {RunStateKind.LLM_REQUESTED, RunStateKind.VALIDATOR_FAIL_RETRYING}:
            self.state = RunStateKind.CANCELLED

    def reset_to_idle(self) -> None:
        """Clear back to IDLE after a terminal transition. Cheap to call
        repeatedly — no-op if already IDLE."""
        self.state = RunStateKind.IDLE
        self.task = None
        self.attempt = 0
        self.last_response = None
        self.last_outcome = None
        self.cancelled = False
        self.metadata = {}

    # ------------------------------------------------------------------ #
    # Predicates                                                          #
    # ------------------------------------------------------------------ #

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def is_running(self) -> bool:
        """True while an LLM worker is in flight or about to be."""
        return self.state in {
            RunStateKind.LLM_REQUESTED,
            RunStateKind.VALIDATOR_FAIL_RETRYING,
        }

    @property
    def attempts_remaining(self) -> int:
        """How many retries are still available after the current attempt."""
        return max(0, self.max_attempts - self.attempt)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    def _require_in(self, allowed: set, action: str) -> None:
        if self.state not in allowed:
            allowed_names = sorted(s.name for s in allowed)
            raise RuntimeError(
                f"Illegal FSM transition: {action!r} requires state in "
                f"{allowed_names}, but current state is {self.state.name}. "
                f"This is an orchestrator bug — file at the call-site."
            )
