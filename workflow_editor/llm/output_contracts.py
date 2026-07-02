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
from . import section_ownership
from .backend_base import LLMTask
from .section_ownership import SectionOwnership

# Text-only Tab Contract
# Allows: procedure_text proposals only (smaller rule context, no JSON/code)
TEXT_ONLY_CONTRACT = """
## Output Contract for Text Tab

You are operating in the TEXT-ONLY workflow context. Your responses MUST follow these rules:

**Allowed Proposals:**
- procedure_text: Textual description of the test procedure

**FORBIDDEN Proposals:**
- procedure_json: You MUST NOT generate structured JSON in this context
- test_code: You MUST NOT generate Python test code in this context

**Validation Rules:**
- You may propose procedure_text or no proposal at all
- Set proposal.mode to "create", "replace", or null (no proposal)
- If you propose procedure_json or test_code, the response will be REJECTED as invalid

This contract ensures you stay focused on the text artifact only.
"""

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




def render_section_emit_list(ownership: SectionOwnership) -> str:
    """Render the procedure_text section-ownership instruction block from a
    resolved SectionOwnership. Lists the LLM-owned sections in the bundle's
    declared order (with headings) as the ONLY things to author, and the
    parser-owned ones as must-not-emit / auto-preserved.

    Iterates ``ownership.section_order`` (the bundle's declared universe);
    labels each section via :func:`section_ownership.heading_label` (canonical
    heading, or a derived ``## Name`` label for a bundle-declared section
    outside the default ruleset). An old/blank SectionOwnership
    with an empty ``section_order`` falls back to ``CANONICAL_SECTION_ORDER``."""
    order = ownership.section_order or section_ownership.CANONICAL_SECTION_ORDER
    owned: list[str] = []
    parser_owned: list[str] = []
    for section in order:
        heading = section_ownership.heading_label(section)
        if section in ownership.llm_sections:
            owned.append(heading)
        else:
            parser_owned.append(heading)

    lines = ["**Section ownership (procedure_text):**"]
    if owned:
        lines.append("Author ONLY these sections, in this exact order, and nothing else:")
        for i, heading in enumerate(owned, start=1):
            lines.append(f"{i}. {heading}")
    else:
        lines.append(
            "All sections are operator-owned for this task — do not author any "
            "procedure_text section; return no procedure_text proposal."
        )
    if parser_owned:
        lines.append(
            "Do NOT emit these — they are operator-owned and reconstructed "
            "automatically (emitting them has no effect):"
        )
        for heading in parser_owned:
            lines.append(f"- {heading}")
    if owned:
        lines.append("Start your output at the first owned section.")
    return "\n".join(lines)


def get_contract_for_tab(
    tab_id: str, *, ownership: "SectionOwnership | None" = None
) -> str:
    """
    Get the output contract for a specific tab.

    Args:
        tab_id: Tab identifier ("text_only", "text_json", "json_code")
        ownership: Resolved section ownership threaded into the emit-list for
            text-producing tabs. When omitted, falls back to the baked-in
            DEFAULT_OWNERSHIP so a caller without project context still works.
            Stays pure: the bundle-resolved ownership is built at the call
            sites (see ``reconstruction.pipeline_ownership``), not here.

    Returns:
        The output contract string for the specified tab. Text-producing tabs
        ("text_only", "text_json") have the section emit-list appended.

    Raises:
        ValueError: If tab_id is not recognized
    """
    contracts = {
        "text_only": TEXT_ONLY_CONTRACT,
        "text_json": TEXT_JSON_CONTRACT,
        "json_code": JSON_CODE_CONTRACT,
    }

    if tab_id not in contracts:
        raise ValueError(f"Unknown tab_id: {tab_id}. Valid values: {list(contracts.keys())}")

    base_contract = contracts[tab_id]
    if tab_id in ("text_only", "text_json"):
        if ownership is None:
            ownership = section_ownership.resolve(section_ownership.DEFAULT_OWNERSHIP)
        return base_contract + "\n" + render_section_emit_list(ownership)
    return base_contract


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
        "text_only": ["procedure_text"],
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
