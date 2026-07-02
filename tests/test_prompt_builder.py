"""Tests for PromptBuilder (task #22: capability-gated task prompts).

The editor ships NO task prompt text. Task prompts come exclusively from
the effective bundle/project config (TaskConfigManager). A task without a
non-empty prompt_template is NOT invocable: PromptBuilder raises
TaskPromptNotDeclaredError instead of silently substituting text.
AD_HOC_CHAT is the one grammar-neutral, editor-native exception.
"""
import pytest
from workflow_editor.llm import (
    PromptBuilder,
    LLMTask,
    LLMRequest,
    NoneBackend,
    TaskPromptNotDeclaredError,
)
from workflow_editor.llm.prompt_builder import AD_HOC_CHAT_DEFAULT_PROMPT
from workflow_editor.core.task_config import ChatConfig, TaskConfig, TaskConfigManager


def _manager_with_prompt(tmp_path, tab_id, task_id, prompt):
    """TaskConfigManager (legacy single-file mode) with one declared prompt."""
    manager = TaskConfigManager(tmp_path / "cfg.json")
    assert manager.update_task_config(
        tab_id=tab_id, task_id=task_id, prompt_template=prompt, enabled=True
    )
    return manager


def _request(task, **overrides):
    kwargs = dict(
        task=task, strict_mode=True,
        procedure_json=None, test_code=None, procedure_text=None,
        rules_content=None, session_summary=None, user_message=None,
    )
    kwargs.update(overrides)
    return LLMRequest(**kwargs)


class TestNoEditorBakedPrompts:
    """The grammar-opinionated editor defaults are gone for good."""

    def test_default_prompt_dicts_deleted(self):
        assert not hasattr(PromptBuilder, "DEFAULT_PROMPTS")
        assert not hasattr(PromptBuilder, "DEFAULT_TASK_INSTRUCTIONS")
        assert not hasattr(PromptBuilder, "get_default_prompts")

    def test_output_format_survives(self):
        # The output format is grammar-neutral plumbing and stays.
        fmt = PromptBuilder.get_default_output_format()
        assert "llm_turn" in fmt
        assert "proposals" in fmt


class TestUndeclaredTaskRaises:
    """A task whose prompt_template is not declared by the effective
    config must fail loudly — never run with substituted text."""

    def test_no_manager_raises(self):
        builder = PromptBuilder()
        with pytest.raises(TaskPromptNotDeclaredError):
            builder.build(_request(LLMTask.GENERATE_CODE_FROM_JSON))

    def test_manager_without_template_raises(self, tmp_path):
        # Fresh manager: editor defaults declare the task ids but ship
        # prompt_template=null everywhere -> not invocable.
        manager = TaskConfigManager(tmp_path / "cfg.json")
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        with pytest.raises(TaskPromptNotDeclaredError):
            builder.build(_request(LLMTask.DERIVE_JSON_FROM_TEXT))

    def test_blank_template_raises(self, tmp_path):
        manager = _manager_with_prompt(
            tmp_path, "text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value, "   \n  "
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        with pytest.raises(TaskPromptNotDeclaredError):
            builder.build(_request(LLMTask.DERIVE_JSON_FROM_TEXT))

    def test_error_message_is_clear(self):
        builder = PromptBuilder()
        with pytest.raises(TaskPromptNotDeclaredError, match="not declared by the active bundle"):
            builder.build(_request(LLMTask.REVIEW_JSON))


class TestDeclaredTaskResolves:
    """Declared prompt_template drives the built prompt."""

    def test_config_prompt_in_built_prompt(self, tmp_path):
        custom = "CUSTOM TEST PROMPT: Do something special"
        manager = _manager_with_prompt(
            tmp_path, "text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value, custom
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        prompt = builder.build(
            _request(LLMTask.DERIVE_JSON_FROM_TEXT, procedure_text="Sample text")
        )
        assert custom in prompt
        assert "response format" in prompt.lower()
        assert "proposals" in prompt

    def test_strict_mode_in_prompt(self, tmp_path):
        manager = _manager_with_prompt(
            tmp_path, "json_code", LLMTask.GENERATE_CODE_FROM_JSON.value, "GEN CODE"
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="json_code")
        prompt = builder.build(
            _request(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True, procedure_json="{}")
        )
        assert "strict" in prompt.lower()

    def test_force_mode_in_prompt(self, tmp_path):
        manager = _manager_with_prompt(
            tmp_path, "json_code", LLMTask.GENERATE_CODE_FROM_JSON.value, "GEN CODE"
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="json_code")
        prompt = builder.build(
            _request(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False, procedure_json="{}")
        )
        assert "force" in prompt.lower()


class TestPromptBuilderArtifacts:
    """Artifact/session/rules sections (grammar-neutral plumbing).

    Uses AD_HOC_CHAT so no declared task prompt is needed."""

    def test_json_artifact_in_prompt(self, sample_json_artifact):
        builder = PromptBuilder()
        prompt = builder.build(
            _request(LLMTask.AD_HOC_CHAT, procedure_json=sample_json_artifact)
        )
        assert "```json" in prompt
        assert sample_json_artifact in prompt

    def test_session_context_included(self):
        builder = PromptBuilder()
        prompt = builder.build(
            _request(LLMTask.AD_HOC_CHAT, session_summary="Previous: LED test")
        )
        assert "Previous: LED test" in prompt

    def test_rules_included(self):
        builder = PromptBuilder()
        prompt = builder.build(
            _request(LLMTask.AD_HOC_CHAT, rules_content="Rule 1: Use fixtures")
        )
        assert "Rule 1: Use fixtures" in prompt


class TestDeprecatedCustomPromptsDict:
    """Backward compatibility: the deprecated custom_prompts dict still
    resolves, and the config layer still wins over it."""

    def test_custom_prompts_dict_resolves(self):
        custom_prompt = "LEGACY CUSTOM PROMPT"
        builder = PromptBuilder(
            custom_prompts={LLMTask.GENERATE_CODE_FROM_JSON.value: custom_prompt}
        )
        prompt = builder.build(
            _request(LLMTask.GENERATE_CODE_FROM_JSON, procedure_json="{}")
        )
        assert custom_prompt in prompt

    def test_priority_order_task_config_over_custom_prompts(self, tmp_path):
        tcm_prompt = "FROM TASK CONFIG MANAGER"
        manager = _manager_with_prompt(
            tmp_path, "json_code", LLMTask.GENERATE_CODE_FROM_JSON.value, tcm_prompt
        )
        legacy_prompt = "FROM LEGACY CUSTOM PROMPTS"
        builder = PromptBuilder(
            task_config_manager=manager,
            tab_id="json_code",
            custom_prompts={LLMTask.GENERATE_CODE_FROM_JSON.value: legacy_prompt},
        )
        prompt = builder.build(
            _request(LLMTask.GENERATE_CODE_FROM_JSON, procedure_json="{}")
        )
        assert tcm_prompt in prompt
        assert legacy_prompt not in prompt

    def test_task_config_manager_without_tab_id(self, tmp_path):
        manager = TaskConfigManager(tmp_path / "cfg.json")
        builder = PromptBuilder(task_config_manager=manager, tab_id=None)
        assert builder._tab_id is None
        # Without a tab_id the manager is never queried -> undeclared.
        with pytest.raises(TaskPromptNotDeclaredError):
            builder.build(_request(LLMTask.DERIVE_JSON_FROM_TEXT))


class TestAdHocChatEditorNative:
    """AD_HOC_CHAT stays editor-native (grammar-neutral): chat works with
    no bundle at all, and chat_config.system_prompt overrides."""

    def test_chat_resolves_without_any_config(self):
        # The settings-dialog Test Connection path: bare backend builder,
        # no manager. Must not raise.
        builder = PromptBuilder()
        prompt = builder.build(_request(LLMTask.AD_HOC_CHAT, user_message="hi"))
        assert "Respond CONSERVATIVELY" in prompt

    def test_chat_config_system_prompt_wins_over_default(self, tmp_path):
        manager = TaskConfigManager(tmp_path / "fallback.json")
        custom = "BE TERSE. Only answer the user's literal question."
        manager.set_chat_config(
            "text_only", ChatConfig(enabled=True, system_prompt=custom)
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_only")
        prompt = builder.build(
            _request(LLMTask.AD_HOC_CHAT, strict_mode=False, user_message="hi")
        )
        assert custom in prompt
        assert "Respond CONSERVATIVELY" not in prompt

    def test_chat_config_blank_falls_through_to_default(self, tmp_path):
        manager = TaskConfigManager(tmp_path / "fallback.json")
        manager.set_chat_config(
            "text_only", ChatConfig(enabled=True, system_prompt=None)
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_only")
        prompt = builder.build(
            _request(LLMTask.AD_HOC_CHAT, strict_mode=False, user_message="hello")
        )
        assert "Respond CONSERVATIVELY" in prompt

    def test_chat_config_does_not_affect_non_chat_tasks(self, tmp_path):
        manager = _manager_with_prompt(
            tmp_path, "text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value, "TASK TEXT"
        )
        manager.set_chat_config(
            "text_json", ChatConfig(enabled=True, system_prompt="CHAT ONLY")
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        prompt = builder.build(
            _request(LLMTask.DERIVE_JSON_FROM_TEXT, procedure_text="Step 1: do X")
        )
        assert "CHAT ONLY" not in prompt
        assert "TASK TEXT" in prompt

    def test_default_prompt_forbids_unsolicited_proposals(self):
        """Regression: typing 'test message' must not trigger an
        unsolicited full procedure review."""
        lowered = AD_HOC_CHAT_DEFAULT_PROMPT.lower()
        assert "conservatively" in lowered
        assert "explicitly" in lowered
        assert "clarif" in lowered
        assert "proposal" in lowered


class TestCustomTaskEffectiveId:
    """A bundle-declared CUSTOM task (id not in the LLMTask enum) routes
    via AD_HOC_CHAT but resolves ITS OWN prompt_template by
    request.custom_task_id — never the chat default chain. An undeclared
    custom id fails loudly (gpt-5.5 finding)."""

    @staticmethod
    def _manager_with_custom_task(tmp_path, tab_id, task_id, prompt):
        manager = TaskConfigManager(tmp_path / "cfg.json")
        assert manager.add_task(
            tab_id, TaskConfig(id=task_id, name="Custom", button_label="Custom",
                        prompt_template=prompt)
        )
        return manager

    def test_declared_custom_task_prompt_resolves(self, tmp_path):
        custom = "BUNDLE CUSTOM TASK PROMPT: verify the shunt wiring"
        manager = self._manager_with_custom_task(
            tmp_path, "text_json", "verify_shunt", custom
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        prompt = builder.build(
            _request(LLMTask.AD_HOC_CHAT, custom_task_id="verify_shunt")
        )
        assert custom in prompt
        assert "Respond CONSERVATIVELY" not in prompt

    def test_undeclared_custom_id_raises(self, tmp_path):
        manager = TaskConfigManager(tmp_path / "cfg.json")
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        with pytest.raises(TaskPromptNotDeclaredError, match="undeclared_custom"):
            builder.build(
                _request(LLMTask.AD_HOC_CHAT, custom_task_id="undeclared_custom")
            )

    def test_custom_id_never_falls_back_to_chat_config(self, tmp_path):
        # chat_config.system_prompt belongs to the PLAIN chat; an
        # undeclared custom id must raise, not inherit chat text.
        manager = TaskConfigManager(tmp_path / "cfg.json")
        manager.set_chat_config(
            "text_json", ChatConfig(enabled=True, system_prompt="CHAT ONLY")
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        with pytest.raises(TaskPromptNotDeclaredError):
            builder.build(
                _request(LLMTask.AD_HOC_CHAT, custom_task_id="undeclared_custom")
            )

    def test_custom_id_without_manager_raises(self):
        # Backend's config-less builder can never resolve a custom task.
        builder = PromptBuilder()
        with pytest.raises(TaskPromptNotDeclaredError):
            builder.build(_request(LLMTask.AD_HOC_CHAT, custom_task_id="anything"))

    def test_plain_chat_unaffected(self, tmp_path):
        # No custom_task_id -> the editor-native chat default still resolves,
        # untouched by declared custom tasks on the same tab.
        manager = self._manager_with_custom_task(
            tmp_path, "text_json", "verify_shunt", "BUNDLE CUSTOM"
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        prompt = builder.build(_request(LLMTask.AD_HOC_CHAT, user_message="hi"))
        assert "Respond CONSERVATIVELY" in prompt
        assert "BUNDLE CUSTOM" not in prompt


class TestOutgoingPromptResolution:
    """Backend seam: raw_prompt > prebuilt_prompt > config-less build.

    The tab pipeline builds the prompt with a config-aware PromptBuilder
    and attaches it as request.prebuilt_prompt; the backend must send
    THAT text, never rebuild with its config-less builder."""

    def test_raw_prompt_wins(self):
        backend = NoneBackend()
        req = _request(LLMTask.AD_HOC_CHAT)
        req.raw_prompt = "RAW"
        req.prebuilt_prompt = "PREBUILT"
        assert backend._resolve_outgoing_prompt(req) == "RAW"

    def test_prebuilt_prompt_used_without_rebuild(self):
        backend = NoneBackend()
        # Undeclared task: a rebuild would raise, so returning the
        # prebuilt text proves no rebuild happens.
        req = _request(LLMTask.REVIEW_JSON)
        req.prebuilt_prompt = "PREBUILT TASK PROMPT"
        assert backend._resolve_outgoing_prompt(req) == "PREBUILT TASK PROMPT"

    def test_fallback_build_raises_for_undeclared_task(self):
        backend = NoneBackend()
        req = _request(LLMTask.REVIEW_JSON)
        with pytest.raises(TaskPromptNotDeclaredError):
            backend._resolve_outgoing_prompt(req)

    def test_fallback_build_resolves_ad_hoc_chat(self):
        backend = NoneBackend()
        req = _request(LLMTask.AD_HOC_CHAT, user_message="ping")
        prompt = backend._resolve_outgoing_prompt(req)
        assert "Respond CONSERVATIVELY" in prompt
        assert "ping" in prompt
