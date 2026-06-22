"""Section-ownership reconstruction — the single seam where the pipeline
combines ownership resolution with the ``pack_parsers`` reconstruct bridge.

The LLM authors the body sections (``## Equipment``, ``## Steps``,
``## Expected``); the parser reconstructs the operator-owned sections
(``# <title>``, description, ``## Meta``) from the PRIOR procedure.

This module is called from BOTH ends of the pipeline:
  - before validation (``validator_dispatch._validate_proposed_text``), and
  - at proposal-apply (``text_json_tab`` / ``text_only_tab``).

Both call sites go through :func:`reconstruct_for_pipeline` so the ownership
resolution + reconstruct combination lives in exactly one place. No Qt, no I/O
beyond what the ``pack_parsers`` bridge does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from . import pack_parsers, section_ownership


def pipeline_ownership(
    project_root: Optional[Path] = None,
    task_override: Optional[Iterable[str]] = None,
) -> "section_ownership.SectionOwnership":
    """The single place the pipeline resolves section ownership: the bundle's
    declared map (``pack_parsers.get_section_ownership``) as the base, with an
    optional per-task override. Both the prompt emit-list and reconstruction
    derive from this.

    Degrades gracefully: if the deterministic parser is unavailable (no wheel /
    no project venv / subprocess error), fall back to the baked-in
    ``DEFAULT_OWNERSHIP`` rather than raising. This keeps the prompt-build path
    non-fatal — the LLM fallback must remain available without the wheel. The
    reconstruction caller still fails loudly later (``reconstruct_text`` itself
    raises ``ParserUnavailable``, caught by ``reconstructed_or_error``)."""
    try:
        base = pack_parsers.get_section_ownership(project_root)
    except pack_parsers.ParserUnavailable:
        base = section_ownership.DEFAULT_OWNERSHIP
    return section_ownership.resolve(base, task_override)


def reconstruct_for_pipeline(
    proposed_text: str,
    prior_text: Optional[str],
    *,
    task_override: Optional[Iterable[str]] = None,
    project_root: Optional[Path] = None,
):
    """Reconstruct an LLM proposal against the prior procedure using the
    resolved section ownership. Returns the pack_parsers reconstruct report
    (.success/.text/.json/.findings/.ok/.errors/.warnings).

    Ownership always resolves through :func:`pipeline_ownership` (bundle
    side-car / wheel default as base, plus any *task_override*), so the
    bundle's editable ``section_ownership.json`` side-car is respected on the
    no-override path too. The resolved LLM set is threaded into
    ``reconstruct_text`` as an explicit ``owned_sections``; for the DEFAULT
    case this is equivalent to the old ``owned_sections=None`` (the wheel's
    reconstruct treats an explicit set equal to its default the same as None —
    see ``ReconstructOverrideEquivalenceTests`` in tests/test_reconstruction.py).
    The side-car read is a cheap file read, so the former no-override fast-path
    (kept only to skip a subprocess round-trip) is no longer needed.
    """
    owned = set(pipeline_ownership(project_root, task_override).llm_sections)
    return pack_parsers.reconstruct_text(
        proposed_text,
        prior_text,
        owned_sections=owned,
        project_root=project_root,
    )


def reconstructed_or_error(
    proposed_text: str,
    prior_text: Optional[str],
    *,
    task_override: Optional[Iterable[str]] = None,
    project_root: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Guarded reconstruction for the apply (proposal → diff dialog) path.

    Return ``(strict_text, best_effort_text, error_message)``:
      - **success**: ``(reconstructed_text, reconstructed_text, None)``.
      - **invalid proposal** (``recon.success == False``): ``(None,
        half_built_text, findings)`` — ``strict_text`` is None so it is never
        auto-applied, but the half-built document is returned so the apply
        path can still offer a *reviewable* diff (with a warning banner)
        instead of silently dropping an invalid proposal. This is what makes
        "auto-correct off" actually let the operator review/fix a bad draft.
      - **nothing to show** (``ParserUnavailable`` — wheel missing / subprocess
        error / timeout): ``(None, None, reason)``.

    Two failure modes the bare :func:`reconstruct_for_pipeline` call leaks:
      - ``pack_parsers.ParserUnavailable`` — caught here so it never propagates
        into a Qt slot and silently drops the proposal.
      - ``recon.success == False`` — the wheel still returns a (usually
        NON-None) half-built ``.text``; we surface it as ``best_effort_text``
        alongside the findings.

    Pure (no Qt). Call sites show the diff when a text is available (with the
    findings as a banner when ``strict_text`` is None), or post a chat warning
    only when ``best_effort_text`` is None too.
    """
    try:
        recon = reconstruct_for_pipeline(
            proposed_text,
            prior_text,
            task_override=task_override,
            project_root=project_root,
        )
    except pack_parsers.ParserUnavailable as exc:
        return None, None, f"Could not reconstruct procedure_text.md: {exc}"
    if not recon.success:
        details = "; ".join(
            f"[{i.code}] {i.message}" for i in recon.errors
        ) or "reconstruction failed (no details)"
        return None, recon.text, details
    return recon.text, recon.text, None
