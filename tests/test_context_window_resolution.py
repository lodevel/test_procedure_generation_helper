"""Tests for the model-context-window resolver used by the chat readouts.

``resolve_context_window`` is a PURE parse helper (no HTTP) that maps an
OpenCode ``providerID/modelID`` string + a ``GET /config/providers`` JSON doc to
the model's real ``limit.context``. It backs ``OpenCodeBackend.get_context_window``
so the context-% denominator reflects the model's true window (e.g. gpt-5.x's
272k+) instead of the static ``common_llm.context_window`` setting.

The sample doc below mirrors the shape a live OpenCode 1.17 server reports.
"""

import pytest

from workflow_editor.llm.opencode_backend import resolve_context_window


# Trimmed but shape-faithful copy of a real GET /config/providers response.
SAMPLE_PROVIDERS = {
    "providers": [
        {
            "id": "openai",
            "name": "OpenAI",
            "models": {
                "gpt-5.4": {
                    "id": "gpt-5.4",
                    "limit": {"context": 1050000, "input": 922000, "output": 128000},
                },
                "gpt-5.5": {
                    "id": "gpt-5.5",
                    "limit": {"context": 272000, "input": 200000, "output": 128000},
                },
                "no-limit-model": {"id": "no-limit-model"},
            },
        },
        {
            "id": "my_vllm",
            "name": "vLLM",
            "models": {
                "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit": {
                    "id": "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
                    "limit": {"context": 131072, "output": 65536},
                },
            },
        },
    ],
    "default": {"openai": "gpt-5.5", "my_vllm": "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"},
}


class TestResolveContextWindow:
    def test_resolves_modern_model_window(self):
        assert resolve_context_window("openai/gpt-5.4", SAMPLE_PROVIDERS) == 1050000

    def test_gpt_5x_reports_large_window_not_static_default(self):
        # The whole point: gpt-5.x carries a window far larger than the old
        # static 15000/16384 setting.
        assert resolve_context_window("openai/gpt-5.5", SAMPLE_PROVIDERS) == 272000

    def test_model_id_with_internal_slash(self):
        # modelID itself contains '/': only the FIRST '/' splits provider/model.
        assert (
            resolve_context_window(
                "my_vllm/cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit", SAMPLE_PROVIDERS
            )
            == 131072
        )

    def test_unknown_provider_returns_none(self):
        assert resolve_context_window("nope/gpt-5.4", SAMPLE_PROVIDERS) is None

    def test_unknown_model_returns_none(self):
        assert resolve_context_window("openai/does-not-exist", SAMPLE_PROVIDERS) is None

    def test_model_without_limit_returns_none(self):
        assert resolve_context_window("openai/no-limit-model", SAMPLE_PROVIDERS) is None

    @pytest.mark.parametrize("bad", ["", None, "no-slash-here"])
    def test_bad_model_string_returns_none(self, bad):
        assert resolve_context_window(bad, SAMPLE_PROVIDERS) is None

    @pytest.mark.parametrize("bad_doc", [None, {}, {"providers": None}, "garbage", []])
    def test_bad_providers_doc_returns_none(self, bad_doc):
        assert resolve_context_window("openai/gpt-5.4", bad_doc) is None

    def test_zero_or_negative_context_returns_none(self):
        doc = {"providers": [{"id": "p", "models": {"m": {"limit": {"context": 0}}}}]}
        assert resolve_context_window("p/m", doc) is None
