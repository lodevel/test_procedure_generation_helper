"""``_turn_peak_token_usage`` must reflect the PEAK prompt context across the
whole agentic turn, not just the final assistant message.

A tool-using turn produces multiple assistant messages: N intermediate
``finish:"tool-calls"`` steps then one final ``finish:"stop"`` message. For
providers that don't populate ``cache.read`` (vLLM/gemma) the big tool-result
prompt is billed on an INTERMEDIATE tool-calls message and the final stop
reports only a tiny delta — so reading only the last message collapses the
context readout to near-zero. The peak across the turn's steps is the real
context the model saw.
"""
from workflow_editor.llm.opencode_backend import OpenCodeBackend


def _msg(role, *, input=0, output=0, cache_read=0, total=None):
    tokens = {"input": input, "output": output, "reasoning": 0,
              "cache": {"read": cache_read, "write": 0}}
    if total is not None:
        tokens["total"] = total
    return {"info": {"role": role, "tokens": tokens}}


def _peak(messages):
    be = OpenCodeBackend()
    return be._turn_peak_token_usage(messages, messages[-1])


def test_vllm_intermediate_tool_call_message_carries_the_peak():
    # vLLM/gemma: no cache.read. The big tool-result prompt is billed on the
    # intermediate tool-calls message; the final stop reports a tiny delta.
    messages = [
        _msg("user", input=500),
        _msg("assistant", input=30000, output=80, cache_read=0),  # tool-calls step
        _msg("assistant", input=120, output=40, cache_read=0),    # final stop
    ]
    prompt, completion, total = _peak(messages)
    assert prompt == 30000          # peak prompt context, not the 120 delta
    assert total == 30080


def test_openai_peak_is_the_final_stop_message_unchanged():
    # OpenAI: the tool result lands in cache.read on the FINAL stop message,
    # so the peak == the last message (behaviour unchanged).
    messages = [
        _msg("user", input=500),
        _msg("assistant", input=500, output=80, cache_read=0),       # tool-calls step
        _msg("assistant", input=200, output=40, cache_read=30000),   # final stop
    ]
    prompt, completion, total = _peak(messages)
    assert prompt == 30200          # input + cache.read on the final message
    assert completion == 40
    assert total == 30240


def test_single_message_turn_unchanged():
    # A plain (no-tool) turn: one assistant message after the user. The peak is
    # exactly that message — identical to reading last_assistant directly.
    messages = [
        _msg("user", input=300),
        _msg("assistant", input=300, output=50, cache_read=0),
    ]
    assert _peak(messages) == (300, 50, 350)


def test_does_not_cross_the_turn_boundary():
    # A prior turn's huge assistant message must NOT leak into this turn's peak:
    # the walk stops at the most recent user message.
    messages = [
        _msg("assistant", input=99999, output=10, cache_read=0),  # PRIOR turn
        _msg("user", input=400),                                  # this turn starts
        _msg("assistant", input=600, output=30, cache_read=0),
    ]
    prompt, completion, total = _peak(messages)
    assert prompt == 600            # not 99999 from the prior turn
    assert total == 630


def test_falls_back_to_last_assistant_when_no_assistant_in_turn():
    # The turn boundary (user) is hit before any assistant message is seen
    # (e.g. a freshly-started turn): fall back to the supplied last_assistant
    # rather than returning zero.
    be = OpenCodeBackend()
    messages = [_msg("user", input=400)]
    last_assistant = _msg("assistant", input=700, output=20, cache_read=0)
    prompt, completion, total = be._turn_peak_token_usage(messages, last_assistant)
    assert prompt == 700
    assert total == 720
