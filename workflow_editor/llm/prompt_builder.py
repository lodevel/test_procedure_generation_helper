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


class PromptBuilder:
    """
    Builds prompts for LLM tasks following spec Section 14.
    
    Prompt structure:
    1. Task instruction (from TaskConfigManager or defaults)
    2. Strict mode flag
    3. Session summary (if any)
    4. Rules content (if loaded)
    5. Artifacts
    6. Output format requirements
    
    Prompt resolution order:
    1. Custom prompt from TaskConfigManager (if available)
    2. DEFAULT_PROMPTS for the specific task
    3. DEFAULT_PROMPTS[AD_HOC_CHAT] as last resort
    """
    
    # Default task instructions (used as fallback when TaskConfig.prompt_template is None)
    DEFAULT_TASK_INSTRUCTIONS = {
        LLMTask.DERIVE_JSON_FROM_CODE: """
Task: Derive procedure.json from test code.

Analyze the provided Python test code and create a structured procedure.json that describes the test procedure.
Extract:
- Test name and description
- Board/equipment requirements
- Test steps (from # Step N markers or inferred from code)
- Expected results
""",
        
        LLMTask.GENERATE_CODE_FROM_JSON: """
Task: Generate test code from procedure.json.

Generate Python test code that implements the procedure described in the JSON.
Requirements:
- Include # Step N markers for each step
- Follow the equipment and measurement specifications
- Handle errors appropriately
""",
        
        LLMTask.REVIEW_JSON: """
Task: Review procedure.json for correctness and completeness.

Analyze the provided procedure.json and identify:
- Missing required fields
- Incomplete step descriptions
- Equipment specification issues
- Any violations of the rules (if provided)

Report issues in validation.issues[] with severity, code, message, location, and suggested_fix.
If you find issues, include a procedure_json proposal with the corrected version.
You may ask clarifying questions if needed.
""",
        
        LLMTask.REVIEW_CODE_VS_JSON: """
Task: Check coherence between procedure.json and test code.

Compare the procedure JSON with the test code and identify:
- Steps in JSON without corresponding code blocks
- Code blocks without corresponding JSON steps
- Equipment mismatches
- Measurement/expectation mismatches
- Rule violations in either artifact

Report issues in validation.issues[] with severity, code, message, location, and suggested_fix.
If you find issues, include proposals (procedure_json and/or test_code) with the corrected versions.
You may ask clarifying questions if needed.
""",
        
        LLMTask.RENDER_TEXT_FROM_JSON: """
Task: Render procedure.json as human-readable text.

Convert the structured JSON into a clear, readable procedure document.
Format as markdown with:
- Title and description
- Equipment list
- Numbered steps with clear instructions
- Expected results
""",
        
        LLMTask.REVIEW_TEXT_PROCEDURE: """
Task: Review procedure text for correctness and completeness.

Analyze the provided procedure text and identify:
- Ambiguous or unclear steps
- Missing equipment specifications
- Missing measurement parameters
- Rule violations (if rules provided)

Report issues in validation.issues[] with severity, code, message, location, and suggested_fix.
If you find issues, include a procedure_text proposal with the corrected version.
You may ask clarifying questions if needed.

IMPORTANT: In your response, ONLY include a 'procedure_text' proposal if needed.
Do NOT generate 'procedure_json' or 'test_code' proposals for this task.
""",
        
        LLMTask.DERIVE_JSON_FROM_TEXT: """
Task: Derive procedure.json from procedure text.

Convert the natural language procedure text into a structured procedure.json.
Extract and structure:
- Test name and description
- Equipment requirements
- Step-by-step procedure
- Expected results and pass/fail criteria

IMPORTANT: In your response, ONLY include a 'procedure_json' proposal.
Do NOT generate 'test_code' or 'procedure_text' proposals for this task.
""",
        
        LLMTask.AD_HOC_CHAT: """
Task: Respond to user question or request.

The user is asking a question or making a request related to test procedure authoring.
Respond helpfully based on the context provided.
If the user asks for changes to an artifact, include a proposal in your response.
If the user asks a question, answer it without modifying artifacts.
""",
        
        LLMTask.REVIEW_CODE: """
Task: Review test code for correctness and rule compliance.

Analyze the provided Python test code and identify:
- Missing or incorrect step markers
- Equipment handling issues
- Measurement structure problems
- Error handling gaps
- Rule violations (if rules provided)
- Code quality issues

Report issues in validation.issues[] with severity, code, message, location, and suggested_fix.
If you find issues, include a test_code proposal with the corrected version.
You may ask clarifying questions if needed.

IMPORTANT: In your response, ONLY include a 'test_code' proposal if needed.
Do NOT generate 'procedure_json' or 'procedure_text' proposals for this task.
""",
        
        LLMTask.REVIEW_TEXT_VS_JSON: """
Task: Check coherence between procedure text and procedure.json.

Compare the procedure text with the procedure JSON and identify:
- Step count mismatches
- Step content/intent mismatches
- Equipment list differences
- Expected result differences

Report issues in validation.issues[] with severity, code, message, location, and suggested_fix.
If you find issues, include proposals (procedure_text and/or procedure_json) with the corrected versions.
You may ask clarifying questions if needed.
""",
    }
    
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
    },
    "text_patches": [
      {
        "line_start": 1,
        "line_end": 3,
        "original": "original text",
        "proposed": "proposed replacement",
        "reason": "why this change"
      }
    ]
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
    
    # Renamed from DEFAULT_TASK_INSTRUCTIONS for clarity
    DEFAULT_PROMPTS = DEFAULT_TASK_INSTRUCTIONS
    
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
                               If None, only DEFAULT_PROMPTS will be used (backward compatibility for tests).
            tab_id: Tab identifier (e.g., "text_json", "json_code") for querying task configs.
                   Required if task_config_manager is provided.
            custom_output_format: Custom output format template. If provided, overrides the default.
            custom_prompts: DEPRECATED. Dictionary mapping task names to custom prompts.
                          Provided for backward compatibility. Use task_config_manager instead.
        
        Note:
            tab_id can be None when task_config_manager is provided (e.g., for main window's
            legacy task execution). In this case, TaskConfigManager will not be queried for
            custom prompts, and DEFAULT_PROMPTS will be used as fallback.
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
    def get_default_prompts() -> Dict[str, str]:
        """
        Get the default prompt templates.
        
        Returns:
            Dictionary mapping task names (as strings) to default prompt templates.
            Used for reference and testing purposes.
        """
        return {task.value: prompt for task, prompt in PromptBuilder.DEFAULT_PROMPTS.items()}
    
    @staticmethod
    def get_default_output_format() -> str:
        """
        Get the default output format template.
        
        Returns:
            Default output format string.
        """
        return PromptBuilder.DEFAULT_OUTPUT_FORMAT
    
    def _get_task_prompt(self, task: LLMTask) -> str:
        """
        Get the prompt template for a task with fallback chain.
        
        Resolution order:
        1. Custom prompt from TaskConfigManager (if configured)
        2. Custom prompt from deprecated custom_prompts dict (if provided)
        3. DEFAULT_PROMPTS for the specific task
        4. DEFAULT_PROMPTS[AD_HOC_CHAT] as last resort
        
        Args:
            task: The LLM task to get prompt for
        
        Returns:
            Prompt template string
        """
        # 1. Try TaskConfigManager (if available)
        if self._task_config_manager is not None and self._tab_id is not None:
            task_config = self._task_config_manager.get_task_config(self._tab_id, task.value)
            if task_config is not None and task_config.prompt_template is not None:
                log.debug(f"Using custom prompt for task '{task.value}' from TaskConfigManager")
                return task_config.prompt_template
        
        # 2. Try deprecated custom_prompts dict (backward compatibility)
        if task in self._custom_prompts_dict:
            log.debug(f"Using custom prompt for task '{task.value}' from deprecated custom_prompts")
            return self._custom_prompts_dict[task]
        
        # 3. Try DEFAULT_PROMPTS for specific task
        if task in self.DEFAULT_PROMPTS:
            return self.DEFAULT_PROMPTS[task]
        
        # 4. Last resort: AD_HOC_CHAT prompt
        log.warning(
            f"Task '{task.value}' not found in DEFAULT_PROMPTS, "
            f"falling back to AD_HOC_CHAT prompt"
        )
        return self.DEFAULT_PROMPTS.get(
            LLMTask.AD_HOC_CHAT,
            "Process the user's request."
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
        """
        sections = []
        
        # 1. Task instruction (with fallback chain)
        task_instruction = self._get_task_prompt(request.task)
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
