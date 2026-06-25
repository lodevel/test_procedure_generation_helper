"""``_extract_token_usage`` must count the CACHED prompt (cache.read), not just
the uncached delta — else a big cached context reads tiny — and prefer
OpenCode's own ``info.tokens.total`` when it's larger.
"""
from workflow_editor.llm.backend_base import NoneBackend


def _extract(data):
    return NoneBackend()._extract_token_usage(data)


def test_cache_read_counted_in_prompt_and_total():
    # Small uncached input, big cached read (the real-world skill-chat case).
    data = {"info": {"tokens": {"input": 200, "output": 50, "reasoning": 0,
                                "cache": {"read": 30000, "write": 0}}}}
    prompt, completion, total = _extract(data)
    assert prompt == 30200          # input + cache.read (the real prompt size)
    assert completion == 50
    assert total == 30250           # input + cache.read + output


def test_prefers_server_total_when_larger():
    data = {"info": {"tokens": {"input": 100, "output": 20, "reasoning": 5,
                                "cache": {"read": 0}, "total": 999}}}
    _, _, total = _extract(data)
    assert total == 999


def test_reconstruct_beats_a_cache_omitting_total():
    # A server total that forgot the cache must not under-count: we take max.
    data = {"info": {"tokens": {"input": 100, "output": 20, "reasoning": 0,
                                "cache": {"read": 5000}, "total": 120}}}
    prompt, _, total = _extract(data)
    assert prompt == 5100
    assert total == 5120


def test_missing_cache_key_is_safe():
    data = {"info": {"tokens": {"input": 10, "output": 5}}}
    prompt, completion, total = _extract(data)
    assert (prompt, completion, total) == (10, 5, 15)


def test_openai_format_unchanged():
    data = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    assert _extract(data) == (10, 5, 15)
