"""Tests for PromptBuilder."""
import pytest
from pathlib import Path
from workflow_editor.llm import PromptBuilder, LLMTask, LLMRequest
from workflow_editor.core.task_config import ChatConfig, TaskConfigManager, TaskConfig

class TestPromptBuilderInstructions:
    def test_all_tasks_have_instructions(self):
        builder = PromptBuilder()
        for task in LLMTask:
            assert task in builder.DEFAULT_PROMPTS
            instruction = builder.DEFAULT_PROMPTS[task]
            assert len(instruction.strip()) > 50
    
    def test_output_format_in_prompt(self, sample_llm_request_minimal):
        builder = PromptBuilder()
        prompt = builder.build(sample_llm_request_minimal)
        assert 'response format' in prompt.lower()
        assert 'proposals' in prompt

class TestPromptBuilderStructure:
    def test_minimal_prompt(self, sample_llm_request_minimal):
        builder = PromptBuilder()
        prompt = builder.build(sample_llm_request_minimal)
        assert builder.DEFAULT_PROMPTS[LLMTask.GENERATE_CODE_FROM_JSON] in prompt
        assert 'response format' in prompt.lower()
    
    def test_strict_mode_in_prompt(self):
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True,
            procedure_json='{}', test_code=None, procedure_text=None,
            rules_content=None, session_summary=None, user_message=None
        )
        builder = PromptBuilder()
        prompt = builder.build(request)
        assert 'strict' in prompt.lower()
    
    def test_force_mode_in_prompt(self):
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False,
            procedure_json='{}', test_code=None, procedure_text=None,
            rules_content=None, session_summary=None, user_message=None
        )
        builder = PromptBuilder()
        prompt = builder.build(request)
        assert 'force' in prompt.lower()

class TestPromptBuilderArtifacts:
    def test_json_artifact_in_prompt(self, sample_json_artifact):
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True,
            procedure_json=sample_json_artifact, test_code=None, procedure_text=None,
            rules_content=None, session_summary=None, user_message=None
        )
        builder = PromptBuilder()
        prompt = builder.build(request)
        assert '```json' in prompt
        assert sample_json_artifact in prompt
    
    def test_session_context_included(self):
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True,
            procedure_json='{}', test_code=None, procedure_text=None,
            rules_content=None, session_summary='Previous: LED test', user_message=None
        )
        builder = PromptBuilder()
        prompt = builder.build(request)
        assert 'Previous: LED test' in prompt
    
    def test_rules_included(self):
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True,
            procedure_json='{}', test_code=None, procedure_text=None,
            rules_content='Rule 1: Use fixtures', session_summary=None, user_message=None
        )
        builder = PromptBuilder()
        prompt = builder.build(request)
        assert 'Rule 1: Use fixtures' in prompt

class TestPromptBuilderTaskConfigIntegration:
    """Test TaskConfigManager integration."""
    
    def test_custom_prompt_from_task_config_manager(self, tmp_path):
        """Test that custom prompts are loaded from TaskConfigManager."""
        # Setup a TaskConfigManager with a custom prompt
        config_file = tmp_path / "test_config.json"
        manager = TaskConfigManager(config_file)
        
        # Update a task with a custom prompt
        custom_prompt = "CUSTOM TEST PROMPT: Do something special"
        manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="Test Label",
            prompt_template=custom_prompt,
            enabled=True
        )
        
        # Create PromptBuilder with TaskConfigManager
        builder = PromptBuilder(
            task_config_manager=manager,
            tab_id="text_json"
        )
        
        # Build a request
        request = LLMRequest(
            task=LLMTask.DERIVE_JSON_FROM_TEXT,
            strict_mode=True,
            procedure_json=None,
            test_code=None,
            procedure_text="Sample text",
            rules_content=None,
            session_summary=None,
            user_message=None
        )
        
        prompt = builder.build(request)
        assert custom_prompt in prompt
        assert "CUSTOM TEST PROMPT" in prompt
    
    def test_fallback_to_default_when_no_custom_prompt(self, tmp_path):
        """Test that DEFAULT_PROMPTS is used when TaskConfig has no custom prompt."""
        config_file = tmp_path / "test_config.json"
        manager = TaskConfigManager(config_file)
        
        # Create PromptBuilder with TaskConfigManager
        builder = PromptBuilder(
            task_config_manager=manager,
            tab_id="text_json"
        )
        
        # Build a request for a task with no custom prompt
        request = LLMRequest(
            task=LLMTask.DERIVE_JSON_FROM_TEXT,
            strict_mode=True,
            procedure_json=None,
            test_code=None,
            procedure_text="Sample text",
            rules_content=None,
            session_summary=None,
            user_message=None
        )
        
        prompt = builder.build(request)
        
        # Should contain the default prompt
        default_prompt = builder.DEFAULT_PROMPTS[LLMTask.DERIVE_JSON_FROM_TEXT]
        assert default_prompt in prompt
    
    def test_backward_compatibility_with_custom_prompts_dict(self):
        """Test backward compatibility with deprecated custom_prompts parameter."""
        custom_prompt = "LEGACY CUSTOM PROMPT"
        builder = PromptBuilder(
            custom_prompts={
                LLMTask.GENERATE_CODE_FROM_JSON.value: custom_prompt
            }
        )
        
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON,
            strict_mode=True,
            procedure_json="{}",
            test_code=None,
            procedure_text=None,
            rules_content=None,
            session_summary=None,
            user_message=None
        )
        
        prompt = builder.build(request)
        assert custom_prompt in prompt
    
    def test_task_config_manager_without_tab_id_uses_defaults(self, tmp_path):
        """Test that providing task_config_manager without tab_id falls back to defaults."""
        config_file = tmp_path / "test_config.json"
        manager = TaskConfigManager(config_file)
        
        # Should not raise — tab_id=None means TaskConfigManager won't be queried
        builder = PromptBuilder(task_config_manager=manager, tab_id=None)
        assert builder._tab_id is None
    
    def test_priority_order_task_config_over_custom_prompts(self, tmp_path):
        """Test that TaskConfigManager takes priority over deprecated custom_prompts."""
        config_file = tmp_path / "test_config.json"
        manager = TaskConfigManager(config_file)
        
        # Set custom prompt in TaskConfigManager
        tcm_prompt = "FROM TASK CONFIG MANAGER"
        manager.update_task_config(
            tab_id="json_code",
            task_id=LLMTask.GENERATE_CODE_FROM_JSON.value,
            button_label="Test",
            prompt_template=tcm_prompt,
            enabled=True
        )
        
        # Also provide deprecated custom_prompts
        legacy_prompt = "FROM LEGACY CUSTOM PROMPTS"
        builder = PromptBuilder(
            task_config_manager=manager,
            tab_id="json_code",
            custom_prompts={
                LLMTask.GENERATE_CODE_FROM_JSON.value: legacy_prompt
            }
        )
        
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON,
            strict_mode=True,
            procedure_json="{}",
            test_code=None,
            procedure_text=None,
            rules_content=None,
            session_summary=None,
            user_message=None
        )
        
        prompt = builder.build(request)

        # Should use TaskConfigManager prompt, not legacy
        assert tcm_prompt in prompt
        assert legacy_prompt not in prompt


class TestPromptBuilderChatConfigWiring:
    """Phase 4.5: chat_config.system_prompt is actually consulted at
    runtime for AD_HOC_CHAT. Previously stored but not applied — a
    bug that should never have shipped."""

    def test_chat_config_system_prompt_wins_over_default(self, tmp_path):
        """When the tab's chat_config has a custom system_prompt, it
        replaces the DEFAULT_PROMPTS[AD_HOC_CHAT] text for AD_HOC_CHAT
        requests on that tab."""
        manager = TaskConfigManager(tmp_path / "fallback.json")
        custom = "BE TERSE. Only answer the user's literal question."
        manager.set_chat_config(
            "text_only",
            ChatConfig(enabled=True, system_prompt=custom),
        )

        builder = PromptBuilder(task_config_manager=manager, tab_id="text_only")
        request = LLMRequest(
            task=LLMTask.AD_HOC_CHAT, strict_mode=False,
            procedure_json=None, test_code=None, procedure_text=None,
            rules_content=None, session_summary=None,
            user_message="hi",
        )
        prompt = builder.build(request)
        assert custom in prompt
        # The default text shouldn't sneak in alongside.
        assert "Respond CONSERVATIVELY" not in prompt

    def test_chat_config_blank_falls_through_to_default(self, tmp_path):
        """system_prompt of None or empty string falls through to the
        DEFAULT_PROMPTS[AD_HOC_CHAT] text. Same intent as None in the
        task_template field."""
        manager = TaskConfigManager(tmp_path / "fallback.json")
        manager.set_chat_config(
            "text_only", ChatConfig(enabled=True, system_prompt=None),
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_only")
        request = LLMRequest(
            task=LLMTask.AD_HOC_CHAT, strict_mode=False,
            procedure_json=None, test_code=None, procedure_text=None,
            rules_content=None, session_summary=None,
            user_message="hello",
        )
        prompt = builder.build(request)
        # Default text surfaces.
        assert "Respond CONSERVATIVELY" in prompt

    def test_chat_config_does_not_affect_non_chat_tasks(self, tmp_path):
        """Setting chat_config.system_prompt for a tab must NOT
        override the prompts of non-chat tasks on that tab."""
        manager = TaskConfigManager(tmp_path / "fallback.json")
        manager.set_chat_config(
            "text_json",
            ChatConfig(enabled=True, system_prompt="CHAT ONLY"),
        )
        builder = PromptBuilder(task_config_manager=manager, tab_id="text_json")
        request = LLMRequest(
            task=LLMTask.DERIVE_JSON_FROM_TEXT, strict_mode=True,
            procedure_json=None, test_code=None,
            procedure_text="Step 1: do X", rules_content=None,
            session_summary=None, user_message=None,
        )
        prompt = builder.build(request)
        assert "CHAT ONLY" not in prompt
        # Default DERIVE_JSON prompt body is present (TaskConfigManager
        # has the baked editor default for it).
        assert "Derive procedure.json" in prompt


class TestAdHocChatPromptIsConservative:
    """The default AD_HOC_CHAT prompt must instruct the LLM NOT to
    proactively review or propose. Regression for the bug where typing
    'test message' triggered an unsolicited full procedure review."""

    def test_default_prompt_forbids_unsolicited_proposals(self):
        prompt = PromptBuilder.DEFAULT_PROMPTS[LLMTask.AD_HOC_CHAT]
        lowered = prompt.lower()
        assert "conservatively" in lowered
        assert "explicitly" in lowered
        # Mentions the clarifying-question fallback for ambiguous input.
        assert "clarif" in lowered
        # Mentions the never-include-a-proposal rule.
        assert "proposal" in lowered