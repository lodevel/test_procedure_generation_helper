"""The skill chat's 🌐 web toggle rides a per-request message-body override.

These lock the body builder shared by both OpenCode send paths: web tools are
sent EXPLICITLY on/off (so the model gets no web access unless the user opts in)
and the model override is unchanged.
"""
from workflow_editor.llm import LLMRequest, LLMTask, OpenCodeBackend, OpenCodeConfig


def _backend(model: str = "") -> OpenCodeBackend:
    return OpenCodeBackend(OpenCodeConfig(model=model))


def _req(web_enabled: bool) -> LLMRequest:
    return LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi", web_enabled=web_enabled
    )


def test_web_enabled_exposes_both_web_tools():
    body = _backend()._build_message_body("hi", _req(web_enabled=True))
    assert body["tools"] == {
        "webfetch": True, "websearch": True, "pdf_tools_read_pdf": True}


def test_web_disabled_sends_explicit_false():
    # Off must be explicit — relying on the server default would let a toggle-off
    # skill keep web access if the launch config/agent enabled it.
    body = _backend()._build_message_body("hi", _req(web_enabled=False))
    assert body["tools"] == {
        "webfetch": False, "websearch": False, "pdf_tools_read_pdf": False}


def test_default_request_has_web_off():
    body = _backend()._build_message_body("hi", LLMRequest(task=LLMTask.AD_HOC_CHAT))
    assert body["tools"] == {
        "webfetch": False, "websearch": False, "pdf_tools_read_pdf": False}


def test_tools_override_does_not_disturb_model_override():
    body = _backend(model="openai/gpt-5.5")._build_message_body(
        "hi", _req(web_enabled=True)
    )
    assert body["model"] == {"providerID": "openai", "modelID": "gpt-5.5"}
    assert body["tools"] == {
        "webfetch": True, "websearch": True, "pdf_tools_read_pdf": True}
    assert body["parts"] == [{"type": "text", "text": "hi"}]


def test_no_model_means_no_model_key():
    body = _backend()._build_message_body("hi", _req(web_enabled=True))
    assert "model" not in body
