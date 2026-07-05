"""The OpenCode message-body overrides built by ``_build_message_body``.

Locks the body builder shared by both send paths: the 🌐 web tools (+ read_pdf)
ride the per-request web toggle, the filesystem/shell tools are always off, the
model override is unchanged, and a caller system prompt becomes message ``system``.
"""
from workflow_editor.llm import LLMRequest, LLMTask, OpenCodeBackend, OpenCodeConfig

# EVERY OpenCode built-in is forced off on every editor request — the LLM gets no
# filesystem (read/glob/grep/list), shell (bash), file-write (edit/write/patch),
# or sub-agent (task) access; only the explicit MCP/web tools are enabled.
_BUILTIN_OFF = {
    "bash": False, "edit": False, "write": False, "patch": False,
    "apply_patch": False, "read": False, "glob": False, "grep": False,
    "list": False, "task": False, "todowrite": False, "todoread": False,
}

# The 7 project_tools (project_tools MCP server) keys, all OFF — the default
# (project_tools_enabled defaults False on LLMRequest).
_PROJECT_TOOLS_OFF = {
    "project_tools_list_property_fields": False,
    "project_tools_list_components": False,
    "project_tools_get_component": False,
    "project_tools_query_net": False,
    "project_tools_netlist": False,
    "project_tools_get_bom": False,
    "project_tools_list_test_points": False,
}

# Local document + rule tools — sandboxed, no network — are ALWAYS on (ungated).
_DOCS_ALWAYS_ON = {
    "pdf_tools_list_documents": True, "pdf_tools_read_document": True,
    "pdf_tools_list_rules": True, "pdf_tools_read_rule": True,
}

# run_skill (recursion infra, #15) rides its own per-request bool; fail-closed
# default = off on every ordinary request.
_RUN_SKILL_OFF = {"run_skill_run_skill": False}


def _backend(model: str = "") -> OpenCodeBackend:
    return OpenCodeBackend(OpenCodeConfig(model=model))


def _req(web_enabled: bool) -> LLMRequest:
    return LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi", web_enabled=web_enabled
    )


def test_web_enabled_exposes_web_tools_and_read_pdf():
    # skill_tools empty by default -> no skill-owned keys in the body.
    body = _backend()._build_message_body("hi", _req(web_enabled=True))
    assert body["tools"] == {
        **_BUILTIN_OFF, **_PROJECT_TOOLS_OFF, **_DOCS_ALWAYS_ON, **_RUN_SKILL_OFF,
        "webfetch": True, "websearch": True, "pdf_tools_read_pdf": True,
        "pdf_tools_save_pdf": False}


def test_web_disabled_sends_explicit_false():
    # Off must be explicit — relying on the server default would let a toggle-off
    # skill keep web access if the launch config/agent enabled it.
    body = _backend()._build_message_body("hi", _req(web_enabled=False))
    assert body["tools"] == {
        **_BUILTIN_OFF, **_PROJECT_TOOLS_OFF, **_DOCS_ALWAYS_ON, **_RUN_SKILL_OFF,
        "webfetch": False, "websearch": False, "pdf_tools_read_pdf": False,
        "pdf_tools_save_pdf": False}


def test_default_request_has_web_off():
    body = _backend()._build_message_body("hi", LLMRequest(task=LLMTask.AD_HOC_CHAT))
    assert body["tools"] == {
        **_BUILTIN_OFF, **_PROJECT_TOOLS_OFF, **_DOCS_ALWAYS_ON, **_RUN_SKILL_OFF,
        "webfetch": False, "websearch": False, "pdf_tools_read_pdf": False,
        "pdf_tools_save_pdf": False}


def test_project_tools_enabled_exposes_the_seven_tools():
    req = LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi", project_tools_enabled=True)
    body = _backend()._build_message_body("hi", req)
    for key in _PROJECT_TOOLS_OFF:
        assert body["tools"][key] is True
    # The project-tools toggle is independent of the web toggle.
    assert body["tools"]["webfetch"] is False


def test_project_tools_default_is_off():
    body = _backend()._build_message_body("hi", LLMRequest(task=LLMTask.AD_HOC_CHAT))
    for key in _PROJECT_TOOLS_OFF:
        assert body["tools"][key] is False


def test_dcdc_tools_enabled_exposes_the_generator_tool():
    # Backend must have skill_tools set so the key is emitted at all.
    be = OpenCodeBackend(OpenCodeConfig(skill_tools={"dcdc_tools": ["generate_dcdc_test"]}))
    # ON: skill_servers_enabled declares dcdc_tools -> key is True.
    req = LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi",
        skill_servers_enabled=["dcdc_tools"])
    body = be._build_message_body("hi", req)
    assert body["tools"]["dcdc_tools_generate_dcdc_test"] is True
    # independent of other toggles
    assert body["tools"]["webfetch"] is False
    assert body["tools"]["project_tools_list_components"] is False
    # OFF: skill_servers_enabled empty (default) -> key present but False.
    req_off = LLMRequest(task=LLMTask.AD_HOC_CHAT, raw_prompt="hi")
    body_off = be._build_message_body("hi", req_off)
    assert body_off["tools"]["dcdc_tools_generate_dcdc_test"] is False


def test_save_pdf_rides_web_and_save_docs():
    be = _backend()
    # save_pdf WRITES a file, so it requires BOTH web (it fetches) and the
    # per-chat save-docs toggle.
    on = be._build_message_body("hi", LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi",
        web_enabled=True, save_docs_enabled=True))
    assert on["tools"]["pdf_tools_save_pdf"] is True
    # save_docs without web -> still off (it can't fetch).
    no_web = be._build_message_body("hi", LLMRequest(
        task=LLMTask.AD_HOC_CHAT, raw_prompt="hi",
        web_enabled=False, save_docs_enabled=True))
    assert no_web["tools"]["pdf_tools_save_pdf"] is False
    # web alone (save_docs defaults off) -> off; read_pdf still on.
    web_only = be._build_message_body("hi", _req(web_enabled=True))
    assert web_only["tools"]["pdf_tools_save_pdf"] is False
    assert web_only["tools"]["pdf_tools_read_pdf"] is True


def test_dcdc_tools_default_is_off():
    # With no skill_tools configured the key is absent (= also off).
    body = _backend()._build_message_body("hi", LLMRequest(task=LLMTask.AD_HOC_CHAT))
    assert body["tools"].get("dcdc_tools_generate_dcdc_test") is not True


def test_local_document_and_rule_tools_always_on():
    # list/read for documents AND rules are local + sandboxed, so they're
    # available even with web OFF; read_pdf (the URL fetcher) stays web-gated.
    for web in (True, False):
        body = _backend()._build_message_body("hi", _req(web_enabled=web))
        for tool in _DOCS_ALWAYS_ON:
            assert body["tools"][tool] is True
    off = _backend()._build_message_body("hi", _req(web_enabled=False))
    assert off["tools"]["pdf_tools_read_pdf"] is False


def test_builtin_tools_always_disabled():
    # The editor's LLM gets NO filesystem/shell/file-write/sub-agent tool, EVER —
    # regardless of the web toggle. read/glob/grep/list/task are the dangerous
    # ones that previously let it wander the user's disk.
    for web in (True, False):
        body = _backend()._build_message_body("hi", _req(web_enabled=web))
        for tool in _BUILTIN_OFF:
            assert body["tools"][tool] is False
    # The real write-via-diff tool is `patch` (not just `apply_patch`).
    assert _backend()._build_message_body("hi", _req(web_enabled=True))["tools"]["patch"] is False


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
