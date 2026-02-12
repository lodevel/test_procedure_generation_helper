"""
LLM Backend Base - Abstract base class for LLM backends.

All LLM backends (OpenCode, External API, None) implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Any, Callable, TYPE_CHECKING
from pathlib import Path
import threading
import logging

if TYPE_CHECKING:
    from .prompt_builder import PromptBuilder
    from .response_parser import ResponseParser

log = logging.getLogger(__name__)


class LLMTask(Enum):
    """LLM task types as defined in spec Section 12."""
    DERIVE_JSON_FROM_CODE = "derive_json_from_code"
    GENERATE_CODE_FROM_JSON = "generate_code_from_json"
    REVIEW_JSON = "review_json"
    REVIEW_CODE_VS_JSON = "review_code_vs_json"
    RENDER_TEXT_FROM_JSON = "render_text_from_json"
    REVIEW_TEXT_PROCEDURE = "review_text_procedure"
    DERIVE_JSON_FROM_TEXT = "derive_json_from_text"
    AD_HOC_CHAT = "ad_hoc_chat"
    REVIEW_CODE = "review_code"
    REVIEW_TEXT_VS_JSON = "review_text_vs_json"


@dataclass
class LLMProposal:
    """A proposed artifact change from the LLM."""
    mode: str  # "replace", "patch", or None
    content: Any  # dict for JSON, str for code/text
    
    @property
    def is_valid(self) -> bool:
        """Check if proposal has valid content."""
        if self.mode is None or self.content is None:
            return False
        if isinstance(self.content, str):
            return len(self.content.strip()) > 0
        if isinstance(self.content, dict):
            return True
        return False


@dataclass
class TextPatch:
    """A proposed text patch."""
    line_start: int
    line_end: int
    original: str
    proposed: str
    reason: str = ""


@dataclass
class ValidationIssue:
    """A validation issue from LLM response."""
    severity: str  # "error" or "warning"
    code: str
    message: str
    location: str = ""
    suggested_fix: str = ""


@dataclass
class LLMResponse:
    """
    Parsed LLM response following the contract in spec Section 13.
    """
    # Raw response for debugging
    raw_response: str = ""
    
    # Parsing status
    success: bool = False
    error_message: str = ""
    context_exceeded: bool = False  # True if context length exceeded
    
    # Task info
    task: Optional[LLMTask] = None
    strict_mode: bool = False
    
    # Token usage tracking
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    
    # Chat message (always display in chat panel)
    assistant_message: str = ""
    
    # Validation results
    validation_status: str = ""  # "pass", "warn", "fail"
    issues: list[ValidationIssue] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    
    # Proposals (only apply after user accepts)
    procedure_json: Optional[LLMProposal] = None
    test_code: Optional[LLMProposal] = None
    procedure_text: Optional[LLMProposal] = None
    text_patches: list[TextPatch] = field(default_factory=list)
    
    # Session updates
    session_delta: dict[str, Any] = field(default_factory=dict)
    
    @property
    def has_proposals(self) -> bool:
        """Check if response contains any proposals."""
        if self.procedure_json and self.procedure_json.is_valid:
            return True
        if self.test_code and self.test_code.is_valid:
            return True
        if self.procedure_text and self.procedure_text.is_valid:
            return True
        if self.text_patches:
            return True
        return False
    
    @property
    def has_issues(self) -> bool:
        """Check if response contains validation issues."""
        return len(self.issues) > 0
    
    @property
    def has_errors(self) -> bool:
        """Check if response contains error-level issues."""
        return any(i.severity == "error" for i in self.issues)


@dataclass
class LLMRequest:
    """Request to send to LLM backend."""
    task: LLMTask
    strict_mode: bool = True
    
    # Artifacts to include
    procedure_json: Optional[str] = None
    test_code: Optional[str] = None
    procedure_text: Optional[str] = None
    
    # Control what to include in prompt (for session optimization)
    include_json: bool = True
    include_code: bool = True
    include_text: bool = True
    
    # Rules content
    rules_content: Optional[str] = None
    include_rules: bool = True  # Whether to include rules in prompt
    
    # Session summary
    session_summary: str = ""
    
    # User message (for ad-hoc chat)
    user_message: str = ""
    
    # Output contract (for tab-specific restrictions)
    output_contract: Optional[str] = None
    
    # Additional context
    extra_context: dict[str, Any] = field(default_factory=dict)


class LLMBackend(ABC):
    """
    Abstract base class for LLM backends.
    
    Implementations:
    - OpenCodeBackend: Uses WSL OpenCode CLI
    - ExternalAPIBackend: Uses OpenAI-compatible API
    - NoneBackend: Disabled, returns error
    """
    
    def __init__(
        self,
        custom_prompts: Optional[dict] = None,
        custom_output_format: Optional[str] = None,
    ):
        """
        Initialize base backend.
        
        Args:
            custom_prompts: Optional custom prompt templates
            custom_output_format: Optional custom output format specification
        """
        # Shared state
        self._running = False
        self._cancel_requested = False
        self._lock = threading.Lock()
        
        # Lazy-loaded prompt builder and response parser
        self._custom_prompts = custom_prompts
        self._custom_output_format = custom_output_format
        self.__prompt_builder: Optional["PromptBuilder"] = None
        self.__response_parser: Optional["ResponseParser"] = None
    
    @property
    def _prompt_builder(self) -> "PromptBuilder":
        """Lazily create prompt builder to avoid circular imports."""
        if self.__prompt_builder is None:
            from .prompt_builder import PromptBuilder
            self.__prompt_builder = PromptBuilder(
                custom_prompts=self._custom_prompts,
                custom_output_format=self._custom_output_format
            )
        return self.__prompt_builder
    
    @property
    def _response_parser(self) -> "ResponseParser":
        """Lazily create response parser to avoid circular imports."""
        if self.__response_parser is None:
            from .response_parser import ResponseParser
            self.__response_parser = ResponseParser()
        return self.__response_parser
    
    def _get_system_prompt(self, task: LLMTask) -> str:
        """Get system prompt for the task.
        
        Subclasses can override for task-specific prompts.
        """
        return """You are an AI assistant helping to create and review test procedures.
You must respond with valid JSON following the specified schema.
Your response must be a single JSON object."""
    
    def _extract_token_usage(self, response_data: dict) -> tuple[int, int, int]:
        """
        Extract token usage from LLM response.
        
        Supports multiple response formats:
        - OpenAI format: response["usage"] with prompt_tokens, completion_tokens, total_tokens
        - OpenCode/Alternative format: response["tokens"] or response["info"]["tokens"] 
          with input, output, reasoning
        
        Args:
            response_data: The response dictionary (not JSON string)
            
        Returns:
            Tuple of (prompt_tokens, completion_tokens, total_tokens)
        """
        import logging
        log = logging.getLogger(__name__)
        
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        
        # Try OpenAI format first
        usage = response_data.get("usage")
        if usage and "prompt_tokens" in usage:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)
            log.debug(f"Token usage (OpenAI format): {total_tokens} total ({prompt_tokens} prompt + {completion_tokens} completion)")
            return prompt_tokens, completion_tokens, total_tokens
        
        # Try alternative format: response["tokens"] or response["info"]["tokens"]
        tokens = response_data.get("tokens")
        if not tokens:
            # Check if wrapped in "info" object (OpenCode format)
            info = response_data.get("info", {})
            tokens = info.get("tokens")
        
        if tokens and ("input" in tokens or "output" in tokens):
            prompt_tokens = tokens.get("input", 0)
            completion_tokens = tokens.get("output", 0)
            reasoning_tokens = tokens.get("reasoning", 0)
            total_tokens = prompt_tokens + completion_tokens + reasoning_tokens
            log.info(f"Token usage (alternative format): {total_tokens} total ({prompt_tokens} input + {completion_tokens} output + {reasoning_tokens} reasoning)")
            return prompt_tokens, completion_tokens, total_tokens
        
        # No token data found
        log.warning("No token usage data found in response")
        return 0, 0, 0
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available and configured."""
        pass
    
    @abstractmethod
    def start(self) -> bool:
        """
        Start the backend (e.g., start OpenCode server).
        
        Returns True on success.
        """
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the backend (e.g., stop OpenCode server)."""
        pass
    
    @abstractmethod
    def send_request(
        self, 
        request: LLMRequest,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> LLMResponse:
        """
        Send a request to the LLM and return the response.
        
        Args:
            request: The LLM request
            progress_callback: Optional callback for progress updates
        
        Returns:
            Parsed LLM response
        """
        pass
    
    @abstractmethod
    def cancel(self) -> None:
        """Cancel any in-progress request."""
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Get backend name for display."""
        pass
    
    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Check if backend is currently running."""
        pass


class NoneBackend(LLMBackend):
    """
    Disabled LLM backend.
    
    All operations return an error indicating LLM is disabled.
    Local validation still works.
    """
    
    def __init__(self):
        # NoneBackend doesn't need prompt builder/parser but needs base init
        super().__init__()
    
    def is_available(self) -> bool:
        return True  # Always "available" since it's just disabled
    
    def start(self) -> bool:
        return True  # No-op
    
    def stop(self) -> None:
        pass  # No-op
    
    def send_request(
        self, 
        request: LLMRequest,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> LLMResponse:
        return LLMResponse(
            success=False,
            error_message="LLM backend is disabled. Enable it in Settings -> LLM Backend.",
            assistant_message="LLM backend is disabled. Please configure it in Settings.",
        )
    
    def cancel(self) -> None:
        pass  # No-op
    
    @property
    def name(self) -> str:
        return "None (Disabled)"
    
    @property
    def is_running(self) -> bool:
        return False
