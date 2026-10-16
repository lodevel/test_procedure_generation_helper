"""Shared context-usage readout — ONE implementation for both chats.

Both the dock (tab) chat and the skill chat show a "Context: X / Y tokens (Z%)"
line. They used to compute it two different ways; this module is the single
source of truth so they can't drift.

The compaction-correct "current context" is the LATEST turn's reported total
tokens, NOT a running sum of every turn. OpenCode reports the total (input +
output) for each turn post-compaction, so after a compact the number naturally
drops; a running sum would keep climbing and over-count. These helpers are pure
(no Qt) so they're trivially testable and reusable from either widget.
"""

from __future__ import annotations

from typing import Mapping

# Usage→colour thresholds (percent of the context window), shared so callers
# don't duplicate the hex values. Ordered high→low; the first threshold whose
# percentage is met wins. The empty-string sentinel below means "under all
# thresholds — let the widget use its own muted default colour".
THRESHOLD_COLOURS: Mapping[float, str] = {
    95.0: "#c0392b",  # red — effectively full
    90.0: "#e67e22",  # orange
    80.0: "#b8860b",  # dark yellow
}

# Returned by format_context_usage when usage is below every threshold; callers
# treat it as "use the widget's muted/default colour" rather than a real colour.
DEFAULT_COLOUR = ""


def used_tokens(response) -> int:
    """The compaction-correct "current context" size for one response.

    Prefers ``total_tokens`` (what OpenCode reports per turn, which already
    reflects a compaction); falls back to ``prompt_tokens + completion_tokens``,
    then ``prompt_tokens`` alone, then 0 when nothing is available.
    """
    total = getattr(response, "total_tokens", 0) or 0
    if total:
        return int(total)
    prompt = getattr(response, "prompt_tokens", 0) or 0
    completion = getattr(response, "completion_tokens", 0) or 0
    if prompt or completion:
        return int(prompt) + int(completion)
    return int(prompt)


def latest_message_total(messages) -> int:
    """The latest stored turn's total tokens — the tab chat's current context.

    Scans from the end for the most recent message carrying token usage and
    returns its :func:`used_tokens` value (same total→prompt+completion→prompt
    fallback chain the skill chat uses on a live response), so the tab chat
    tracks compaction the same way instead of summing every turn. Returns 0
    when no message has a token count yet.
    """
    for msg in reversed(list(messages or [])):
        used = used_tokens(msg)
        if used:
            return used
    return 0


def usage_colour(pct: float) -> str:
    """Colour hex for a usage percentage, or ``DEFAULT_COLOUR`` if under all
    thresholds (caller substitutes its widget's muted default)."""
    for threshold in sorted(THRESHOLD_COLOURS, reverse=True):
        if pct >= threshold:
            return THRESHOLD_COLOURS[threshold]
    return DEFAULT_COLOUR


def format_context_usage(used: int, limit: int) -> tuple[str, str]:
    """Render the shared readout: ``(text, colour)``.

    ``text`` is ``"Context: {used:,} / {limit:,} tokens ({pct:.0f}%)"``.
    ``colour`` is a hex string from :data:`THRESHOLD_COLOURS`, or
    :data:`DEFAULT_COLOUR` (empty) meaning the caller should use its widget's
    muted default. ``limit`` is floored at 1 to avoid divide-by-zero.
    """
    limit = int(limit) or 1
    pct = 100.0 * int(used) / limit
    text = f"Context: {int(used):,} / {limit:,} tokens ({pct:.0f}%)"
    return text, usage_colour(pct)
