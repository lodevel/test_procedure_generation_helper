"""LLM Backend modules for the workflow editor."""

from .backend_base import LLMBackend, LLMResponse, LLMRequest, LLMTask, NoneBackend, LLMProposal
from .opencode_backend import OpenCodeBackend, OpenCodeConfig
from .external_api_backend import ExternalAPIBackend, ExternalAPIConfig
from .response_parser import ResponseParser
from .prompt_builder import PromptBuilder
from .output_contracts import get_contract_for_tab, get_allowed_artifacts
from .tab_context import TabContext, ChatMessage

__all__ = [
    "LLMBackend",
    "LLMResponse",
    "LLMRequest",
    "LLMTask",
    "NoneBackend",
    "OpenCodeBackend",
    "OpenCodeConfig",
    "ExternalAPIBackend",
    "ExternalAPIConfig",
    "ResponseParser",
    "PromptBuilder",
    "LLMProposal",
    "get_contract_for_tab",
    "get_allowed_artifacts",
    "TabContext",
    "ChatMessage",
]
