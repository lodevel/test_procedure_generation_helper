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

    When *task_override* is None the wheel's declared default ownership is
    used (equipment/steps/expected = LLM); this avoids an extra subprocess
    ``get_section_ownership`` round-trip in the common case. When provided,
    *task_override* is the authoritative LLM-owned section set: it is resolved
    against the bundle's base map and threaded into ``reconstruct_text`` so
    everything else becomes parser-owned.
    """
    if task_override is None:
        return pack_parsers.reconstruct_text(
            proposed_text,
            prior_text,
            owned_sections=None,
            project_root=project_root,
        )

    base = pack_parsers.get_section_ownership(project_root)
    owned = set(section_ownership.resolve(base, task_override).llm_sections)
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
) -> tuple[Optional[str], Optional[str]]:
    """Guarded reconstruction for the apply (proposal → diff dialog) path.

    Return ``(text_to_apply, error_message)``. On any failure
    ``text_to_apply`` is None and ``error_message`` is a human-readable
    reason; on success the reverse (``(reconstructed_text, None)``).

    Two failure modes the bare :func:`reconstruct_for_pipeline` call leaks:
      - ``pack_parsers.ParserUnavailable`` (wheel missing / subprocess
        error / timeout) — caught here so it never propagates into a Qt
        slot and silently drops the proposal.
      - ``recon.success == False`` — the wheel still returns a NON-None
        half-built ``.text`` in this case, so an ``is not None`` guard at
        the call site would let a broken document through. We summarise the
        findings into the error message instead.

    Pure (no Qt). Call sites turn ``error_message`` into a system chat
    message and return without applying.
    """
    try:
        recon = reconstruct_for_pipeline(
            proposed_text,
            prior_text,
            task_override=task_override,
            project_root=project_root,
        )
    except pack_parsers.ParserUnavailable as exc:
        return None, f"Could not reconstruct procedure_text.md: {exc}"
    if not recon.success:
        details = "; ".join(
            f"[{i.code}] {i.message}" for i in recon.errors
        ) or "reconstruction failed (no details)"
        return None, details
    return recon.text, None
