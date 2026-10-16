"""LLM Backend modules for the workflow editor.

Importable without Qt: the Qt worker thread lives in ``llm.worker`` and is
imported from there by the widgets that run it."""


def _ensure_opencode_client_on_path() -> None:
    """Make the shared ``opencode_client`` package importable before the
    re-export modules below load it.

    The client lives in the ``external/opencode_client`` git submodule
    (migration doc §2c/§3). This is a no-op when it is already resolvable —
    ``pip install -e external/opencode_client`` or already on ``sys.path`` — and
    otherwise walks up to the submodule root and prepends it, mirroring how
    ``__main__`` bootstraps ``procedo.core``.
    """
    import importlib.util
    import sys
    from pathlib import Path

    try:
        if importlib.util.find_spec("opencode_client") is not None:
            return
    except (ImportError, ValueError):
        pass
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "external" / "opencode_client"
        if (candidate / "opencode_client" / "__init__.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_opencode_client_on_path()

from .backend_base import LLMBackend, LLMResponse, LLMRequest, LLMTask, NoneBackend, LLMProposal
from .opencode_backend import OpenCodeBackend, OpenCodeConfig
from .response_parser import ResponseParser
from .prompt_builder import PromptBuilder, TaskPromptNotDeclaredError
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
    "ResponseParser",
    "PromptBuilder",
    "TaskPromptNotDeclaredError",
    "LLMProposal",
    "get_contract_for_tab",
    "get_allowed_artifacts",
    "TabContext",
    "ChatMessage",
]
