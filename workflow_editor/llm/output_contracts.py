"""
Output Contracts - Defines which artifacts each tab can propose.

Each tab has a specific output contract that enforces which artifacts
the LLM can propose in its response. This prevents context overflow
by limiting the LLM's output scope per task.

Additionally, each LLM task has a more specific contract defining
which artifacts it should produce. This helps detect when the LLM
proposes unexpected artifacts (e.g., proposing procedure_text when
the task is DERIVE_JSON_FROM_TEXT).
"""

from typing import Optional
from .backend_base import LLMTask

# Text-JSON Tab Contract
# Allows: procedure_text and procedure_json proposals only
TEXT_JSON_CONTRACT = """
## Output Contract for Text-JSON Tab

You are operating in the TEXT-JSON workflow context. Your responses MUST follow these rules:

**Allowed Proposals:**
- procedure_text: Textual description of the test procedure
- procedure_json: Structured JSON representation

**FORBIDDEN Proposals:**
- test_code: You MUST NOT generate Python test code in this context

**Validation Rules:**
- You may propose procedure_text OR procedure_json OR both
- Set proposal.mode to "create", "replace", or null (no proposal)
- If you propose test_code, the response will be REJECTED as invalid

This contract ensures you stay focused on text and JSON artifacts only.
"""

# JSON-Code Tab Contract
# Allows: procedure_json and test_code proposals only
JSON_CODE_CONTRACT = """
## Output Contract for JSON-Code Tab

You are operating in the JSON-CODE workflow context. Your responses MUST follow these rules:

**Allowed Proposals:**
- procedure_json: Structured JSON representation
- test_code: Python test code implementation

**FORBIDDEN Proposals:**
- procedure_text: You MUST NOT generate textual procedure descriptions in this context

**Validation Rules:**
- You may propose procedure_json OR test_code OR both
- Set proposal.mode to "create", "replace", or null (no proposal)
- If you propose procedure_text, the response will be REJECTED as invalid

This contract ensures you stay focused on JSON and code artifacts only.
"""




def get_contract_for_tab(tab_id: str) -> str:
    """
    Get the output contract for a specific tab.
    
    Args:
        tab_id: Tab identifier ("text_json", "json_code")
        
    Returns:
        The output contract string for the specified tab
        
    Raises:
        ValueError: If tab_id is not recognized
    """
    contracts = {
        "text_json": TEXT_JSON_CONTRACT,
        "json_code": JSON_CODE_CONTRACT,
    }
    
    if tab_id not in contracts:
        raise ValueError(f"Unknown tab_id: {tab_id}. Valid values: {list(contracts.keys())}")
    
    return contracts[tab_id]


def get_allowed_artifacts(tab_id: str) -> list[str]:
    """
    Get the list of artifacts allowed for a specific tab.
    
    Args:
        tab_id: Tab identifier ("text_json", "json_code")
        
    Returns:
        List of allowed artifact names
        
    Raises:
        ValueError: If tab_id is not recognized
    """
    allowed = {
        "text_json": ["procedure_text", "procedure_json"],
        "json_code": ["procedure_json", "test_code"],
    }
    
    if tab_id not in allowed:
        raise ValueError(f"Unknown tab_id: {tab_id}. Valid values: {list(allowed.keys())}")
    
    return allowed[tab_id]


# Task-Level Output Contracts
# Maps each LLM task to the specific artifacts it should produce
TASK_OUTPUT_CONTRACTS = {
    # Text-JSON Tab Tasks
    LLMTask.DERIVE_JSON_FROM_TEXT: ["procedure_json"],
    LLMTask.RENDER_TEXT_FROM_JSON: ["procedure_text"],
    LLMTask.REVIEW_TEXT_PROCEDURE: ["procedure_text"],
    LLMTask.REVIEW_JSON: ["procedure_json"],
    LLMTask.REVIEW_TEXT_VS_JSON: ["procedure_text", "procedure_json"],
    
    # JSON-Code Tab Tasks
    LLMTask.GENERATE_CODE_FROM_JSON: ["test_code"],
    LLMTask.DERIVE_JSON_FROM_CODE: ["procedure_json"],
    LLMTask.REVIEW_CODE: ["test_code"],
    LLMTask.REVIEW_CODE_VS_JSON: ["procedure_json", "test_code"],
    
    # Ad-hoc chat uses tab-level contract (no task-specific restriction)
    LLMTask.AD_HOC_CHAT: None,
}


def get_task_expected_artifacts(task: LLMTask) -> Optional[list[str]]:
    """
    Get the list of artifacts expected for a specific task.
    
    Args:
        task: LLM task type
        
    Returns:
        List of expected artifact names, or None if task uses tab-level contract
    """
    return TASK_OUTPUT_CONTRACTS.get(task)
