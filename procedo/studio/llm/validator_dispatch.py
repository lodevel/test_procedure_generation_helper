"""Validator dispatch for the LLM-with-feedback loop.

Single point of integration with the project's deterministic v2.0.1 text
renderer (loaded via ``ProjectManager.get_text_renderer()``).
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

This module owns (the editor-facing :class:`ValidationOutcome` lives in
``core/validation_outcome.py``):
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
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..core.validation_outcome import (
    ValidationIssueView,
    ValidationOutcome,
    _FauxParseError,
    _issue_from_validator,
    _merge_outcomes,
    _outcome_from_parse_error,
    _outcome_from_report,
    _skipped,
)
from . import reconstruction
from .backend_base import LLMResponse

log = logging.getLogger(__name__)


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


def _fragment_line_shift(recon_text: str, fragment_text: str) -> int:
    """Line shift (reconstructed-doc line − fragment line) for the body region.

    Reconstruction prepends operator-owned identity (title/description/``## Meta``)
    ahead of the LLM-authored body fragment, so a finding the validator reports in
    reconstructed-doc coordinates is offset from where the LLM actually wrote it.
    Anchoring on the first non-blank fragment line gives that constant shift; both
    indices are 0-based so the delta equals the 1-based delta. Returns 0 when the
    anchor can't be located (no reconstruction shift assumed).

    Both sides are split with ``splitlines()`` so CRLF/LF/CR fragments and the
    LF-normalized recon line up (terminators dropped). The recon body is appended
    after the identity, so its anchor is the *last* occurrence — using it tolerates
    the same anchor line appearing in the prepended identity. Residual assumption:
    the anchor is not duplicated *within* the LLM body itself."""
    recon_lines = recon_text.splitlines()
    frag_lines = fragment_text.splitlines()
    anchor = next((l for l in frag_lines if l.strip()), None)  # first non-blank
    if anchor is None or anchor not in recon_lines:
        return 0
    recon_idx = len(recon_lines) - 1 - recon_lines[::-1].index(anchor)
    frag_idx = frag_lines.index(anchor)
    return recon_idx - frag_idx


def _validate_proposed_text(
    response: LLMResponse,
    current: CurrentArtifacts,
    project_root: Path,
    validate_fn: Callable[..., Any],
    task_override: Optional[Iterable[str]] = None,
) -> Optional[ValidationOutcome]:
    """The LLM proposed a ``procedure_text`` update. Reconstruct the body
    fragment against the prior procedure (so operator-owned identity comes
    from the prior, not the LLM), then validate the reconstructed full
    document. Runs R1 against the current/proposed json (whichever is in
    scope); if no json is available, falls back to text-alone parse+R3+R4
    via the validator's text-only path. Also runs name-fidelity (catches
    LLM stripping leading/trailing/internal punctuation from named tokens
    like `+HIGH_28V` -> `HIGH_28V`) when the project config allows it.
    """
    proposed_text = _proposal_content(response, "procedure_text")
    if proposed_text is None:
        return None
    # Pass the operator's pre-LLM text as `original_text` so the renderer
    # can run name-fidelity. The disable key (config.json -> validation.
    # name_fidelity = "disabled") opts the project out for cases where
    # the heuristic produces too many false positives.
    original_text = current.get("text")

    # Reconstruct the operator-owned sections from the prior BEFORE
    # validating, so the validator sees the full document the operator
    # will actually apply (identity + Meta from prior, body from the LLM).
    recon = reconstruction.reconstruct_for_pipeline(
        str(proposed_text), original_text,
        task_override=task_override, project_root=project_root,
    )
    # recon.text is set even on failure (half-built doc); compute the shift so we
    # can translate full-doc line numbers back to fragment coords per-issue below.
    shift = _fragment_line_shift(recon.text, str(proposed_text))
    if not recon.success:
        outcome = _outcome_from_report(recon)  # surfaces RECON_* / body findings
        _translate_issue_lines(outcome, shift)
        return outcome
    reconstructed_text = recon.text

    json_obj = _resolve_json_for_crosscheck(response, current)
    check_names = _name_fidelity_enabled(project_root)

    report = validate_fn(
        text=reconstructed_text,
        json_obj=json_obj,
        mode="all",
        original_text=original_text,
        check_names=check_names,
    )
    outcome = _outcome_from_report(report)
    _translate_issue_lines(outcome, shift)
    return outcome


def _translate_issue_lines(outcome: ValidationOutcome, shift: int) -> None:
    """Rewrite each issue's ``.line`` from reconstructed-doc to fragment coords.

    This is the ONE place that knows the text path's coordinate space, so the
    translation lives here rather than at render time — json-handler findings
    (their own coords, offset 0) are never touched. ``issue.line`` becomes
    fragment-coords (LLM-facing); ``issue.location`` is left UNTOUCHED so the
    operator dock keeps the full-document location. A translated line < 1 sits in
    the parser-owned identity region (the LLM can't address it) → ``None``."""
    for issue in outcome.issues:
        if issue.line is not None:
            frag = issue.line - shift
            issue.line = frag if frag >= 1 else None


def _name_fidelity_enabled(project_root: Optional[Path]) -> bool:
    """Read ``<project>/config/config.json -> validation.name_fidelity``.
    Default: enabled. Returns False only when explicitly set to "disabled"
    (string) or False (bool)."""
    if project_root is None:
        return True
    cfg_path = project_root / "config" / "config.json"
    if not cfg_path.exists():
        return True
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    val = (cfg.get("validation") or {}).get("name_fidelity", True)
    if isinstance(val, str):
        return val.lower() != "disabled"
    return bool(val)


def _validate_proposed_json(
    response: LLMResponse,
    current: CurrentArtifacts,
    project_root: Path,
    validate_fn: Callable[..., Any],
    task_override: Optional[Iterable[str]] = None,
) -> Optional[ValidationOutcome]:
    """The LLM proposed a ``procedure_json`` update. Always runs
    R3 (schema) + R4 (topology) + R5 (semantic) on the proposed JSON;
    cross-checks with current text (R1) when available.

    Phase 9.4: R2 (JSON↔code) is no longer dispatched; ``code``/``inventory``
    are accepted but unused.

    ``task_override`` is accepted for a uniform handler signature but
    ignored — JSON proposals don't go through section-ownership
    reconstruction (only the text path does).
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
    # Mirror the text path's reconstruct step, JSON-side: re-derive the
    # parser-owned Meta pins (format_version / rules_pack and the conditional
    # labscpi_pack / fncore_pack) from the ACTIVE BUNDLE so the LLM may OMIT
    # meta and never supplies a wrong-but-valid pin. Best-effort: any failure
    # leaves the JSON untouched and the validator reports the genuine error.
    _fill_parser_owned_meta(proposed_json, project_root)
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


def _fill_parser_owned_meta(
    json_obj: Any, project_root: Optional[Path],
) -> None:
    """Graft bundle-derived parser-owned meta onto ``json_obj['meta']`` in place.

    JSON-side analogue of the text path's reconstruct step
    (``_validate_proposed_text`` -> ``reconstruct_for_pipeline``). Re-derives the
    parser-owned Meta (format_version / rules_pack + conditional labscpi_pack /
    fncore_pack pins) from the active bundle via ``pack_parsers.sync_meta_json``,
    which PRESERVES operator-owned keys (board, requirement, revision,
    authored_by). So the LLM may OMIT meta — or supply a wrong-but-valid pin —
    and the validated meta always reflects the active bundle. Best-effort and
    non-fatal: if the deterministic path is unavailable or the proposal is too
    broken to render/parse, the JSON is left exactly as the LLM authored it and
    the validator reports the real finding.
    """
    if not isinstance(json_obj, dict):
        return
    from ..pack_ops import engine_replies, engine_session, pack_parsers

    try:
        derived = pack_parsers.sync_meta_json(json_obj, project_root=project_root)
    except (engine_session.ParserUnavailable, engine_replies.ParseFailure) as exc:
        log.debug("meta fill skipped (parser path unavailable): %s", exc)
        return
    except Exception as exc:  # noqa: BLE001 - never break the gate on a fill error
        log.debug("meta fill skipped (unexpected): %s", exc)
        return
    if derived:
        json_obj["meta"] = derived


def _validate_proposed_code(
    response: LLMResponse,
    current: CurrentArtifacts,
    project_root: Path,
    validate_fn: Callable[..., Any],
    task_override: Optional[Iterable[str]] = None,
) -> Optional[ValidationOutcome]:
    """The LLM proposed a ``test_code`` update.

    ``task_override`` is accepted for a uniform handler signature but
    ignored — code proposals don't go through section-ownership
    reconstruction (only the text path does).

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
    if the response's JSON value differs from the baseline:
      * Mutates ``response.procedure_json.content`` (when proposed) to
        rewrite ``meta[<field>]`` with the baseline value.

    The TEXT side is intentionally NOT touched here: the text apply/validate
    path runs section-ownership reconstruction, which rebuilds ``## Meta``
    (including ``requirement:``) wholesale from the prior. Line-editing the
    text proposal would be a dead effect — the reconstructed Meta overrides
    it — and would emit a misleading ``META_REQUIREMENT_AUTO_RESTORED``
    warning. The JSON path does NOT reconstruct, so its restore stays live.

    Returns a list of informational :class:`ValidationIssueView` (severity
    ``warning``) describing each restoration.

    The implementation does NOT call the full text_parser/text_emitter to
    avoid a stale-wheel dependency: a dict edit on ``meta[<field>]`` is
    sufficient and works regardless of which rules_packager_base version is
    installed.
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
        json_change = _restore_requirement_in_json_proposal(response, baseline_value)
        if json_change:
            from_value = json_change[0]
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


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #


def validate_response(
    response: LLMResponse,
    current_artifacts: CurrentArtifacts,
    project_root: Optional[Path],
    task_override: Optional[Iterable[str]] = None,
) -> ValidationOutcome:
    """Run the deterministic validator on an LLM response.

    Universal across LLMTask, custom tasks, and ad-hoc chat — keyed on
    which proposal slots the response populated, not on which button
    fired. Always returns a :class:`ValidationOutcome` — never raises.
    Validator crashes are caught and surfaced as ``skipped=True`` so GUI
    bugs in the validator can never block the LLM workflow.

    ``task_override`` is the per-task section-ownership override
    (``TaskConfig.llm_owned_sections``; ``None`` → bundle default). It is
    threaded into the text handler's before-validate reconstruction so the
    validated document uses the same LLM-owned set as the prompt emit-list
    and the at-apply reconstruction.
    """
    if project_root is None:
        return _skipped("no active project")
    from ..pack_ops import pack_parsers
    import functools
    available, reason = pack_parsers.is_available(project_root)
    if not available:
        return _skipped(f"deterministic validator unavailable: {reason}")
    validate_fn = functools.partial(pack_parsers.validate, project_root=project_root)

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
            outcome = handler(
                response, current_artifacts, project_root, validate_fn,
                task_override,
            )
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

    Resolution order:
      1. ``validator_loop.enabled`` flag in the project config — when
         False, the validator is intentionally OFF and we return the
         "disabled" reason. Callers suppress validator-unavailable
         warnings for this reason (Phase 4.6: stop nagging operators
         who opted out).
      2. Probe the per-project text_renderer + the pack's bijective
         handler. Either missing → "validator unavailable" reason.
    """
    if project_root is None:
        return False, "no active project"
    # 1. Per-project explicit opt-out.
    from .validator_loop_settings import is_enabled as _is_enabled
    if not _is_enabled(project_root):
        return False, "disabled in project settings (validator_loop.enabled=false)"
    # 2. Wheel-import probe via the project's own venv Python.
    from ..pack_ops import pack_parsers
    available, reason = pack_parsers.is_available(project_root)
    if not available:
        return False, f"deterministic validator unavailable — {reason}"
    return True, "deterministic path active"


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

    Warnings (``severity == "warning"``) are also filtered out — they
    document state the system already handled (e.g.
    ``META_REQUIREMENT_AUTO_RESTORED`` which fires AFTER the auto-
    restore mutated the proposal). Asking the LLM to "fix" something
    the system already fixed wastes a retry turn and risks the LLM
    re-introducing the very thing auto-restore stripped.

    Set ``include_fix_hints=False`` to suppress the per-finding fix hint
    lines (useful for evaluating raw LLM correction ability without the
    parser's prescriptive guidance, or for keeping the feedback turn short).
    """
    llm_issues = [
        i for i in outcome.issues
        if i.fixable_by != "operator" and i.severity != "warning"
    ]
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
        if issue.line is not None:
            # ``issue.line`` is already in fragment coordinates — the text handler
            # translated it (json findings carry their own coords, never shifted).
            loc = f" at line {issue.line}"
        elif issue.location:
            # No structured line (e.g. schema/pointer findings) — the pointer is
            # coordinate-free, so show it untranslated.
            loc = f" at {issue.location}"
        else:
            loc = ""
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
    from ..pack_ops import pack_parsers
    available, reason = pack_parsers.is_available(project_root)
    if not available:
        return _skipped(f"Validator unavailable: {reason}")

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

    # Note: Phase 5.1 no longer wires inventory / code through the
    # renderer-side validate. The legacy renderer ignored them anyway
    # (codegen path is separate; see pack_parsers.generate_code).

    try:
        report = pack_parsers.validate(
            text=text or None,
            json_obj=json_obj,
            mode="all",
            project_root=project_root,
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
