"""Shared LLM backend types — now single-sourced in the ``opencode_client``
package (migration doc §3a). This module is a thin re-export so existing
``from procedo.studio.llm.backend_base import X`` imports keep working while the
definitions live *once*, shared with the web app, so the two can never drift.
"""

from opencode_client.backend_base import (  # noqa: F401
    LLMBackend,
    LLMProposal,
    LLMRequest,
    LLMResponse,
    LLMTask,
    NoneBackend,
    ValidationIssue,
)
