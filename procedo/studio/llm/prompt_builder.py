"""Prompt assembly — single-sourced in ``opencode_client`` (migration doc §3a).
Thin re-export; see ``backend_base`` for the anti-drift rationale.
"""

from opencode_client.prompt_builder import (  # noqa: F401
    AD_HOC_CHAT_DEFAULT_PROMPT,
    PromptBuilder,
    TaskPromptNotDeclaredError,
)
