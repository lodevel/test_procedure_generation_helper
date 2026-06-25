"""Tests for the shared context-usage readout helpers.

These are pure functions (no Qt) used by BOTH the dock/tab chat and the skill
chat so the "Context: X / Y tokens (Z%)" line can't drift between them.
"""

from types import SimpleNamespace

import pytest

from workflow_editor.llm.context_usage import (
    DEFAULT_COLOUR,
    THRESHOLD_COLOURS,
    format_context_usage,
    latest_message_total,
    used_tokens,
    usage_colour,
)


class TestUsedTokens:
    def test_prefers_total(self):
        # total wins even when prompt+completion sum to something else.
        r = SimpleNamespace(total_tokens=5000, prompt_tokens=3000, completion_tokens=1000)
        assert used_tokens(r) == 5000

    def test_falls_back_to_prompt_plus_completion(self):
        r = SimpleNamespace(total_tokens=0, prompt_tokens=3000, completion_tokens=1200)
        assert used_tokens(r) == 4200

    def test_falls_back_to_prompt_only(self):
        r = SimpleNamespace(total_tokens=0, prompt_tokens=900, completion_tokens=0)
        assert used_tokens(r) == 900

    def test_zero_when_nothing(self):
        r = SimpleNamespace(total_tokens=0, prompt_tokens=0, completion_tokens=0)
        assert used_tokens(r) == 0

    def test_missing_attrs_are_zero(self):
        # A bare object with no token attributes at all -> 0, no crash.
        assert used_tokens(SimpleNamespace()) == 0

    def test_none_values_treated_as_zero(self):
        r = SimpleNamespace(total_tokens=None, prompt_tokens=None, completion_tokens=None)
        assert used_tokens(r) == 0


class TestLatestMessageTotal:
    def test_picks_last_message_with_tokens(self):
        msgs = [
            SimpleNamespace(total_tokens=1000),
            SimpleNamespace(total_tokens=0),   # a user turn carries no total
            SimpleNamespace(total_tokens=4200),
            SimpleNamespace(total_tokens=0),   # trailing user turn
        ]
        # Compaction-correct: latest *reported* total, NOT the sum (5200).
        assert latest_message_total(msgs) == 4200

    def test_drops_after_compaction(self):
        # A running sum would only ever climb; the latest total can drop.
        before = [SimpleNamespace(total_tokens=15000)]
        after = before + [SimpleNamespace(total_tokens=6000)]
        assert latest_message_total(after) == 6000

    def test_empty_is_zero(self):
        assert latest_message_total([]) == 0
        assert latest_message_total(None) == 0


class TestFormatContextUsage:
    def test_text_format_with_thousands_separator(self):
        text, _ = format_context_usage(12345, 200000)
        assert text == "Context: 12,345 / 200,000 tokens (6%)"

    def test_percent_rounds_to_integer(self):
        text, _ = format_context_usage(1500, 2000)  # 75%
        assert "(75%)" in text

    def test_colour_neutral_below_80(self):
        _, colour = format_context_usage(79, 100)
        assert colour == DEFAULT_COLOUR == ""

    def test_colour_yellow_at_80(self):
        _, colour = format_context_usage(80, 100)
        assert colour == THRESHOLD_COLOURS[80.0] == "#b8860b"

    def test_colour_orange_at_90(self):
        _, colour = format_context_usage(90, 100)
        assert colour == THRESHOLD_COLOURS[90.0] == "#e67e22"

    def test_colour_red_at_95(self):
        _, colour = format_context_usage(95, 100)
        assert colour == THRESHOLD_COLOURS[95.0] == "#c0392b"

    def test_colour_red_when_over_full(self):
        _, colour = format_context_usage(150, 100)  # 150%
        assert colour == "#c0392b"

    def test_zero_limit_does_not_crash(self):
        text, colour = format_context_usage(100, 0)
        # limit floored to 1 -> 10000% -> red, no ZeroDivisionError.
        assert "tokens" in text
        assert colour == "#c0392b"


class TestUsageColourThresholdBoundaries:
    @pytest.mark.parametrize(
        "pct,expected",
        [
            (0.0, ""),
            (79.9, ""),
            (80.0, "#b8860b"),
            (89.9, "#b8860b"),
            (90.0, "#e67e22"),
            (94.9, "#e67e22"),
            (95.0, "#c0392b"),
            (100.0, "#c0392b"),
        ],
    )
    def test_boundaries(self, pct, expected):
        assert usage_colour(pct) == expected
