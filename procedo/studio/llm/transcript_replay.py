"""Compact, text-only replay of a tab's chat transcript for lost-session recovery.

When the OpenCode server is replaced mid-chat (fresh install / different storage)
its server-side session 404s. The dock then mints a fresh session and replays the
prior conversation as a single 'Conversation so far' preamble built here. Pure (no
Qt/HTTP), unit-testable; replays only the human-visible ChatMessage.content (never
the giant full_prompt/full_response), matching the text-only contract.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tab_context import ChatMessage

# Model-FACING preamble (not just a user disclaimer): the prior tool results are
# genuinely gone from the fresh session, so the model is instructed to re-call
# the relevant tools before trusting ANY value quoted from the replayed text.
# This closes the silent-wrong-output hole where the model reuses stale
# netlist/BOM/datasheet facts asserted in earlier turns.
_HEADER = (
    "Conversation so far, replayed after the previous server session was lost. "
    "The earlier tool results (netlist, BOM, datasheets) are GONE from this new "
    "session. Before relying on ANY component value, net name, or part number "
    "quoted below, you MUST re-call the relevant tools to re-verify it -- treat "
    "the quoted text as a memory aid, not as a trusted source:"
)
_ROLE_LABEL = {"user": "User", "assistant": "Assistant", "system": "Note"}


def serialize_transcript(messages, char_budget: int) -> str:
    """Render messages (list[ChatMessage]) as a preamble bounded to char_budget.

    Accrues NEWEST-first so the most recent turns survive truncation, emits
    oldest-first. Skips empty turns. Returns '' when nothing to replay.
    """
    if not messages or char_budget <= 0:
        return ""
    blocks: list[str] = []
    used = len(_HEADER) + 2
    for msg in reversed(messages):
        content = (getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        label = _ROLE_LABEL.get(getattr(msg, "role", ""), "User")
        block = f"{label}: {content}"
        if used + len(block) + 2 > char_budget:
            break
        blocks.append(block)
        used += len(block) + 2
    if not blocks:
        return ""
    blocks.reverse()
    return _HEADER + "\n\n" + "\n\n".join(blocks)
