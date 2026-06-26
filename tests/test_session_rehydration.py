"""Lost-session rehydration tests for the OpenCode dock chat.

Covers the three pure/mockable seams of the rehydration feature:
  A) serialize_transcript  -- the text-only conversation-so-far preamble,
  B) OpenCodeBackend._is_session_not_found -- the lost-session detector,
  C) OpenCodeBackend._send_via_api -- detect + mint-fresh + replay + retry-ONCE.

All HTTP is mocked; the backend is built with ``object.__new__`` (bypassing
``__init__``) and only the touched attrs are set, so no Qt/pack is needed.
"""
import json

import pytest

from workflow_editor.llm import opencode_backend as ocb
from workflow_editor.llm.opencode_backend import OpenCodeBackend
from workflow_editor.llm.backend_base import LLMRequest, LLMResponse, LLMTask
from workflow_editor.llm.tab_context import ChatMessage
from workflow_editor.llm.transcript_replay import serialize_transcript


# --------------------------------------------------------------------------- #
# A) serialize_transcript (pure)                                               #
# --------------------------------------------------------------------------- #

class TestSerializeTranscript:
    def test_empty_list_returns_blank(self):
        assert serialize_transcript([], 10_000) == ""

    def test_nonpositive_budget_returns_blank(self):
        msgs = [ChatMessage(role="user", content="hello")]
        assert serialize_transcript(msgs, 0) == ""
        assert serialize_transcript(msgs, -5) == ""

    def test_all_whitespace_content_returns_blank(self):
        msgs = [
            ChatMessage(role="user", content="   "),
            ChatMessage(role="assistant", content="\n\t "),
        ]
        assert serialize_transcript(msgs, 10_000) == ""

    def test_role_labels(self):
        msgs = [
            ChatMessage(role="user", content="u1"),
            ChatMessage(role="assistant", content="a1"),
            ChatMessage(role="system", content="s1"),
        ]
        out = serialize_transcript(msgs, 10_000)
        assert "User: u1" in out
        assert "Assistant: a1" in out
        assert "Note: s1" in out
        # unknown role falls back to User
        out2 = serialize_transcript([ChatMessage(role="weird", content="x")], 10_000)
        assert "User: x" in out2

    def test_header_present(self):
        out = serialize_transcript([ChatMessage(role="user", content="hi")], 10_000)
        assert out.startswith("Conversation so far")
        # model-facing instruction: must tell the model to re-call the tools
        assert "re-call" in out.lower()

    def test_newest_first_survival_under_truncation(self):
        # 10 numbered messages; a tiny budget that fits only the last couple.
        msgs = [ChatMessage(role="user", content=f"message number {i:02d}") for i in range(10)]
        # budget room for header + ~2 short blocks
        budget = len(
            "Conversation so far, replayed after the previous server session was lost. "
            "The earlier tool results (netlist, BOM, datasheets) are GONE from this new "
            "session. Before relying on ANY component value, net name, or part number "
            "quoted below, you MUST re-call the relevant tools to re-verify it -- treat "
            "the quoted text as a memory aid, not as a trusted source:"
        ) + 2 + 2 * (len("User: message number 09") + 2)
        out = serialize_transcript(msgs, budget)
        # The LAST messages survive, the first do NOT.
        assert "message number 09" in out
        assert "message number 08" in out
        assert "message number 00" not in out
        assert "message number 01" not in out
        # Order is oldest-first among kept blocks: 08 appears before 09.
        assert out.index("message number 08") < out.index("message number 09")

    def test_content_only_never_leaks_full_prompt_or_response(self):
        msgs = [
            ChatMessage(
                role="user",
                content="visible question",
                full_prompt="SECRET_RULES_DUMP and netlist artifacts",
            ),
            ChatMessage(
                role="assistant",
                content="visible answer",
                full_response="SECRET_RAW_RESPONSE blob",
            ),
        ]
        out = serialize_transcript(msgs, 10_000)
        assert "visible question" in out
        assert "visible answer" in out
        assert "SECRET_RULES_DUMP" not in out
        assert "SECRET_RAW_RESPONSE" not in out


# --------------------------------------------------------------------------- #
# Shared fakes                                                                 #
# --------------------------------------------------------------------------- #

class FakeResponse:
    """Minimal stand-in for a ``requests`` Response."""

    def __init__(self, status_code, text="{}", json_obj=None, raise_json=False):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self._json_obj = json_obj
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        if self._json_obj is not None:
            return self._json_obj
        return json.loads(self.text)


def _not_found():
    return FakeResponse(404, text='{"name": "NotFoundError"}', json_obj={"name": "NotFoundError"})


# --------------------------------------------------------------------------- #
# B) _is_session_not_found (pure)                                              #
# --------------------------------------------------------------------------- #

class TestSessionLostDetection:
    def test_404_notfounderror_is_true(self):
        assert OpenCodeBackend._is_session_not_found(_not_found()) is True

    def test_404_other_name_is_false(self):
        r = FakeResponse(404, json_obj={"name": "BadRequest"})
        assert OpenCodeBackend._is_session_not_found(r) is False

    def test_200_is_false(self):
        r = FakeResponse(200, json_obj={"name": "NotFoundError"})
        assert OpenCodeBackend._is_session_not_found(r) is False

    def test_404_non_json_is_false(self):
        r = FakeResponse(404, raise_json=True)
        assert OpenCodeBackend._is_session_not_found(r) is False


# --------------------------------------------------------------------------- #
# C) _send_via_api detect + rehydrate + retry-ONCE                             #
# --------------------------------------------------------------------------- #

class _FakeParser:
    def parse(self, raw, task, plain_text=False):
        return LLMResponse(success=True, assistant_message="ok")


def _make_backend(create_session_returns="new"):
    """Build an OpenCodeBackend bypassing __init__; stub only what _send_via_api
    touches. Returns (backend, state) where state records calls."""
    from types import SimpleNamespace

    be = object.__new__(OpenCodeBackend)
    be.config = SimpleNamespace(server_url="http://x", request_timeout=30)
    be._session_id = "old"
    state = {"create_calls": 0, "bodies": [], "prompts": []}

    def _create_session():
        state["create_calls"] += 1
        return create_session_returns

    def _build_message_body(prompt, request):
        state["prompts"].append(prompt)
        return {"prompt": prompt}

    be._create_session = _create_session
    be._build_message_body = _build_message_body
    be._extract_token_usage = lambda data: (0, 0, 0)
    # _response_parser is a property on the base reading the mangled attr.
    setattr(be, "_LLMBackend__response_parser", _FakeParser())
    return be, state


def _patch_post(monkeypatch, responses):
    """Patch opencode_backend.requests.post to pop FakeResponses in order and
    record (url, json) per call. Returns the calls list."""
    calls = []
    seq = list(responses)

    def fake_post(url, json=None, timeout=None, **kw):
        calls.append({"url": url, "json": json})
        return seq.pop(0)

    monkeypatch.setattr(ocb.requests, "post", fake_post)
    return calls


class TestRehydrateRetry:
    def test_happy_path_mints_replays_and_succeeds(self, monkeypatch):
        be, state = _make_backend(create_session_returns="new")
        calls = _patch_post(monkeypatch, [_not_found(), FakeResponse(200, text="{}")])
        request = LLMRequest(task=LLMTask.AD_HOC_CHAT)
        request.conversation_preamble = "PREAMBLE"

        resp = be._send_via_api("PROMPT", request)

        assert resp.success is True
        assert resp.session_rehydrated is True
        assert be._session_id == "new"
        assert request.conversation_preamble is None  # consumed
        assert state["create_calls"] == 1
        # retry POST hit the NEW session
        assert "/session/new/message" in calls[1]["url"]
        # the merged text carried BOTH the preamble and the prompt
        merged = state["prompts"][-1]
        assert "PREAMBLE" in merged and "PROMPT" in merged

    def test_anti_loop_second_404_returns_error_no_second_mint(self, monkeypatch):
        be, state = _make_backend(create_session_returns="new")
        _patch_post(monkeypatch, [_not_found(), _not_found()])
        request = LLMRequest(task=LLMTask.AD_HOC_CHAT)
        request.conversation_preamble = "PREAMBLE"

        resp = be._send_via_api("PROMPT", request)

        assert state["create_calls"] == 1  # minted exactly once
        assert resp.success is False
        assert "404" in resp.error_message
        assert resp.session_rehydrated is True

    def test_no_preamble_no_rehydration(self, monkeypatch):
        be, state = _make_backend()
        _patch_post(monkeypatch, [_not_found()])
        request = LLMRequest(task=LLMTask.AD_HOC_CHAT)
        request.conversation_preamble = None

        resp = be._send_via_api("PROMPT", request)

        assert state["create_calls"] == 0  # never minted
        assert resp.success is False
        assert "404" in resp.error_message
        assert resp.session_rehydrated is False

    def test_non_404_failure_is_not_rehydrated(self, monkeypatch):
        be, state = _make_backend()
        _patch_post(monkeypatch, [FakeResponse(500, text="server error")])
        request = LLMRequest(task=LLMTask.AD_HOC_CHAT)
        request.conversation_preamble = "PREAMBLE"

        resp = be._send_via_api("PROMPT", request)

        assert state["create_calls"] == 0
        assert resp.success is False
        assert "500" in resp.error_message
        assert resp.session_rehydrated is False
        # preamble untouched (rehydration never armed)
        assert request.conversation_preamble == "PREAMBLE"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
