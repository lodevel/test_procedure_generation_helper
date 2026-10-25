"""A regenerated test.py keeps the operator's pinned bench constants, one way for every view that
regenerates it (⚡ Quick Code, the assistant's test.py proposal, ⚡ Parse + Generate).

Bench identification (VISA/COM address, baud, timeout, remote, channels) lives only in test.py
module constants (v2.0.x design, 2026-04-28 operator directive). Codegen — and the LLM, which does
not know the bench — emits defaults (``ASRL1::INSTR``, ``COM1``) that would otherwise clobber the
operator's real values on every regen, and the operator would see them as changes to review.
"""

from __future__ import annotations

import logging

from ..llm.code_constants_merge import equipment_ids_from_procedure, preserve_bench_constants

log = logging.getLogger(__name__)


def keep_pinned_constants(code: str, current_code: str, procedure, source: str) -> str:
    """*code* with *current_code*'s pinned constants for *procedure*'s equipment put back (a
    procedure.json dict; anything else has no equipment); *source* names the regen in the log."""
    ids = equipment_ids_from_procedure(procedure) if isinstance(procedure, dict) else []
    code, replaced = preserve_bench_constants(code, current_code, ids)
    if replaced:
        log.info(
            "%s: preserved %d operator-pinned constant(s): %s",
            source,
            len(replaced),
            ", ".join(replaced),
        )
    return code
