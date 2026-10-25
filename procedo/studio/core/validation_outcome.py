"""The editor-facing validation outcome: :class:`ValidationOutcome` and its issues, and the
mapping from validator reports, parse errors and exceptions into it. One shape for every
consumer (the LLM feedback loop, the validator buttons, the structured-error dialog, the
validators registry). No GUI imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Outcome dataclass — one shape for all consumers                              #
# --------------------------------------------------------------------------- #


@dataclass
class ValidationIssueView:
    """Editor-facing issue. Detached from rules_packager_base types so the
    GUI never imports the validator package directly."""

    code: str
    message: str
    severity: str = "error"  # "error" | "warning"
    location: str = ""
    fix_hint: str = ""
    fixable_by: str = "either"  # "llm" | "operator" | "either"
    # Line in the LLM-authored fragment's coordinates once the text handler has
    # translated it (None if outside the LLM-owned region or not line-based); the
    # operator-facing full-document location stays in ``location``.
    line: Optional[int] = None

    def to_dock_dict(self) -> dict[str, str]:
        """Render to the dict shape ``dock_widget.show_validation_result_from_list``
        expects (see ``dock_widget.py:159``)."""
        return {
            "message": self.message,
            "severity": self.severity,
            "location": self.location,
            "code": self.code,
            "suggested_fix": self.fix_hint,
        }


@dataclass
class ValidationOutcome:
    """Result of running the deterministic validator on an LLM response or
    direct artifact input.

    The three states the GUI distinguishes:
      - ``skipped=True``  → validator unavailable / inapplicable; fall through
        to operator-only review.
      - ``ok=True, skipped=False`` → validator passed; safe to apply.
      - ``ok=False, skipped=False`` → validator rejected; ``issues`` carries
        the structured error list.
    """

    ok: bool = True
    skipped: bool = False
    reason: str = ""
    issues: list[ValidationIssueView] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def all_operator_only(self) -> bool:
        """True iff there is at least one error AND every error is tagged
        ``fixable_by="operator"``. Used by the auto-retry FSM to short-circuit
        the loop — if the LLM cannot fix any of the errors by design, retrying
        wastes turns and the operator must intervene instead."""
        errors = [i for i in self.issues if i.severity == "error"]
        return bool(errors) and all(i.fixable_by == "operator" for i in errors)


def render_validation_outcome_summary(outcome: "ValidationOutcome") -> str:
    """One-liner summary of a validator outcome for a status bar / dialog.

    Centralized so the menu action and the tab-level button render the
    same wording. Returns sentences like "Procedure validates clean.",
    "Procedure passes with 2 warning(s) — see Findings.", etc.
    """
    if outcome.skipped:
        return f"Validator skipped: {outcome.reason}"
    n_err = sum(1 for i in outcome.issues if i.severity == "error")
    n_warn = sum(1 for i in outcome.issues if i.severity == "warning")
    if outcome.ok and not outcome.issues:
        return "Procedure validates clean."
    if outcome.ok:
        return f"Procedure passes with {n_warn} warning(s) — see Findings."
    return f"Procedure has {n_err} error(s) and {n_warn} warning(s) — see Findings."


# --------------------------------------------------------------------------- #
# Issue mapping — rules_packager_base → editor-facing                         #
# --------------------------------------------------------------------------- #


def _format_location(loc: Any) -> str:
    """Squash the validator's ``location`` dict (file/line/pointer) into a
    one-liner for display. Tolerant of any shape."""
    if not loc:
        return ""
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        parts = []
        for key in ("file", "line", "column", "pointer"):
            if key in loc and loc[key] not in (None, ""):
                parts.append(f"{key}={loc[key]}")
        return " ".join(parts)
    return str(loc)


def issue_from_validator(issue: Any) -> ValidationIssueView:
    """Convert a finding emitted by the project's text_renderer (or a
    ``ParseError`` mapped through :func:`_parse_error_to_issue`) into the
    editor-facing view. Defensive against missing attributes — duck-typed
    against the renderer's Issue shape (code/message/severity/location/
    fix_hint/fixable_by)."""
    loc_raw = getattr(issue, "location", None)
    line = loc_raw.get("line") if isinstance(loc_raw, dict) else None
    return ValidationIssueView(
        code=getattr(issue, "code", "") or "",
        message=getattr(issue, "message", "") or "",
        severity=getattr(issue, "severity", "error") or "error",
        location=_format_location(loc_raw),
        fix_hint=getattr(issue, "fix_hint", "") or "",
        fixable_by=getattr(issue, "fixable_by", "either") or "either",
        line=line,
    )


def outcome_from_report(report: Any) -> ValidationOutcome:
    """Convert a ``ValidationReport`` to a :class:`ValidationOutcome`."""
    issues = [issue_from_validator(i) for i in getattr(report, "errors", [])]
    return ValidationOutcome(
        ok=bool(getattr(report, "ok", True)),
        skipped=False,
        issues=issues,
    )


def outcome_from_parse_error(exc: Any) -> ValidationOutcome:
    """Convert a ParseError-shaped exception into a
    :class:`ValidationOutcome`.

    Two cases:

    * The exception carries a non-empty ``.findings`` list (Phase 5.1
      ``engine_replies.ParseFailure``): every Finding becomes a row so
      the structured-error dialog shows all the wheel's reasons, not
      just the first. Without this fan-out the dialog renders one
      catch-all "[PARSE_ERROR] parsing failed" line and silently drops
      every secondary finding.
    * The exception has only ``.code`` / ``.message`` / ``.line`` (the
      legacy ``ParseError`` shape from the v2.0.0 wrapper): single
      catch-all issue is built from those attributes.
    """
    findings = getattr(exc, "findings", None) or []
    if findings:
        issues = [issue_from_validator(f) for f in findings]
        return ValidationOutcome(ok=False, skipped=False, issues=issues)
    issue = ValidationIssueView(
        code=getattr(exc, "code", "PARSE_ERROR") or "PARSE_ERROR",
        message=str(exc) if str(exc) else getattr(exc, "message", ""),
        severity="error",
        location=_format_location(
            {
                "line": getattr(exc, "line", None),
                "column": getattr(exc, "column", None),
            }
        ),
        fix_hint=getattr(exc, "fix_hint", "") or "",
    )
    return ValidationOutcome(ok=False, skipped=False, issues=[issue])


def skipped_outcome(reason: str) -> ValidationOutcome:
    return ValidationOutcome(ok=True, skipped=True, reason=reason)


def merge_outcomes(outcomes: list[ValidationOutcome]) -> ValidationOutcome:
    """Combine per-artifact outcomes into a single result.

    Semantics (least surprising for the FSM and chat banners):
      - All ``skipped`` → ``skipped`` (carries the first reason).
      - Any not-ok → not-ok (issues are union of all not-skipped outcomes).
      - Otherwise → ok with no issues.

    Skipped outcomes never contribute issues to the merged result — a
    "no inventory" skip on the code validator shouldn't show up as an
    error when the text + json validators both passed.
    """
    if not outcomes:
        return skipped_outcome("LLM did not propose any artifact")
    if all(o.skipped for o in outcomes):
        return outcomes[0]
    issues: list[ValidationIssueView] = []
    failed = False
    for o in outcomes:
        if o.skipped:
            continue
        issues.extend(o.issues)
        if not o.ok:
            failed = True
    return ValidationOutcome(ok=not failed, skipped=False, issues=issues)


def outcome_from_exception(exc: BaseException) -> ValidationOutcome:
    """Wrap any exception raised by a parser/codegen wrapper into a
    :class:`ValidationOutcome`. Used by the Quick Parse / Quick Code
    error paths so the structured-error dialog handles every failure
    mode the same way.

    Recognises ``ParseError`` (rules_packager_base) by duck-type — has
    ``code`` + ``fix_hint`` attributes. Falls back to a generic single
    issue with the exception class name as the code for unknown types
    (covers ``RuntimeError`` from the wrapper's missing-wheel path,
    ``ValueError`` / ``KeyError`` from codegen, etc.).
    """
    if hasattr(exc, "code") and hasattr(exc, "fix_hint"):
        return outcome_from_parse_error(exc)
    issue = ValidationIssueView(
        code=type(exc).__name__,
        message=str(exc) if str(exc) else repr(exc),
        severity="error",
        fix_hint="",
    )
    return ValidationOutcome(ok=False, skipped=False, issues=[issue])


# --------------------------------------------------------------------------- #
# Internal: faux ParseError for JSON-decode failures                          #
# --------------------------------------------------------------------------- #


class FauxParseError:
    """Minimal duck-type used when the LLM returned proposed JSON as a
    string that doesn't itself decode. Lets us reuse
    :func:`outcome_from_parse_error` without importing rules_packager_base
    types in this module."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        self.line = None
        self.column = None
        self.fix_hint = "Re-emit the procedure_json field as a valid JSON object."

    def __str__(self) -> str:
        return self.message
