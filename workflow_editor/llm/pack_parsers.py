"""Phase 5.1: direct wheel-side parser/validator helpers.

Replaces the project-local
``<project>/config/parsers/<kind>/<variant>.py`` wrapper layer. The
wrapper classes (`ProcedureTextParser`, `ProcedureCodeParser`,
`ProcedureTextRenderer`) only adapted the wheel's public API to a
specific method shape the editor invented; they carried no
project-specific logic. With three operations exposed by the wheel
(``parse``, ``render``, ``validate_schema``, ``check_name_fidelity``,
``codegen.generate``), nothing is gained by routing through a
per-project Python file — but plenty is lost when the wheel's API
moves (e.g. the v2.0.0 ``bijective_validator`` → v2.0.1
``rules.v2_0_1.parser`` rename silently breaks every wrapper).

This module imports from the wheel directly. The editor's Quick
Parse / Quick Code buttons and the deterministic-validator dispatch
all call into here. Per-project parser variants are no longer a
concept — the active rules pack ships exactly one parser version,
and the wheel is the single source of truth.

When the wheel is missing or pre-2.0.1, every entry point raises a
``ParserUnavailable`` with a uniform "reinstall the wheel" message;
callers surface it as a tooltip / status / chat-panel echo.

Eager imports would drag rules_packager_base into every editor
startup; lazy imports keep the editor robust against a stale or
missing wheel (the LLM workflow still works).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# Wheel version we expect (and that the v2.0.1 grammar requires).
_REQUIRED_WHEEL = "rules_packager_base.rules.v2_0_1.parser"


class ParserUnavailable(RuntimeError):
    """Raised when the deterministic parser/codegen path can't run.

    Carries a uniform message so callers (chat-panel echo, tooltip,
    button-hide logic) can show one error to the operator: "reinstall
    the wheel". Distinct from a ParseError (which carries grammar-level
    failure detail and goes to the structured-error dialog).
    """


# ---------------------------------------------------------------------------
# Wheel-import helpers (lazy, cached)
# ---------------------------------------------------------------------------


def _import_wheel():
    """Import the v2.0.1 parser module; cache the module object.

    Returns the module. Raises :class:`ParserUnavailable` with a fix
    hint if the wheel isn't installed / is too old.
    """
    try:
        import rules_packager_base.rules.v2_0_1.parser as _parser
        return _parser
    except ImportError as exc:
        raise ParserUnavailable(
            f"{_REQUIRED_WHEEL} is not importable: {exc}. "
            f"Reinstall the rules_packager_base wheel (>= 2.0.1) into "
            f"the venv running the workflow editor. The LLM fallback "
            f"remains available."
        ) from exc


def is_available() -> tuple[bool, str]:
    """Probe whether the wheel imports cleanly. Returns ``(available, reason)``.

    Used by ``validator_dispatch.is_loop_available`` and the editor's
    chat-panel validator-status indicator. Cheap — just a single
    ``import``. Doesn't touch the project's config dir at all.
    """
    try:
        _import_wheel()
        return True, "deterministic path active"
    except ParserUnavailable as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Quick Parse — canonical text → procedure JSON
# ---------------------------------------------------------------------------


def parse_text(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse canonical-text procedure into the v2.0.1 JSON shape.

    Returns ``(procedure_json, warnings)``:
      - ``procedure_json`` — the parsed dict.
      - ``warnings`` — list of human-readable strings from non-error
        findings (the wheel reports `severity="warning"` findings here).

    Raises:
      :class:`ParserUnavailable` if the wheel isn't importable.
      ``ParseFailure`` (a generic Exception with .findings) if the
        wheel produced error-severity findings — callers route this to
        ``ValidatorErrorDialog`` for structured rendering. The
        exception type carries ``code`` and ``fix_hint`` attributes for
        compatibility with the legacy ParseError shape.
    """
    wheel = _import_wheel()
    result = wheel.parse(text)
    if not result.success or result.json is None:
        errors = result.errors if hasattr(result, "errors") else [
            f for f in result.findings if f.severity == "error"
        ]
        raise _make_parse_failure(errors, result.findings)
    warnings = [
        _format_finding(f) for f in result.findings if f.severity == "warning"
    ]
    return result.json, warnings


def render_text(procedure_json: dict[str, Any]) -> str:
    """Emit canonical-text procedure from the v2.0.1 JSON shape."""
    wheel = _import_wheel()
    return wheel.render(procedure_json)


# ---------------------------------------------------------------------------
# Quick Code — procedure JSON → Python test code
# ---------------------------------------------------------------------------


def generate_code(
    procedure: dict[str, Any],
    project_root: Optional[Path],
) -> tuple[str, list[str]]:
    """Generate test.py source from a procedure JSON dict.

    Phase 7a (2026-05-12): the legacy ``<project>/inventory.json``
    fallback is gone. Per the v2.0.x design, bench-identification
    fields (visa, port, baud, timeout, remote) live as module
    constants in the generated test.py and are preserved across
    regeneration. Quick Code generates fresh code with codegen
    defaults; the operator then edits those defaults via the
    Equipment Editor and the sync mechanism skips them from drift
    detection. There is no longer a sidecar inventory file.

    Returns ``(code, warnings)``. Raises :class:`ParserUnavailable` if
    the wheel isn't importable.
    """
    del project_root  # no inventory file to consult; left for API symmetry
    wheel_codegen = _import_codegen()
    code = wheel_codegen.generate(procedure, None)
    return code, []


def _import_codegen():
    """Import the v2.0.1 codegen submodule; raise ParserUnavailable on
    failure. Codegen lives at ``rules_packager_base.rules.v2_0_1.parser.codegen``,
    NOT at the legacy ``rules_packager_base.bijective_validator.codegen``
    (which Codex flagged as not present in the current wheel)."""
    try:
        from rules_packager_base.rules.v2_0_1.parser import codegen as _codegen
        return _codegen
    except ImportError as exc:
        raise ParserUnavailable(
            f"rules_packager_base.rules.v2_0_1.parser.codegen is not "
            f"importable: {exc}. Reinstall the rules_packager_base wheel "
            f"(>= 2.0.1) into the venv running the workflow editor. The "
            f"LLM fallback remains available."
        ) from exc


# ---------------------------------------------------------------------------
# Full validation — used by validators_builtin.validate_procedure
# ---------------------------------------------------------------------------


def validate(
    *,
    text: Optional[str] = None,
    json_obj: Optional[dict[str, Any]] = None,
    mode: str = "all",
    original_text: Optional[str] = None,
    check_names: bool = True,
) -> "_ValidateReport":
    """Run the full deterministic validation pipeline.

    Composes:
      - ``parse(text)`` — grammar parse + lex (when text is provided).
      - ``validate_schema(json_obj)`` — schema + topology + semantic.
      - ``check_name_fidelity(text, original_text)`` — catch the LLM
        stripping `+HIGH_28V` → `HIGH_28V` etc. (when both text and
        original_text are provided AND check_names=True).

    Returns a ``_ValidateReport`` duck-typed against the legacy
    ProcedureTextRenderer.validate() shape so
    ``validator_dispatch._outcome_from_report`` can consume it
    unchanged. Raises :class:`ParserUnavailable` if the wheel isn't
    importable.
    """
    wheel = _import_wheel()
    findings: list[Any] = []

    parsed_json = json_obj
    if text is not None:
        result = wheel.parse(text)
        findings.extend(result.findings)
        if result.success and result.json is not None:
            parsed_json = parsed_json or result.json

    if parsed_json is not None and mode in ("all", "schema"):
        findings.extend(wheel.validate_schema(parsed_json))

    if text is not None and original_text and check_names:
        findings.extend(wheel.check_name_fidelity(original_text, text))

    return _ValidateReport(findings)


class _Issue:
    """Editor-facing adapter over a wheel ``Finding``. Mirrors the
    legacy canonical.py wrapper's _Issue shape so
    ``validator_dispatch._issue_from_validator`` can consume it:

      - ``.location`` is a dict ``{line, column}`` (legacy shape) so
        ``_format_location`` renders it as ``"line=X column=Y"``.
        Raw Findings carry ``.line``/``.col`` attributes which the
        validator_dispatch issue-builder doesn't know how to read.
    """

    def __init__(self, finding: Any) -> None:
        self.code = getattr(finding, "code", "") or ""
        self.message = getattr(finding, "message", "") or ""
        self.severity = getattr(finding, "severity", "error") or "error"
        line = getattr(finding, "line", 0)
        col = getattr(finding, "col", 0)
        loc: dict[str, Any] = {}
        if line:
            loc["line"] = line
        if col:
            loc["column"] = col
        self.location = loc
        self.fix_hint = getattr(finding, "fix_hint", "") or ""
        self.fixable_by = getattr(finding, "fixable_by", "either") or "either"


class _ValidateReport:
    """Duck-typed report shape that mirrors the legacy
    ProcedureTextRenderer.validate() return:
      - ``.ok: bool`` — True iff no error-severity finding.
      - ``.errors: list[_Issue]`` — error-severity only.
      - ``.warnings: list[_Issue]`` — warning-severity (validate_current_state
        surfaces these alongside errors for the operator-facing panel).
    """

    def __init__(self, findings: list[Any]) -> None:
        # Adapt every Finding to the editor-facing _Issue shape.
        self._issues = [_Issue(f) for f in findings]
        # Mirror the legacy report's coarse "ok" — block iff any error.
        self.ok = not any(i.severity == "error" for i in self._issues)
        self.errors = [i for i in self._issues if i.severity == "error"]
        self.warnings = [i for i in self._issues if i.severity == "warning"]


# ---------------------------------------------------------------------------
# Internal: ParseFailure construction
# ---------------------------------------------------------------------------


def _make_parse_failure(errors: list[Any], findings: list[Any]) -> "ParseFailure":
    """Build a ParseFailure carrying the first error's code+fix_hint
    plus the full findings list. Mirrors the legacy ParseError shape
    so ValidatorErrorDialog.show_from_exception treats it identically.

    ``findings`` is pre-wrapped in :class:`_Issue` so every entry has
    the editor-facing shape (`.location` as a dict, `.fixable_by`,
    etc.) — ``validator_dispatch._outcome_from_parse_error`` fans them
    out into a multi-row ValidationOutcome so every wheel finding
    surfaces in the structured-error dialog, not just the primary.
    """
    primary = errors[0] if errors else None
    code = getattr(primary, "code", "PARSE_ERROR") if primary else "PARSE_ERROR"
    line = getattr(primary, "line", None) if primary else None
    col = getattr(primary, "col", None) if primary else None
    fix_hint = getattr(primary, "fix_hint", "") if primary else ""
    msg = str(primary) if primary else "Parsing failed."
    exc = ParseFailure(msg)
    exc.code = code
    exc.line = line
    exc.column = col
    exc.fix_hint = fix_hint
    exc.findings = [_Issue(f) for f in findings]
    return exc


class ParseFailure(Exception):
    """Raised by :func:`parse_text` when the wheel's parser produced
    error-severity findings. Duck-typed against the legacy ParseError
    (carries ``.code``, ``.line``, ``.column``, ``.fix_hint``,
    ``.findings``)."""


def _format_finding(finding: Any) -> str:
    """One-line human-readable string for a Finding. Used for the
    warnings list returned by :func:`parse_text`."""
    code = getattr(finding, "code", "?")
    line = getattr(finding, "line", 0)
    msg = getattr(finding, "message", str(finding))
    return f"line {line} [{code}] {msg}"


# ---------------------------------------------------------------------------
# Step-text extraction from canonical procedure_text.md
# ---------------------------------------------------------------------------


import re as _step_re

_STEP_TEXTS_LINE_RE = _step_re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", _step_re.MULTILINE)
_STEPS_SECTION_RE = _step_re.compile(
    r"^##\s+Steps\s*$(.*?)(?=^##\s+|\Z)",
    _step_re.MULTILINE | _step_re.DOTALL,
)


def step_texts_from_canonical(procedure_text: str) -> dict[int, str]:
    """Return ``{step_number: canonical_line}`` from a procedure_text.md.

    Scans the ``## Steps`` section for lines matching ``<n>. <text>.``
    and returns one entry per step. Used by the workflow editor's
    JSON-Code + Traceability tabs to surface the operator-readable
    step description even when the JSON step is an op-call shape
    (``{"op": "psu.set_voltage", "device": "PSU1", ...}``) carrying no
    top-level ``text`` field.

    Returns an empty dict when no Steps section is present or no
    numbered lines are found — callers fall back to ``step.get("text")``
    or a placeholder.
    """
    if not procedure_text:
        return {}
    section_match = _STEPS_SECTION_RE.search(procedure_text)
    if section_match is None:
        return {}
    body = section_match.group(1)
    out: dict[int, str] = {}
    for m in _STEP_TEXTS_LINE_RE.finditer(body):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        # Strip the trailing period that canonical-text mandates.
        text = m.group(2).rstrip(".").rstrip()
        out[n] = text
    return out
