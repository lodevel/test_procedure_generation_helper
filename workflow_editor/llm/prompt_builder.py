"""
Prompt Builder - Builds prompts for LLM tasks.

Assembles the prompt with:
- Task instruction
- Artifacts (JSON, code, text)
- Session summary
- Rules content
- Output format requirements
"""

import logging
from typing import Optional, Dict, TYPE_CHECKING
from .backend_base import LLMRequest, LLMTask

if TYPE_CHECKING:
    from ..core.task_config import TaskConfigManager

log = logging.getLogger(__name__)


class TaskPromptNotDeclaredError(RuntimeError):
    """Raised when a task is invoked whose ``prompt_template`` is not
    declared by the active bundle/project configuration.

    Task prompts are grammar-opinionated and belong to the pack/bundle
    layer. The editor never substitutes its own text: an undeclared task
    is not invocable (its button is greyed out), and a programmatic
    invocation fails loudly here instead of silently running with a
    made-up prompt.
    """


# Editor-native default for AD_HOC_CHAT ONLY. The ad-hoc chat is
# grammar-neutral (it encodes no procedure-format opinion), so it may keep
# an editor-shipped default; a per-tab ``chat_config.system_prompt``
# overrides it. Every other task prompt must come from the effective
# bundle/project config.
AD_HOC_CHAT_DEFAULT_PROMPT = """
Task: Respond to user question or request.

The user is asking a question or making a request related to test
procedure authoring. Respond CONSERVATIVELY:

- Only review or propose changes when the user EXPLICITLY asks for
  them (e.g. "review this", "fix the equipment IDs", "rewrite step 3").
- If the user's intent is unclear OR the message is conversational
  ("hi", "test", "?", short greetings, ambiguous one-liners), ask a
  brief clarifying question. Do NOT proactively review the procedure
  or produce a proposal.
- If the user asks a question, answer it without modifying artifacts.
- Never include a proposal (procedure_json, test_code, procedure_text)
  unless the user explicitly asked for a change.
"""


class PromptBuilder:
    """
    Builds prompts for LLM tasks following spec Section 14.

    Prompt structure:
    1. Task instruction (from TaskConfigManager)
    2. Strict mode flag
    3. Session summary (if any)
    4. Rules content (if loaded)
    5. Artifacts
    6. Output format requirements

    Prompt resolution order:
    1. prompt_template from TaskConfigManager (effective bundle/project config)
    1b. For AD_HOC_CHAT: per-tab chat_config.system_prompt
    2. Deprecated custom_prompts dict (backward compatibility)
    3. For AD_HOC_CHAT only: AD_HOC_CHAT_DEFAULT_PROMPT (editor-native)
    4. Otherwise: raise TaskPromptNotDeclaredError (never substitute text)
    """

    # Default output format requirements
    DEFAULT_OUTPUT_FORMAT = """
## Required Response Format

You MUST respond with a valid JSON object following this schema:

```json
{
  "type": "llm_turn",
  "task": "<task_name>",
  "strict_mode": <true|false>,
  "assistant_message": "Human-readable message for the user.",
  "validation": {
    "status": "pass|warn|fail",
    "issues": [
      {
        "severity": "error|warning",
        "code": "ISSUE_CODE",
        "message": "Description of the issue",
        "location": "where in the artifact",
        "suggested_fix": "how to fix it"
      }
    ],
    "assumptions": ["any assumptions made"]
  },
  "proposals": {
    "procedure_json": {
      "mode": "replace",
      "content": { /* the full JSON object */ }
    },
    "test_code": {
      "mode": "replace",
      "content": "the full Python code"
    },
    "procedure_text": {
      "mode": "replace",
      "content": "the full markdown text"
    }
  },
  "session_delta": {
    "intent": "updated intent if changed",
    "open_questions": [],
    "resolved_questions": [],
    "decisions_added": []
  }
}
```

Rules:
- Always include "assistant_message" with a helpful message
- For review tasks, include validation.issues[] with problems found AND include proposals with the fixes
- For generation tasks, include proposals with the generated artifacts
- Set proposal mode to null if not providing that artifact
- Only UTF-8
"""
    
    def __init__(
        self, 
        task_config_manager: Optional['TaskConfigManager'] = None,
        tab_id: Optional[str] = None,
        custom_output_format: Optional[str] = None,
        # DEPRECATED: For backward compatibility only
        custom_prompts: Optional[Dict[str, str]] = None
    ):
        """
        Initialize PromptBuilder with TaskConfigManager integration.
        
        Args:
            task_config_manager: TaskConfigManager for querying per-tab task configurations.
                               If None, only the deprecated custom_prompts dict (and the
                               AD_HOC_CHAT editor default) can resolve a task prompt.
            tab_id: Tab identifier (e.g., "text_json", "json_code") for querying task configs.
                   Required if task_config_manager is provided.
            custom_output_format: Custom output format template. If provided, overrides the default.
            custom_prompts: DEPRECATED. Dictionary mapping task names to custom prompts.
                          Provided for backward compatibility. Use task_config_manager instead.
        
        Note:
            tab_id can be None when task_config_manager is provided (e.g., for main window's
            legacy task execution). In this case, TaskConfigManager will not be queried for
            custom prompts, and only AD_HOC_CHAT (editor default) remains resolvable.
        """
        self._task_config_manager = task_config_manager
        self._tab_id = tab_id
        
        # DEPRECATED: Support old custom_prompts parameter for backward compatibility
        self._custom_prompts_dict: Dict[LLMTask, str] = {}
        if custom_prompts:
            log.warning(
                "PromptBuilder: custom_prompts parameter is deprecated. "
                "Use TaskConfigManager instead."
            )
            for task_name, prompt in custom_prompts.items():
                # Convert string task names to LLMTask enum if needed
                if isinstance(task_name, str):
                    try:
                        task_enum = LLMTask(task_name)
                        self._custom_prompts_dict[task_enum] = prompt
                    except ValueError:
                        log.warning(f"Invalid task name in custom_prompts: {task_name}")
                else:
                    self._custom_prompts_dict[task_name] = prompt
        
        # Use custom or default output format
        self.output_format = custom_output_format if custom_output_format else self.DEFAULT_OUTPUT_FORMAT
        
        log.debug(
            f"PromptBuilder initialized: tab_id={tab_id}, "
            f"has_task_config_manager={task_config_manager is not None}, "
            f"has_custom_prompts={len(self._custom_prompts_dict) > 0}, "
            f"custom_output_format={custom_output_format is not None}"
        )
    
    @staticmethod
    def get_default_output_format() -> str:
        """
        Get the default output format template.
        
        Returns:
            Default output format string.
        """
        return PromptBuilder.DEFAULT_OUTPUT_FORMAT
    
    def _get_task_prompt(self, task: LLMTask, custom_task_id: Optional[str] = None) -> str:
        """
        Get the prompt template for a task.

        ``custom_task_id`` is the effective id of a bundle-declared CUSTOM
        task (not in the LLMTask enum) routed through AD_HOC_CHAT. When set,
        ONLY step 1 applies, keyed by that id — the AD_HOC_CHAT fallback
        chain (1b/2/3) belongs to the plain chat and must never silently
        substitute chat text for a custom task.

        Resolution order:
        1. prompt_template from TaskConfigManager (the effective
           bundle/project config — the editor layer ships none), keyed by
           the effective id (``custom_task_id`` or ``task.value``).
        1b. For AD_HOC_CHAT (no custom_task_id): per-tab
            ``chat_config.system_prompt`` (so the workflows-dialog Chat
            editor actually takes effect at runtime — Phase 4.5).
        2. Custom prompt from deprecated custom_prompts dict (if provided)
        3. For AD_HOC_CHAT only (no custom_task_id):
           AD_HOC_CHAT_DEFAULT_PROMPT (grammar-neutral, editor-native).
        4. Otherwise: raise TaskPromptNotDeclaredError — the editor never
           substitutes text for a task the active bundle did not declare.

        Args:
            task: The LLM routing task to get prompt for
            custom_task_id: Effective id of a bundle-declared custom task,
                or None for plain enum tasks.

        Returns:
            Prompt template string

        Raises:
            TaskPromptNotDeclaredError: If the effective task (other than
                plain AD_HOC_CHAT) has no non-empty prompt_template in the
                effective config.
        """
        effective_id = custom_task_id or task.value

        # 1. Try TaskConfigManager (if available), keyed by the EFFECTIVE id
        # so a custom task resolves ITS declared prompt_template.
        if self._task_config_manager is not None and self._tab_id is not None:
            task_config = self._task_config_manager.get_task_config(self._tab_id, effective_id)
            if task_config is not None and (task_config.prompt_template or "").strip():
                log.debug(f"Using prompt for task '{effective_id}' from TaskConfigManager")
                return task_config.prompt_template

        # A custom task gets NO fallback chain: 1b/2/3 all key on the
        # AD_HOC_CHAT routing task and would substitute chat text.
        if custom_task_id is None:
            # 1b. For AD_HOC_CHAT, consult the tab's chat_config.system_prompt.
            # The workflows dialog's Chat section edits this field; without
            # this wiring the field was stored-but-unused.
            if (task == LLMTask.AD_HOC_CHAT
                    and self._task_config_manager is not None
                    and self._tab_id is not None):
                chat = self._task_config_manager.get_chat_config(self._tab_id)
                if chat is not None and chat.system_prompt:
                    log.debug(f"Using chat_config.system_prompt for tab '{self._tab_id}'")
                    return chat.system_prompt

            # 2. Try deprecated custom_prompts dict (backward compatibility)
            if task in self._custom_prompts_dict:
                log.debug(f"Using custom prompt for task '{task.value}' from deprecated custom_prompts")
                return self._custom_prompts_dict[task]

            # 3. AD_HOC_CHAT keeps a grammar-neutral editor-native default.
            if task == LLMTask.AD_HOC_CHAT:
                return AD_HOC_CHAT_DEFAULT_PROMPT

        # 4. Undeclared task: fail loudly, never substitute text.
        raise TaskPromptNotDeclaredError(
            f"Task '{effective_id}' is not declared by the active bundle "
            f"(no prompt_template in the effective workflow configuration)."
        )
    
    def build(self, request: LLMRequest, output_contract_override: Optional[str] = None) -> str:
        """
        Build the complete prompt for a request.
        
        Args:
            request: LLM request with task and context
            output_contract_override: Optional output contract to append after output format.
                                     Used by TabContext to enforce tab-specific contracts.
        
        Returns:
            Complete prompt string

        Raises:
            TaskPromptNotDeclaredError: If the task's prompt_template is not
                declared by the effective config (see _get_task_prompt).
        """
        sections = []
        
        # 1. Task instruction (with fallback chain). The effective id is the
        # request's custom_task_id for bundle-declared custom tasks.
        task_instruction = self._get_task_prompt(request.task, request.custom_task_id)
        sections.append(f"# Task\n{task_instruction}")
        
        # 2. Strict mode
        mode_desc = "STRICT" if request.strict_mode else "FORCE"
        mode_instruction = f"""
## Mode: {mode_desc}

{"Strict mode: You may refuse to generate output if the input is ambiguous or insufficient. Ask clarifying questions." if request.strict_mode else "Force mode: You MUST generate output even if ambiguous. Document all assumptions and issues."}
"""
        sections.append(mode_instruction)
        
        # 3. Session summary
        if request.session_summary:
            sections.append(f"# Session Context\n{request.session_summary}")
        
        # 4. Rules content (only if not already in session)
        if request.rules_content and request.include_rules:
            sections.append(f"# Rules\n{request.rules_content}")
        
        # 5. Artifacts (only if not already in session)
        if request.procedure_json and request.include_json:
            sections.append(f"# Current procedure.json\n```json\n{request.procedure_json}\n```")
        
        if request.test_code and request.include_code:
            sections.append(f"# Current test.py\n```python\n{request.test_code}\n```")
        
        if request.procedure_text and request.include_text:
            sections.append(f"# Current procedure_text.md\n```markdown\n{request.procedure_text}\n```")
        
        # 6. User message (for ad-hoc chat)
        if request.user_message:
            sections.append(f"# User Message\n{request.user_message}")
        
        # 7. Output format
        sections.append(self.output_format)
        
        # 8. Output contract override (for tab-specific restrictions)
        if output_contract_override:
            sections.append(output_contract_override)
        
        return "\n\n".join(sections)
