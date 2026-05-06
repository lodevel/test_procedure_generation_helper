"""Validator dispatch for the LLM-with-feedback loop.

Single point of integration with ``rules_packager_base.bijective_validator``.
Lazy imports keep GUI startup robust against stale wheels — if the
deterministic path is unavailable, the dispatcher returns ``skipped=True``
and the workflow editor falls back to LLM-only review.

The same outcome shape is consumed by:
  - ``LLMTabMixin._handle_llm_response`` to gate / loop the LLM call;
  - ``validator_error_dialog`` to render structured errors when the
    Quick-Parse / Quick-Code button surfaces a failure directly.

**Routing model — artifact-shape dispatch (Phase 3, 2026-04-27):** the
dispatch is keyed on which proposal slots the LLM populated in the
``LLMResponse`` (``procedure_text`` / ``procedure_json`` / ``test_code``),
not on which ``LLMTask`` button fired. This makes the loop universal:

  - Standard buttons (Text→JSON, JSON→Code, …) already populate one slot;
    the matching validator runs.
  - **Chat-panel ad-hoc messages** that produce a proposal (e.g. "regen
    the JSON with PSU2 added") now get validated too — they were no-ops
    under the previous LLMTask-keyed dispatch.
  - **Custom tasks** are task-agnostic — the validator follows the
    artifact, not the button label.
  - Multi-slot responses run multiple validators; the outcome is OK iff
    every applicable check passes.
  - Responses with no proposal (review-style chat) return ``skipped``
    automatically.

This module owns:
  - The :class:`ValidationOutcome` dataclass — the editor-facing result.
  - :func:`validate_response` — the entry point the mixin calls.
  - :func:`format_validator_feedback` — renders an outcome as the user-role
    follow-up message used to drive the auto-retry loop.
  - :func:`is_loop_available` — coarse "is the deterministic path loaded?"
    check used to gate the operator-facing toggle.

No GUI imports here — consumers map ``ValidationOutcome.issues`` onto
their own widget shapes (e.g. the dock-widget findings panel's dict
shape is constructed by the mixin, not by this module).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .backend_base import LLMResponse

log = logging.getLogger(__name__)


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
    return (
        f"Procedure has {n_err} error(s) and {n_warn} warning(s) — "
        f"see Findings."
    )


# --------------------------------------------------------------------------- #
# Inventory loader (mtime cache)                                              #
# --------------------------------------------------------------------------- #


_INVENTORY_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}


def _resolve_inventory(
    json_obj: dict[str, Any],
    project_root: Optional[Path],
    code: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Resolve the inventory codegen needs for the given procedure_json.

    Per the v2.0.x design: bench identification (visa, port, baud,
    timeout, remote, manual_override) lives in the generated test.py
    module constants. This helper assembles the inventory dict by
    AST-extracting those constants from ``code`` (used for forward-only
    codegen; see ``12_Codegen_Specification_v2.md``).

    Resolution order:
      1. If ``code`` carries operator-pinned constants, build the
         inventory from them. This is the v2.0.x path.
      2. Otherwise fall back to legacy ``<project>/inventory.json``
         for projects pre-dating the v2.0.x design.
      3. If neither is available, return None.
    """
    inv = _inventory_from_code(json_obj, code)
    if inv is not None:
        return inv
    if project_root is not None:
        return _load_inventory(project_root)
    return None


def _inventory_from_code(
    json_obj: dict[str, Any],
    code: Optional[str],
) -> Optional[dict[str, Any]]:
    """Build an inventory dict from ``code``'s module-level bench constants.

    Returns None when no equipment ids match any constant in code (e.g.
    code is empty, or all constants are non-literal). The returned dict
    is the shape codegen expects: ``{format_version, instruments: {<id>:
    {type, ...bench_fields}}}`` with the per-equipment type pulled from
    ``json_obj.equipment[]`` and the bench fields decoded from the
    constant suffixes (``<ID>_VISA`` → ``visa``, etc.).
    """
    if not code or not code.strip():
        return None
    from .code_constants_merge import (
        equipment_ids_from_procedure,
        extract_pinned_constants,
    )
    equipment = json_obj.get("equipment") or []
    if not isinstance(equipment, list):
        return None
    eq_ids = equipment_ids_from_procedure(json_obj)
    if not eq_ids:
        return None
    pinned = extract_pinned_constants(code, eq_ids)
    if not pinned:
        return None

    # Map each suffix to its inventory-side field name. Mirrors the
    # codegen substitution table in `12_Json_Code_Bijection_v2.md` §3.
    suffix_to_field = {
        "_VISA": "visa",
        "_CHANNEL": "channel",
        "_TIMEOUT_MS": "timeout_ms",
        "_TIMEOUT_S": "timeout_s",
        "_REMOTE": "remote",
        "_PORT": "port",
        "_BAUD": "baud",
        "_MANUAL_OVERRIDE": "manual_override",
    }

    type_by_id = {
        e["id"]: e.get("type") for e in equipment
        if isinstance(e, dict) and "id" in e
    }

    instruments: dict[str, Any] = {}
    for eq_id in eq_ids:
        slot: dict[str, Any] = {}
        eq_type = type_by_id.get(eq_id)
        if eq_type:
            slot["type"] = eq_type
        if eq_type == "controller":
            # subtype lives only in procedure.json; carry it through so
            # the inventory schema validates downstream.
            for e in equipment:
                if isinstance(e, dict) and e.get("id") == eq_id and e.get("subtype"):
                    slot["subtype"] = e["subtype"]
                    break
        for suffix, field in suffix_to_field.items():
            const_name = eq_id + suffix
            if const_name in pinned:
                slot[field] = pinned[const_name]
        instruments[eq_id] = slot

    return {"format_version": "2.0.0", "instruments": instruments}


def _load_inventory(project_root: Path) -> Optional[dict[str, Any]]:
    """Read ``<project_root>/inventory.json`` with an mtime-keyed cache.

    Returns ``None`` if the file is missing — JSON↔code validation needs an
    inventory; absence is treated by the caller as "skipped" rather than
    "failed" so a partly-set-up project keeps working.
    """
    path = project_root / "inventory.json"
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _INVENTORY_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("inventory.json at %s failed to load: %s", path, exc)
        return None
    _INVENTORY_CACHE[path] = (mtime, data)
    return data


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


def _issue_from_validator(issue: Any) -> ValidationIssueView:
    """Convert a ``rules_packager_base.bijective_validator.types.Issue``
    (or a ``ParseError`` mapped through :func:`_parse_error_to_issue`) to
    the editor-facing view. Defensive against missing attributes — the
    validator's Issue type is dataclass-shaped but we don't import it."""
    return ValidationIssueView(
        code=getattr(issue, "code", "") or "",
        message=getattr(issue, "message", "") or "",
        severity=getattr(issue, "severity", "error") or "error",
        location=_format_location(getattr(issue, "location", None)),
        fix_hint=getattr(issue, "fix_hint", "") or "",
        fixable_by=getattr(issue, "fixable_by", "either") or "either",
    )


def _outcome_from_report(report: Any) -> ValidationOutcome:
    """Convert a ``ValidationReport`` to a :class:`ValidationOutcome`."""
    issues = [_issue_from_validator(i) for i in getattr(report, "errors", [])]
    return ValidationOutcome(
        ok=bool(getattr(report, "ok", True)),
        skipped=False,
        issues=issues,
    )


def _outcome_from_parse_error(exc: Any) -> ValidationOutcome:
    """Convert a ``ParseError`` raised by ``text_parser.parse`` to an
    :class:`ValidationOutcome` carrying a single issue."""
    issue = ValidationIssueView(
        code=getattr(exc, "code", "PARSE_ERROR") or "PARSE_ERROR",
        message=str(exc) if str(exc) else getattr(exc, "message", ""),
        severity="error",
        location=_format_location({
            "line": getattr(exc, "line", None),
            "column": getattr(exc, "column", None),
        }),
        fix_hint=getattr(exc, "fix_hint", "") or "",
    )
    return ValidationOutcome(ok=False, skipped=False, issues=[issue])


def _skipped(reason: str) -> ValidationOutcome:
    return ValidationOutcome(ok=True, skipped=True, reason=reason)


# --------------------------------------------------------------------------- #
# Lazy-imported validator entry points                                        #
# --------------------------------------------------------------------------- #


def _import_validator():
    """Import ``rules_packager_base.bijective_validator`` on demand.

    Returns ``(validate_fn, deterministic_path_available_fn)`` or
    ``(None, None)`` if the package is unavailable.
    """
    try:
        from rules_packager_base.bijective_validator import (  # type: ignore[import-not-found]
            validate as _validate,
            deterministic_path_available as _avail,
        )
        return _validate, _avail
    except Exception as exc:  # pragma: no cover — exercised only on stale wheels
        log.info("bijective validator not importable (%s); GUI will fall back to LLM-only.", exc)
        return None, None


# --------------------------------------------------------------------------- #
# Artifact-shape dispatch handlers                                            #
# --------------------------------------------------------------------------- #
#
# One handler per artifact slot the LLM might propose. Each handler
# returns:
#   - None when the slot wasn't proposed (not applicable; dispatcher
#     filters these so the outcome reflects only relevant checks).
#   - A ValidationOutcome when applicable. The handler decides which
#     validator(s) to run based on what other current artifacts the
#     editor has — e.g. a proposed text alone runs the parse + R3 + R4
#     trip; a proposed text plus a current json runs the full R1 pair.
#
# Multi-slot responses (a single LLM turn proposing both text AND json,
# say) get every applicable handler run; outcomes are merged by
# :func:`_merge_outcomes` so the FSM and the chat banners see a single
# combined result.
#
# Handlers MUST be permissive: missing optional inputs (no current
# artifact, no inventory) collapse to ``_skipped`` rather than crashing.
# That keeps a partly-filled editor working — e.g. validating a proposed
# text against a not-yet-authored json simply parses the text alone.

CurrentArtifacts = dict[str, Optional[str]]
"""Snapshot of the editor's three artifact texts: keys ``text``, ``json``,
``code``. Values are the current contents (raw strings) or None if the
artifact slot is empty / not yet authored."""


def _proposal_content(response: LLMResponse, attr: str) -> Optional[Any]:
    """Pull the LLMProposal content for ``attr`` ∈ {procedure_text,
    procedure_json, test_code}. Returns the proposal's ``.content`` (a str
    or dict depending on artifact) or None when the LLM didn't propose
    that artifact this turn."""
    proposal = getattr(response, attr, None)
    if proposal is None or not getattr(proposal, "mode", None):
        return None
    return getattr(proposal, "content", None)


def _resolve_json_for_crosscheck(
    response: LLMResponse, current: CurrentArtifacts,
) -> Optional[Any]:
    """Pick the JSON object the validator should cross-check a text or
    code proposal against — **proposed-side only, never current**.

    The LLM's auto-correction loop is about the LLM's self-consistency:
    "given this turn's proposed artifacts, do they validate as a
    coherent set?" Cross-checking the proposed text against the editor's
    *current* json (which the operator hasn't accepted yet — typically a
    stale legacy artifact early in a migration) drowns the panel in
    issues from state the LLM has nothing to do with, and prevents the
    retry loop from converging when the editor's pre-loop state is
    malformed.

    Returns ``None`` when the LLM didn't propose json this turn; callers
    then validate the side they have alone (parse + R3 + R4 on the text;
    R3 + R4 on the json), which is the right semantic for one-slot
    responses.

    The ``current`` parameter is kept on the signature for symmetry with
    the other resolvers and for forward-compatibility with a possible
    "operator wants to validate against current state" mode, but is not
    consulted today.
    """
    del current  # intentionally unused, see docstring
    proposed = _proposal_content(response, "procedure_json")
    if proposed is None:
        return None
    if isinstance(proposed, str):
        try:
            return json.loads(proposed)
        except json.JSONDecodeError:
            return None  # malformed — _validate_proposed_json reports it
    return proposed


def _resolve_text_for_crosscheck(
    response: LLMResponse, current: CurrentArtifacts,
) -> Optional[str]:
    """Pick the text the validator should cross-check a json proposal
    against — **proposed-side only**. See
    :func:`_resolve_json_for_crosscheck` for the rationale; same
    semantics, text side.
    """
    del current  # intentionally unused, see _resolve_json_for_crosscheck
    proposed = _proposal_content(response, "procedure_text")
    return str(proposed) if proposed is not None else None


def _resolve_code_for_crosscheck(
    response: LLMResponse, current: CurrentArtifacts,
) -> Optional[str]:
    """Pick the code the validator should cross-check a json proposal
    against — **proposed-side only**. Symmetric to the text/json
    resolvers.
    """
    del current  # intentionally unused, see _resolve_json_for_crosscheck
    proposed = _proposal_content(response, "test_code")
    return str(proposed) if proposed is not None else None


def _validate_proposed_text(
    response: LLMResponse,
    current: CurrentArtifacts,
    project_root: Path,
    validate_fn: Callable[..., Any],
) -> Optional[ValidationOutcome]:
    """The LLM proposed a ``procedure_text`` update. Run R1 against the
    current/proposed json (whichever is in scope); if no json is
    available, fall back to text-alone parse+R3+R4 via the validator's
    text-only path.
    """
    proposed_text = _proposal_content(response, "procedure_text")
    if proposed_text is None:
        return None
    json_obj = _resolve_json_for_crosscheck(response, current)
    report = validate_fn(text=str(proposed_text), json_obj=json_obj, mode="all")
    return _outcome_from_report(report)


def _validate_proposed_json(
    response: LLMResponse,
    current: CurrentArtifacts,
    project_root: Path,
    validate_fn: Callable[..., Any],
) -> Optional[ValidationOutcome]:
    """The LLM proposed a ``procedure_json`` update. Always runs
    R3 (schema) + R4 (topology) + R5 (semantic) on the proposed JSON;
    cross-checks with current text (R1) when available.

    Phase 9.4: R2 (JSON↔code) is no longer dispatched; ``code``/``inventory``
    are accepted but unused.
    """
    proposed_json = _proposal_content(response, "procedure_json")
    if proposed_json is None:
        return None
    if isinstance(proposed_json, str):
        try:
            proposed_json = json.loads(proposed_json)
        except json.JSONDecodeError as exc:
            return _outcome_from_parse_error(
                _FauxParseError("JS_BODY_INVALID", str(exc))
            )
    # Cross-check sides: prefer proposed text when the LLM proposed it in
    # the same turn — otherwise the validator compares the new json
    # against the operator's stale (likely legacy) editor state and the
    # retry loop never converges.
    text = _resolve_text_for_crosscheck(response, current)
    kwargs: dict[str, Any] = {"json_obj": proposed_json, "mode": "all"}
    if text:
        kwargs["text"] = text
    report = validate_fn(**kwargs)
    return _outcome_from_report(report)


def _validate_proposed_code(
    response: LLMResponse,
    current: CurrentArtifacts,
    project_root: Path,
    validate_fn: Callable[..., Any],
) -> Optional[ValidationOutcome]:
    """The LLM proposed a ``test_code`` update.

    Phase 9.4: R2 (JSON↔code) bijection was removed when the deterministic
    code inspector was deleted. Codegen is forward-only; code edits flow
    back to JSON via the LLM workflow (no deterministic guarantee). For
    now, we still run schema + topology against any in-scope JSON so the
    operator gets warnings about JSON drift, but we no longer round-trip
    the proposed code through an inspector.

    Future: replace with a "manual code → JSON via LLM" pass — out of
    scope for Phase 9.4.
    """
    proposed_code = _proposal_content(response, "test_code")
    if proposed_code is None:
        return None
    json_obj = _resolve_json_for_crosscheck(response, current)
    if json_obj is None:
        return _skipped(
            "no procedure_json available — cannot validate proposed code"
        )
    report = validate_fn(json_obj=json_obj, mode="all")
    return _outcome_from_report(report)


_ARTIFACT_HANDLERS: tuple[Callable[..., Optional[ValidationOutcome]], ...] = (
    _validate_proposed_text,
    _validate_proposed_json,
    _validate_proposed_code,
)


# --------------------------------------------------------------------------- #
# Phase 7.1 (2026-04-30) — operator-only field auto-restore                   #
# --------------------------------------------------------------------------- #
#
# When the LLM populates or modifies `meta.requirement` (an operator-supplied
# field per Doc 02 §2.1 + Doc 12 §2.1.4), we deterministically restore the
# baseline value INSIDE the response BEFORE the artifact handlers run. This
# bypasses the costly retry round-trip that the prior `META_REQUIREMENT_CHANGED`
# error path triggered. The operator sees a single informational warning issue
# (`META_REQUIREMENT_AUTO_RESTORED`) so they know the LLM tried to invent
# something and the system corrected it.

# Operator-only meta fields handled by the auto-restore. Currently scoped to
# `requirement` per Phase 7.1; extend by adding to this set.
_OPERATOR_ONLY_META_FIELDS: frozenset[str] = frozenset({"requirement"})


import re as _re

# Match the `requirement:` line in canonical-text Meta block. The whole
# line is captured so we can string-replace it. Tolerant of (a) bare key
# with no value (`requirement:`), (b) `requirement: <value>`, and trailing
# whitespace differences. Anchored to start-of-line so it doesn't
# accidentally match `requirement:` inside step prose.
_META_REQUIREMENT_LINE_RE = _re.compile(
    r"^requirement:(?:[ \t]+([^\n]*?))?[ \t]*$", _re.MULTILINE
)


def _extract_meta_requirement_from_text(text: str) -> str:
    """Pull the `meta.requirement` value from a canonical-text procedure.

    Returns the value string ("" if the line is absent or empty). Does
    not full-parse the procedure — just regex-scans for the
    ``requirement:`` line. Phase 7.1 design: avoids dependency on the
    full parser so the auto-restore works against any version of the
    rules_packager_base wheel.
    """
    if not text:
        return ""
    m = _META_REQUIREMENT_LINE_RE.search(text)
    if m is None:
        return ""
    return (m.group(1) or "").strip()


def _auto_restore_operator_only_fields(
    response: LLMResponse,
    current_artifacts: dict[str, str],
) -> list[ValidationIssueView]:
    """Phase 7.1 — restore baseline values for operator-only meta fields
    before the artifact handlers run.

    Reads the pre-LLM baseline from ``current_artifacts["text"]``. If the
    baseline is empty / None, this is a no-op — no baseline means no
    comparison.

    For each operator-only field in :data:`_OPERATOR_ONLY_META_FIELDS`,
    if the response's value differs from the baseline:
      * Mutates ``response.procedure_text.content`` (when proposed) to
        rewrite the ``requirement:`` line with the baseline value.
      * Mutates ``response.procedure_json.content`` (when proposed) to
        rewrite ``meta[<field>]`` with the baseline value.

    Returns a list of informational :class:`ValidationIssueView` (severity
    ``warning``) describing each restoration.

    Multi-slot consistency rule: when the LLM proposed BOTH text and JSON
    on the same turn, both proposals are restored to the same baseline
    value verbatim — preserving the proposal-pair coherence the LLM
    intended to ship.

    The implementation does NOT call the full text_parser/text_emitter to
    avoid a stale-wheel dependency: a regex line-replace on the
    `requirement:` line is sufficient and works regardless of which
    rules_packager_base version is installed.
    """
    issues: list[ValidationIssueView] = []
    baseline_text = current_artifacts.get("text") if current_artifacts else None
    if not baseline_text:
        return issues  # no-op when no baseline available

    # Currently the only operator-only field with a known auto-restore
    # rule is `requirement`; loop here keeps the structure ready for
    # future additions (revision, authored_by, authored_date) without
    # restructuring the dispatch flow.
    for field_name in _OPERATOR_ONLY_META_FIELDS:
        if field_name != "requirement":
            continue  # add per-field handlers as the set grows
        baseline_value = _extract_meta_requirement_from_text(baseline_text)
        text_change = _restore_requirement_in_text_proposal(response, baseline_value)
        json_change = _restore_requirement_in_json_proposal(response, baseline_value)
        if text_change or json_change:
            from_value = (text_change or json_change)[0]
            issues.append(ValidationIssueView(
                code="META_REQUIREMENT_AUTO_RESTORED",
                severity="warning",
                message=(
                    f"The LLM populated or modified the operator-only field "
                    f"`meta.{field_name}` (baseline: {baseline_value!r}, "
                    f"response: {from_value!r}). Auto-restored to baseline "
                    f"value before applying. Per Doc 02 §2.1 and Doc 12 "
                    f"§2.1.4, this slot is operator-supplied only — the LLM "
                    f"cannot infer a value and must NOT use it as a freeform "
                    f"dumping ground for inferred prose."
                ),
                location="/meta/" + field_name,
                fix_hint=(
                    "No action needed — the system corrected this "
                    "automatically. If the LLM keeps inventing values here, "
                    "review the rule docs (Doc 12 §2.1.4) shown to the model."
                ),
            ))
    return issues


def _restore_requirement_in_text_proposal(
    response: LLMResponse,
    baseline_value: str,
) -> Optional[tuple[str, str]]:
    """Mutate `response.procedure_text.content` to restore baseline.

    Returns ``(from_value, to_value)`` if a change was applied, ``None``
    if no change was needed or no text proposal exists. Uses a regex
    line-replace on the `requirement:` line — no full-procedure parse
    needed.
    """
    proposal = getattr(response, "procedure_text", None)
    if proposal is None or not getattr(proposal, "is_valid", False):
        return None
    content = proposal.content
    if not isinstance(content, str):
        return None
    proposed_value = _extract_meta_requirement_from_text(content)
    if proposed_value == baseline_value:
        return None
    # Build the canonical replacement line.
    new_line = "requirement:" if not baseline_value else f"requirement: {baseline_value}"
    if _META_REQUIREMENT_LINE_RE.search(content):
        new_content = _META_REQUIREMENT_LINE_RE.sub(new_line, content, count=1)
    else:
        # Line was absent in proposal — but proposed_value would have been
        # "" so we'd have early-returned above when baseline_value also "".
        # If we get here, baseline_value is non-empty and proposal lacks
        # the line entirely. Skip mutation; let the handlers flag it.
        return None
    proposal.content = new_content
    return (proposed_value, baseline_value)


def _restore_requirement_in_json_proposal(
    response: LLMResponse,
    baseline_value: str,
) -> Optional[tuple[str, str]]:
    """Mutate `response.procedure_json.content` to restore baseline."""
    proposal = getattr(response, "procedure_json", None)
    if proposal is None or not getattr(proposal, "is_valid", False):
        return None
    content = proposal.content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except Exception:
            return None
        re_emit_str = True
    elif isinstance(content, dict):
        parsed = content
        re_emit_str = False
    else:
        return None
    proposed_meta = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
    proposed_value = (proposed_meta.get("requirement") or "") if isinstance(proposed_meta, dict) else ""
    if proposed_value == baseline_value:
        return None
    new_meta = dict(proposed_meta) if isinstance(proposed_meta, dict) else {}
    if baseline_value:
        new_meta["requirement"] = baseline_value
    else:
        new_meta.pop("requirement", None)
    parsed["meta"] = new_meta
    if re_emit_str:
        try:
            proposal.content = json.dumps(parsed, indent=2)
        except Exception as exc:
            log.warning("auto-restore: json re-emit crashed: %s", exc)
            return None
    else:
        proposal.content = parsed
    return (proposed_value, baseline_value)


def _merge_outcomes(outcomes: list[ValidationOutcome]) -> ValidationOutcome:
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
        return _skipped("LLM did not propose any artifact")
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


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #


def validate_response(
    response: LLMResponse,
    current_artifacts: CurrentArtifacts,
    project_root: Optional[Path],
) -> ValidationOutcome:
    """Run the deterministic validator on an LLM response.

    Universal across LLMTask, custom tasks, and ad-hoc chat — keyed on
    which proposal slots the response populated, not on which button
    fired. Always returns a :class:`ValidationOutcome` — never raises.
    Validator crashes are caught and surfaced as ``skipped=True`` so GUI
    bugs in the validator can never block the LLM workflow.
    """
    if project_root is None:
        return _skipped("no active project")
    validate_fn, _ = _import_validator()
    if validate_fn is None:
        return _skipped("rules_packager_base.bijective_validator not importable")

    # Phase 7.1 (2026-04-30): auto-restore operator-only meta fields the
    # LLM tried to populate or modify. Runs BEFORE the artifact handlers
    # so the downstream validators see the corrected response.
    try:
        restore_issues = _auto_restore_operator_only_fields(response, current_artifacts)
    except Exception as exc:  # noqa: BLE001 — never let auto-restore block validation
        log.exception("auto-restore crashed; falling back without restoration.")
        restore_issues = []

    outcomes: list[ValidationOutcome] = []
    for handler in _ARTIFACT_HANDLERS:
        try:
            outcome = handler(response, current_artifacts, project_root, validate_fn)
        except Exception as exc:  # noqa: BLE001 — never let validator bugs block LLM flow
            log.exception(
                "validator handler %s crashed; falling back to operator review.",
                handler.__name__,
            )
            outcome = _skipped(f"validator crashed: {type(exc).__name__}: {exc}")
        if outcome is not None:
            outcomes.append(outcome)
    merged = _merge_outcomes(outcomes)
    if restore_issues:
        # Surface auto-restore warnings alongside any validator outcome.
        # Don't downgrade ok=True → False; warnings shouldn't block apply.
        merged = ValidationOutcome(
            ok=merged.ok,
            skipped=merged.skipped,
            issues=list(restore_issues) + list(merged.issues),
        )
    return merged


def is_loop_available(project_root: Optional[Path]) -> tuple[bool, str]:
    """Coarse availability probe used to gate the operator toggle's enabled
    state. Returns ``(available, reason)`` — when available is False, the
    reason is shown in the toggle's tooltip.
    """
    if project_root is None:
        return False, "no active project"
    _, avail_fn = _import_validator()
    if avail_fn is None:
        return False, "rules_packager_base.bijective_validator not importable"
    try:
        if not avail_fn():
            return False, "no pack registered bijective handlers in this venv"
    except Exception as exc:
        return False, f"validator probe crashed: {type(exc).__name__}: {exc}"
    return True, "deterministic path active"


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
        return _outcome_from_parse_error(exc)
    issue = ValidationIssueView(
        code=type(exc).__name__,
        message=str(exc) if str(exc) else repr(exc),
        severity="error",
        fix_hint="",
    )
    return ValidationOutcome(ok=False, skipped=False, issues=[issue])


def format_validator_feedback(
    outcome: ValidationOutcome,
    attempt: int,
    max_attempts: int,
    include_fix_hints: bool = True,
) -> str:
    """Render the outcome as a user-role follow-up message for the LLM.

    This is what powers the auto-retry loop — the LLM sees this turn after
    its previous attempt, learns from the structured ``[code]`` /
    ``fix hint`` payload, and re-emits a corrected response.

    Operator-only findings (``fixable_by == "operator"``) are filtered out
    of the LLM-facing message — the LLM cannot fix them by design, so showing
    them wastes tokens and confuses the rewrite. They remain visible to the
    operator via the dock widget.

    Set ``include_fix_hints=False`` to suppress the per-finding fix hint
    lines (useful for evaluating raw LLM correction ability without the
    parser's prescriptive guidance, or for keeping the feedback turn short).
    """
    llm_issues = [i for i in outcome.issues if i.fixable_by != "operator"]
    if not llm_issues:
        # All findings are operator-only — caller should have short-circuited.
        # Defensive empty return; callers must not pass this through.
        return ""
    lines = [
        f"[VALIDATOR FEEDBACK — automated, attempt {attempt} of {max_attempts}]",
        f"The deterministic validator rejected your previous response with {len(llm_issues)} error(s):",
        "",
    ]
    for issue in llm_issues:
        loc = f" at {issue.location}" if issue.location else ""
        lines.append(f"  - {issue.message}{loc}")
        if include_fix_hints and issue.fix_hint:
            lines.append(f"    fix hint: {issue.fix_hint}")
    lines.append("")
    lines.append(
        "Please produce a corrected response that resolves these errors. "
        "Do NOT repeat unaffected portions of correct content unless your "
        "output contract requires a full replacement."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Internal: faux ParseError for JSON-decode failures                          #
# --------------------------------------------------------------------------- #


class _FauxParseError:
    """Minimal duck-type used when the LLM returned proposed JSON as a
    string that doesn't itself decode. Lets us reuse
    :func:`_outcome_from_parse_error` without importing rules_packager_base
    types in this module."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        self.line = None
        self.column = None
        self.fix_hint = "Re-emit the procedure_json field as a valid JSON object."

    def __str__(self) -> str:
        return self.message


# --------------------------------------------------------------------------- #
# Operator-triggered "Validate Procedure" entry point                         #
# --------------------------------------------------------------------------- #


def validate_current_state(
    project_root: Optional[Path],
    text: Optional[str],
    json_str: Optional[str],
    code: Optional[str],
) -> ValidationOutcome:
    """Run the deterministic validator against the current on-disk artifacts.

    Distinct from :func:`validate_response` (which validates an LLM proposal):
    this is the operator's "Validate Procedure" button — no proposal, no LLM
    in the loop, just `text + json + code + inventory` through the validator's
    full mode='all' pipeline.

    Returns warnings as well as errors (unlike :func:`_outcome_from_report`
    which is tuned for the FSM and returns errors only). The findings panel
    benefits from seeing soft-warning codes too — operators can read them and
    decide whether to act.
    """
    validate_fn, _ = _import_validator()
    if validate_fn is None:
        return _skipped(
            "Validator unavailable: rules_packager_base.bijective_validator "
            "could not be imported. Build and install the rules_packager wheel."
        )

    if not (text or json_str or code):
        return _skipped(
            "No artifacts to validate (procedure_text, procedure.json, and "
            "test.py are all empty)."
        )

    json_obj: Optional[dict[str, Any]] = None
    if json_str and json_str.strip():
        try:
            json_obj = json.loads(json_str)
        except json.JSONDecodeError as exc:
            return _outcome_from_parse_error(_FauxParseError(
                "JSON_DECODE_FAILED",
                f"procedure.json is not valid JSON: {exc}",
            ))

    # v2.0.x design: bench identification lives in test.py module
    # constants. The inventory is extracted from those constants for
    # forward-only codegen (Doc 12). Falls back to legacy
    # `<project>/inventory.json` for projects pre-dating v2.0.x.
    inventory: Optional[dict[str, Any]] = None
    if code and json_obj is not None:
        inventory = _resolve_inventory(json_obj, project_root, code=code)

    try:
        report = validate_fn(
            text=text or None,
            json_obj=json_obj,
            code=code or None,
            inventory=inventory,
            mode="all",
        )
    except Exception as exc:  # pragma: no cover — defensive only
        log.exception("validate_current_state: validator raised: %s", exc)
        return ValidationOutcome(
            ok=False,
            skipped=False,
            issues=[ValidationIssueView(
                code="VALIDATOR_INTERNAL_ERROR",
                message=f"Validator raised an unexpected exception: {exc}",
                severity="error",
            )],
        )

    issues: list[ValidationIssueView] = []
    issues.extend(_issue_from_validator(i) for i in getattr(report, "errors", []))
    issues.extend(_issue_from_validator(i) for i in getattr(report, "warnings", []))
    return ValidationOutcome(
        ok=bool(getattr(report, "ok", True)),
        skipped=False,
        issues=issues,
    )
