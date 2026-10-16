"""Built-in validator registrations for Phase 2.

Wraps the two Phase-1 validators (``validate_current_state`` from
``validator_dispatch``, ``CodeValidator`` from
``core.validators``) and registers them under their canonical
namespaced ids:

* ``rules_packager_base.validate_procedure`` — full v2.0.x deterministic
  pipeline (R1 text↔JSON, R3 schema, R4 topology). Phase 5 lifts this
  physically into the rules_packager_base wheel; the id is stable.
* ``core.check_python_syntax`` — ``py_compile`` syntax check on
  ``test.py``. Grammar-agnostic, lives in the ``core.`` pseudo-pack so
  it stays available even when no rules pack is selected.

Importing this module is a no-op; registration runs only via
``register_builtins()`` which the registry's
``ensure_builtins_registered()`` calls lazily on first touch.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..core.validators import (
    CodeValidator,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)
from ..core.validators_registry import (
    ValidatorContext,
    register,
)
from .validator_dispatch import (
    ValidationIssueView,
    ValidationOutcome,
    validate_current_state,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapters: ValidationResult → ValidationOutcome
# ---------------------------------------------------------------------------


def _to_outcome(result: ValidationResult) -> ValidationOutcome:
    """Convert a core ``ValidationResult`` into the registry's
    canonical ``ValidationOutcome``."""
    return ValidationOutcome(
        ok=result.is_valid,
        skipped=False,
        issues=[_to_issue_view(i) for i in result.issues],
    )


def _to_issue_view(issue: ValidationIssue) -> ValidationIssueView:
    return ValidationIssueView(
        code=issue.code,
        message=issue.message,
        severity=_severity_str(issue.severity),
        location=issue.location,
    )


def _severity_str(severity: ValidationSeverity) -> str:
    """``ValidationSeverity`` is an enum; the outcome carries strings."""
    return severity.value if isinstance(severity, ValidationSeverity) else str(severity)


def _skipped(reason: str) -> ValidationOutcome:
    return ValidationOutcome(ok=True, skipped=True, reason=reason, issues=[])


# ---------------------------------------------------------------------------
# Validator wrappers
# ---------------------------------------------------------------------------


def _validate_procedure(ctx: ValidatorContext) -> ValidationOutcome:
    """Full v2.0.x deterministic pipeline. Delegates to
    ``validator_dispatch.validate_current_state`` which already returns
    a ``ValidationOutcome``."""
    return validate_current_state(
        project_root=ctx.project_root,
        text=ctx.artifact_text,
        json_str=ctx.artifact_json,
        code=ctx.artifact_code,
    )


def _check_python_syntax(ctx: ValidatorContext) -> ValidationOutcome:
    """``py_compile`` syntax check on the current code artifact."""
    if not ctx.artifact_code or not ctx.artifact_code.strip():
        return _skipped("No test.py content to check.")
    return _to_outcome(CodeValidator().validate(ctx.artifact_code))


# ---------------------------------------------------------------------------
# Registration entry point
# ---------------------------------------------------------------------------


_BUILTIN_REGISTRATIONS: Iterable[tuple[str, object]] = (
    ("rules_packager_base.validate_procedure",    _validate_procedure),
    ("core.check_python_syntax",                  _check_python_syntax),
)


def register_builtins() -> None:
    """Register every built-in validator. Called by the registry's lazy
    initialization; safe to call directly from tests."""
    for vid, fn in _BUILTIN_REGISTRATIONS:
        register(vid, fn)  # type: ignore[arg-type]
    log.debug("Registered %d built-in validators", len(_BUILTIN_REGISTRATIONS))
