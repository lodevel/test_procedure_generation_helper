"""Detect when the dcdc_authoring skill has emitted a finished test block.

The authoring wizard's turn is "done" once the LLM has called the
``generate_dcdc_test`` tool, whose output is the canonical procedure block:
a ``## Equipment`` / ``## Steps`` / ``## Expected`` triple, in that exact order
(see :func:`workflow_editor.authoring.dcdc_test_generator.generate_dcdc_test`,
which assembles those three headings at dcdc_test_generator.py:638-646). While
the turn is still a clarifying question ("which rail?"), none of those headings
are present, so the assistant text is the signal we scrape.

PRAGMATIC PROSE DETECTOR (shipped here)
---------------------------------------
:func:`find_dcdc_test_block` scans the assistant text for the three headings in
order and returns the block; :func:`is_done` is the boolean wrapper. This works
off ``LLMResponse`` text alone — the only thing the headless run-skill path
reliably exposes (``SkillChatSession.interpret`` prefers
``LLMResponse.assistant_message`` and falls back to ``raw_response``;
backend_base.py:91 and :70 respectively).

PLANNED UPGRADE — the tool-call event is a stronger signal
----------------------------------------------------------
A prose scrape can be fooled (the LLM could paste a sample block while still
asking a question). The unambiguous signal is the ``generate_dcdc_test``
TOOL-CALL event itself. As mapped, the backend does NOT surface tool calls today:

  * ``LLMResponse`` has no ``tool_calls``/``tool_results`` field — only
    ``raw_response`` (backend_base.py:70) and ``assistant_message``
    (backend_base.py:91).
  * The OpenCode SSE handler
    ``OpenCodeBackend._process_sse_event`` (opencode_backend.py:693) only
    branches on ``part_type == "reasoning"`` (opencode_backend.py:746) and
    ``part_type == "text"`` (opencode_backend.py:761); an OpenCode ``"tool"``
    part (which carries the ``generate_dcdc_test`` invocation + result) hits no
    branch and is dropped.

The upgrade: add a ``"tool"`` branch in ``_process_sse_event`` that records the
tool name + result onto a new ``LLMResponse.tool_calls`` field, then change
:func:`is_done` to "saw a ``generate_dcdc_test`` tool-call event" and have
:func:`find_dcdc_test_block` prefer the captured tool result over the prose
scrape. Until that lands, the block detector below is the signal — SHIP IT.

Pure stdlib; no Qt, no I/O.
"""
from __future__ import annotations

import re
from typing import Optional

# The three canonical section headings, in the order generate_dcdc_test emits
# them. Each must appear on its own line (a Markdown ATX heading). We allow
# leading indentation and flexible inner spacing so a slightly-reflowed block
# (or one inside a fenced code block) still matches, but the '##' level and the
# heading word are exact.
_HEADINGS = ("Equipment", "Steps", "Expected")


def _heading_pattern(word: str) -> re.Pattern:
    """A MULTILINE regex matching ``## <word>`` as a standalone heading line."""
    return re.compile(rf"(?m)^[ \t]*(##[ \t]+{re.escape(word)}[ \t]*)$")


_PATTERNS = tuple(_heading_pattern(w) for w in _HEADINGS)


def find_dcdc_test_block(text: str) -> Optional[str]:
    """Return the canonical generated-test block, or ``None``.

    The block is recognised iff ``text`` contains the headings ``## Equipment``,
    ``## Steps`` and ``## Expected`` — each on its own line — in THAT order. When
    matched, the returned string is the slice from the ``## Equipment`` heading
    to the end of ``text`` (right-stripped); otherwise ``None``.

    Out-of-order headings, a missing heading, or a plain question reply all
    return ``None``.
    """
    if not text:
        return None

    pos = 0
    start: Optional[int] = None
    for i, pat in enumerate(_PATTERNS):
        m = pat.search(text, pos)
        if m is None:
            return None
        if i == 0:
            # Slice from the '##' (group 1), not any leading indentation.
            start = m.start(1)
        # Next heading must come strictly AFTER this one (enforces order).
        pos = m.end()

    assert start is not None  # all three patterns matched
    return text[start:].rstrip()


def is_done(text: str) -> bool:
    """True iff ``text`` contains a finished dcdc test block (see above)."""
    return find_dcdc_test_block(text) is not None
