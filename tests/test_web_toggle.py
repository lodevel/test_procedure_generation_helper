"""The OpenCode message-body overrides built by ``_build_message_body``.

Locks the body builder shared by both send paths: the 🌐 web tools (+ read_pdf)
ride the per-request web toggle, the filesystem/shell tools are always off, the
model override is unchanged, and a caller system prompt becomes message ``system``.
"""
from workflow_editor.llm import LLMRequest, LLMTask, OpenCodeBackend, OpenCodeConfig

# Filesystem/shell tools are disabled on every editor request.
_CODING_OFF = {"edit": False, "write": False, "bash": False, "apply_patch": False}


def _backend(model: str = "") -> OpenCodeBackend:
    return OpenCodeBackend(OpenCodeConfig(model=model))


def _req(web_enabled: bool) -> LLMRequest:
    return LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi", web_enabled=web_enabled
    )


def test_web_enabled_exposes_web_tools_and_read_pdf():
    body = _backend()._build_message_body("hi", _req(web_enabled=True))
    assert body["tools"] == {
        **_CODING_OFF,
        "webfetch": True, "websearch": True, "pdf_tools_read_pdf": True}


def test_web_disabled_sends_explicit_false():
    # Off must be explicit — relying on the server default would let a toggle-off
    # skill keep web access if the launch config/agent enabled it.
    body = _backend()._build_message_body("hi", _req(web_enabled=False))
    assert body["tools"] == {
        **_CODING_OFF,
        "webfetch": False, "websearch": False, "pdf_tools_read_pdf": False}


def test_default_request_has_web_off():
    body = _backend()._build_message_body("hi", LLMRequest(task=LLMTask.AD_HOC_CHAT))
    assert body["tools"] == {
        **_CODING_OFF,
        "webfetch": False, "websearch": False, "pdf_tools_read_pdf": False}


def test_coding_tools_always_disabled():
    # The editor's LLM never edits the filesystem, regardless of the web toggle.
    for web in (True, False):
        body = _backend()._build_message_body("hi", _req(web_enabled=web))
        for tool in ("edit", "write", "bash", "apply_patch"):
            assert body["tools"][tool] is False


def test_tools_override_does_not_disturb_model_override():
    body = _backend(model="openai/gpt-5.5")._build_message_body(
        "hi", _req(web_enabled=True)
    )
    assert body["model"] == {"providerID": "openai", "modelID": "gpt-5.5"}
    assert body["tools"]["webfetch"] is True
    assert body["parts"] == [{"type": "text", "text": "hi"}]


def test_no_model_means_no_model_key():
    body = _backend()._build_message_body("hi", _req(web_enabled=True))
    assert "model" not in body


def test_system_prompt_becomes_message_system():
    req = LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi",
        system_prompt="You are the skill.")
    body = _backend()._build_message_body("hi", req)
    assert body["system"] == "You are the skill."


def test_no_system_prompt_means_no_system_key():
    body = _backend()._build_message_body("hi", _req(web_enabled=True))
    assert "system" not in body
