"""
Tab Context - Manages independent LLM context per tab.

Each tab has its own:
- Chat conversation history (in-memory only)
- Selected rules (persisted to config)
- Output contract (tab-specific artifact restrictions)
- Token usage tracking
- Backend instance (lazily created via factory)

This replaces the centralized MainWindow.run_llm_task() approach.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from pathlib import Path

from . import reconstruction
from .backend_base import LLMBackend, LLMRequest, LLMResponse, LLMTask
from .output_contracts import get_contract_for_tab, get_allowed_artifacts, get_task_expected_artifacts
from .prompt_builder import PromptBuilder
from .response_parser import ResponseParser
from ..core.session_state import SessionState

if TYPE_CHECKING:
    from ..core import ProjectManager, ArtifactManager
    from ..core.task_config import TaskConfigManager
    from .backend_factory import BackendFactory

log = logging.getLogger(__name__)


# YAML frontmatter fields stripped from rule docs before they ship to the LLM.
# These exist for the build pipeline (rules_index.json, audience routing) and add
# no semantic value the LLM uses to perform its tasks. Strip them on read so the
# markdown source on disk stays canonical while the LLM payload is leaner.
_LLM_USELESS_FRONTMATTER_FIELDS = frozenset({"related", "audience"})


def _strip_llm_useless_frontmatter(content: str) -> str:
    """
    Remove allowlisted top-level frontmatter fields from a markdown doc.

    Recognizes the standard `---\\n<yaml>\\n---\\n` frontmatter block at the
    start of the file. Only top-level fields whose name is in
    `_LLM_USELESS_FRONTMATTER_FIELDS` are stripped, including any continuation
    lines indented under them (so multi-line list values like
    `related:\\n  - a\\n  - b` are dropped as a unit). Other frontmatter is
    preserved as-is. Files without frontmatter are returned unchanged.
    """
    if not content.startswith("---\n"):
        return content
    end_marker_idx = content.find("\n---\n", 4)
    if end_marker_idx == -1:
        return content
    fm_body = content[4:end_marker_idx]
    rest = content[end_marker_idx + len("\n---\n"):]

    kept_lines: list[str] = []
    skipping = False
    for line in fm_body.split("\n"):
        # A top-level field starts at column 0 with `<name>:` (no leading space).
        if line and not line[0].isspace() and ":" in line:
            field_name = line.split(":", 1)[0].strip()
            skipping = field_name in _LLM_USELESS_FRONTMATTER_FIELDS
            if skipping:
                continue
        elif skipping:
            # Continuation line (indented, blank, or comment under a stripped field).
            continue
        kept_lines.append(line)

    new_fm = "\n".join(kept_lines)
    return f"---\n{new_fm}\n---\n{rest}"


# Task-to-Artifact Requirements Mapping
# Defines which artifacts each task needs as input
TASK_ARTIFACT_REQUIREMENTS = {
    # Text-JSON Tab Tasks
    LLMTask.DERIVE_JSON_FROM_TEXT: ["procedure_text"],
    LLMTask.RENDER_TEXT_FROM_JSON: ["procedure_json"],
    LLMTask.REVIEW_TEXT_PROCEDURE: ["procedure_text"],
    LLMTask.REVIEW_JSON: ["procedure_json"],
    LLMTask.REVIEW_TEXT_VS_JSON: ["procedure_text", "procedure_json"],
    
    # JSON-Code Tab Tasks
    LLMTask.GENERATE_CODE_FROM_JSON: ["procedure_json"],
    LLMTask.DERIVE_JSON_FROM_CODE: ["test_code"],
    LLMTask.REVIEW_CODE: ["test_code"],
    LLMTask.REVIEW_CODE_VS_JSON: ["procedure_json", "test_code"],
    
    # Ad-hoc chat uses all artifacts available in current tab
    LLMTask.AD_HOC_CHAT: None,
}


@dataclass
class ChatMessage:
    """A single chat message in tab context."""
    role: str  # 'user', 'assistant', 'system'
    content: str  # Display text
    full_prompt: Optional[str] = None  # Full prompt (for user messages)
    full_response: Optional[str] = None  # Full response (for assistant messages)
    thinking_content: str = ""  # LLM thinking/reasoning content
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    msg_id: str = ""  # Unique message ID (UUID)
    
    def __post_init__(self):
        """Generate UUID if msg_id not provided."""
        if not self.msg_id:
            import uuid
            self.msg_id = str(uuid.uuid4())


class TabContext:
    """
    Manages LLM context for a specific tab.
    
    Responsibilities:
    - Maintain independent chat history (in-memory)
    - Load/save selected rules from config
    - Apply tab-specific output contracts
    - Execute LLM requests with filtered context
    - Track cumulative token usage
    - Own backend instance via factory (lazy initialization)
    """
    
    def __init__(
        self,
        tab_id: str,
        backend_factory: "BackendFactory",
        project_manager: "ProjectManager",
        artifact_manager: "ArtifactManager",
        task_config_manager: "TaskConfigManager",
        session_state: "SessionState" = None,  # Ignored - kept for backward compatibility
    ):
        """
        Initialize tab context.

        Args:
            tab_id: Tab identifier ("text_only", "text_json", "json_code")
            backend_factory: Factory for creating backend instances
            project_manager: Project manager for rule enumeration
            artifact_manager: Artifact manager for current content
            task_config_manager: TaskConfigManager — owns per-task
                ``selected_rules`` resolution after Phase 1.
            session_state: DEPRECATED - ignored. Each tab creates its own SessionState.
        """
        self.tab_id = tab_id
        self._backend_factory = backend_factory
        self._backend: Optional[LLMBackend] = None  # Lazy initialization
        self.project_manager = project_manager
        self.task_config_manager = task_config_manager
        self.artifact_manager = artifact_manager
        
        # Per-tab session state (in-memory only, not persisted)
        self.session_state = SessionState()
        
        # In-memory chat history (not persisted)
        self.messages: list[ChatMessage] = []
        
        # Per-tab raw LLM responses (for RawResponseViewer)
        self.raw_responses: list[str] = []
        
        # Per-tab validation issues (for FindingsPanel)
        self.validation_issues: list[dict] = []
        
        # Current task tracking for validation
        self._current_task: Optional[LLMTask] = None
        
        # Token tracking
        self.cumulative_tokens = 0
        
        # Artifact tracking for conditional sending
        self._artifact_checksums: dict[str, str] = {}  # artifact_name → MD5 hash
        self._first_interaction: bool = True  # True until first LLM request
        
        # Rules tracking for automatic re-sending when rules change
        self._rules_checksum: Optional[str] = None  # MD5 hash of rules content
        
        # Artifact optimization state (tracks what's been sent to LLM)
        self._last_json_content: Optional[str] = None
        self._last_code_content: Optional[str] = None
        self._last_text_content: Optional[str] = None
        self._rules_sent: bool = False
        
        log.info(
            f"TabContext initialized for {tab_id} (project_root={self.project_manager.project_root})"
        )
    
    @property
    def backend(self) -> LLMBackend:
        """
        Get backend instance, creating lazily on first access.

        If the factory returns a :class:`NoneBackend` as a *fallback*
        (e.g. because the OpenCode server was not running), the result is
        **not** cached so that the next access retries the factory and can
        pick up a newly available server.  A ``NoneBackend`` is only
        cached when the configured backend type is explicitly ``"none"``.

        Returns:
            LLMBackend instance for this tab
        """
        if self._backend is not None:
            return self._backend

        from .backend_base import NoneBackend

        if self._backend_factory is None:
            log.warning(f"TabContext {self.tab_id}: No backend factory available")
            self._backend = NoneBackend()
            return self._backend

        log.info(f"TabContext {self.tab_id}: Creating backend lazily via factory")
        backend = self._backend_factory.create_backend(self.tab_id)

        # Start the backend if needed
        if hasattr(backend, 'start'):
            log.debug(f"TabContext {self.tab_id}: Starting backend")
            backend.start()

        # Only cache the backend if it is a real backend or the user
        # explicitly configured "none".  A NoneBackend returned as a
        # fallback (server unavailable) must NOT be cached so that the
        # next property access retries.
        if not isinstance(backend, NoneBackend) or self._backend_factory.backend_type == "none":
            self._backend = backend
        else:
            log.info(
                f"TabContext {self.tab_id}: Factory returned fallback NoneBackend — "
                "will retry on next access"
            )

        return backend
    
    def update_managers(
        self,
        artifact_manager: "ArtifactManager",
        session_state: "SessionState" = None  # Ignored - kept for backward compatibility
    ):
        """Update manager references after test is opened.
        
        This fixes the issue where TabContext is created with None managers
        during tab initialization, but real managers are created later when
        a test is opened.
        
        Also reloads the tab configuration now that project_root is available,
        replacing the default 'all' config with the actual project's config file.
        
        Args:
            artifact_manager: Updated artifact manager instance
            session_state: DEPRECATED - ignored. Each tab has its own SessionState.
        """
        self.artifact_manager = artifact_manager
        # Note: session_state parameter is ignored - each tab has its own per-tab SessionState

        # Workflow config reloads are driven externally by MainWindow calling
        # ``task_config_manager.reload(project_root)`` when a project opens.
        # Clear the rules checksum so the next LLM request resends rules
        # against the (possibly new) per-task selection.
        self._rules_checksum = None

        log.info(f"TabContext {self.tab_id}: managers updated")
    
    def reset_conversation(self):
        """Reset chat history, token counter, first interaction flag, and optimization state."""
        self.messages.clear()
        self.cumulative_tokens = 0
        self._first_interaction = True  # Reset flag so rules are sent on next interaction
        self._artifact_checksums.clear()  # Clear artifact checksums so all are re-sent
        self._rules_checksum = None  # Clear rules checksum so rules are re-sent
        # Reset optimization state
        self._last_json_content = None
        self._last_code_content = None
        self._last_text_content = None
        self._rules_sent = False
        log.info(f"TabContext {self.tab_id}: conversation reset (_first_interaction=True, optimization state cleared)")
    
    def get_selected_rules(self) -> list[str]:
        """Resolve the rule filenames to send for the current LLM request.

        After Phase 1 of the workflows-to-project-config refactor,
        ``selected_rules`` is per-task. We pick the task that's actively
        being executed (``self._current_task``) when one is set, and fall
        back to the tab's first task otherwise. The ``"all"`` sentinel
        expands against the project's available rule files.
        """
        raw = self._raw_selected_rules_for_current_task()
        if raw is None or raw == "all":
            return [rf.name for rf in self.project_manager.get_rules_files()]
        if isinstance(raw, list):
            return list(raw)
        log.warning(
            f"TabContext {self.tab_id}: unexpected selected_rules type %r; falling back to all",
            type(raw).__name__,
        )
        return [rf.name for rf in self.project_manager.get_rules_files()]

    def _raw_selected_rules_for_current_task(self):
        """Look up the per-task ``selected_rules`` value (no expansion)."""
        # Phase 1 stores selected_rules per task; prefer the task that's
        # being executed right now (``_current_task`` set by ``_build_request``).
        if self._current_task is not None:
            value = self.task_config_manager.get_selected_rules_for_task(
                self.tab_id, self._current_task.value
            )
            if value is not None:
                return value
        # No active task yet — use the first task's selection as a stable
        # representative for "this tab".
        tasks = self.task_config_manager.get_all_tasks_for_tab(self.tab_id)
        if tasks:
            return tasks[0].selected_rules
        return None
    
    def update_backend(self, new_backend: LLMBackend):
        """
        Update the backend for this tab context directly.
        
        Called when the user switches LLM backends in settings.
        Preserves conversation history since messages are independent of backend.
        
        Note: Prefer using update_backend_factory() instead when possible,
        as it integrates better with the factory-based architecture.
        
        Args:
            new_backend: The new LLM backend instance
        """
        old_backend_name = self._backend.__class__.__name__ if self._backend else "None"
        new_backend_name = new_backend.__class__.__name__
        
        self._backend = new_backend
        
        # Keep conversation history - it's independent of the backend
        # Users can continue their conversation with the new backend
        
        log.info(f"TabContext {self.tab_id}: backend updated from {old_backend_name} to {new_backend_name}, preserving {len(self.messages)} messages")
    
    def update_backend_factory(self, new_factory: "BackendFactory"):
        """
        Update backend factory and recreate backend on next use.
        
        This is the preferred way to change backend configuration.
        The existing backend is discarded and a new one will be created
        lazily on the next access to the backend property.
        
        Args:
            new_factory: The new BackendFactory instance
        """
        old_factory_type = self._backend_factory.backend_type if self._backend_factory else "none"
        new_factory_type = new_factory.backend_type if new_factory else "none"
        
        self._backend_factory = new_factory
        
        # Clear old backend - will be recreated on next use
        if self._backend is not None:
            if hasattr(self._backend, 'stop'):
                log.debug(f"TabContext {self.tab_id}: Stopping old backend")
                self._backend.stop()
            self._backend = None
        
        # Reset optimization state since new backend means new session
        self._last_json_content = None
        self._last_code_content = None
        self._last_text_content = None
        self._rules_sent = False
        self._first_interaction = True
        
        log.info(
            f"TabContext {self.tab_id}: backend factory updated from {old_factory_type} to {new_factory_type}, "
            f"backend cleared for lazy recreation"
        )
    
    def reset_backend(self):
        """
        Reset backend session (creates new session).
        
        This method resets the backend session state without changing
        the backend type. Use this when you need a fresh session with
        the same backend configuration.
        """
        if self._backend is not None:
            if hasattr(self._backend, 'reset_session'):
                log.debug(f"TabContext {self.tab_id}: Resetting backend session")
                self._backend.reset_session()
        
        # Reset optimization state since new session
        self._last_json_content = None
        self._last_code_content = None
        self._last_text_content = None
        self._rules_sent = False
        self._first_interaction = True
        
        log.info(f"TabContext {self.tab_id}: backend session reset, optimization state cleared")
    
    # Artifact optimization helper methods
    
    def _should_include_json(self, content: Optional[str], force: bool) -> bool:
        """
        Determine if JSON should be included in request.
        
        Args:
            content: Current JSON content
            force: If True, always include
            
        Returns:
            True if JSON should be included
        """
        if not content:
            return False
        if force or self._first_interaction:
            return True
        return content != self._last_json_content
    
    def _should_include_code(self, content: Optional[str], force: bool) -> bool:
        """
        Determine if code should be included in request.
        
        Args:
            content: Current code content
            force: If True, always include
            
        Returns:
            True if code should be included
        """
        if not content:
            return False
        if force or self._first_interaction:
            return True
        return content != self._last_code_content
    
    def _should_include_text(self, content: Optional[str], force: bool) -> bool:
        """
        Determine if text should be included in request.
        
        Args:
            content: Current text content
            force: If True, always include
            
        Returns:
            True if text should be included
        """
        if not content:
            return False
        if force or self._first_interaction:
            return True
        return content != self._last_text_content
    
    def _should_include_rules(self, content: Optional[str], force: bool) -> bool:
        """
        Determine if rules should be included in request.
        
        Args:
            content: Current rules content
            force: If True, always include
            
        Returns:
            True if rules should be included
        """
        if not content:
            return False
        if force or self._first_interaction:
            return True
        if not self._rules_sent:
            return True
        # Check if rules changed
        return self._has_rules_changed(content)
    
    def _update_optimization_state(self, request: LLMRequest):
        """
        Update optimization state after building request.
        
        Tracks which content was sent so we can skip unchanged content
        in subsequent requests.
        
        Args:
            request: The request that was built and will be sent
        """
        # Track JSON content if included
        if request.procedure_json:
            self._last_json_content = request.procedure_json
            log.debug(f"TabContext {self.tab_id}: Updated last_json_content")
        
        # Track code content if included
        if request.test_code:
            self._last_code_content = request.test_code
            log.debug(f"TabContext {self.tab_id}: Updated last_code_content")
        
        # Track text content if included
        if request.procedure_text:
            self._last_text_content = request.procedure_text
            log.debug(f"TabContext {self.tab_id}: Updated last_text_content")
        
        # Track artifact checksums and first-interaction flag
        for name in ("procedure_text", "procedure_json", "test_code"):
            content = getattr(request, name, None)
            if content:
                self.mark_artifact_sent(name, content)
        
        # Track rules content if included
        if request.rules_content:
            self._rules_sent = True
            self._update_rules_checksum(request.rules_content)
            log.debug(f"TabContext {self.tab_id}: Updated rules checksum, rules_sent=True")
    
    def confirm_request_delivered(self, request: LLMRequest) -> None:
        """Mark artifacts and rules in *request* as successfully delivered.

        Call this **only** after the backend has returned a successful
        response.  This prevents the optimisation state from advancing
        when the server is unreachable and a ``NoneBackend`` is used.
        """
        self._update_optimization_state(request)
        log.debug(f"TabContext {self.tab_id}: Optimization state confirmed after successful delivery")
    
    def set_selected_rules(self, rule_filenames: list[str]):
        """Update selected rules for this tab and persist via TaskConfigManager.

        Phase 1 keeps the UI tab-level: the same list propagates to every
        task in the tab. Per-task selection lands in Phase 4's editor.
        """
        self.task_config_manager.set_selected_rules_for_tab(self.tab_id, rule_filenames)
        self.task_config_manager.save_config()

        # Clear rules checksum so the next LLM request resends rules.
        self._rules_checksum = None

        log.info(
            f"TabContext {self.tab_id}: rules updated to {len(rule_filenames)} selected, "
            "checksum cleared"
        )
    
    def get_selected_rules_content(self) -> Optional[str]:
        """
        Get concatenated content of selected rules only.
        
        Returns:
            Concatenated markdown content of selected rules, or None if no rules
        """
        log.info(f"TabContext {self.tab_id}: get_selected_rules_content() called")
        if self.project_manager.rules_root is None:
            log.warning(f"TabContext {self.tab_id}: rules_root is None!")
            return None
        
        log.info(f"TabContext {self.tab_id}: rules_root = {self.project_manager.rules_root}")
        selected_filenames = self.get_selected_rules()
        log.info(f"TabContext {self.tab_id}: get_selected_rules_content() loading {len(selected_filenames)} rules: {selected_filenames}")
        if not selected_filenames:
            log.warning(f"TabContext {self.tab_id}: No rules selected!")
            return None
        
        contents = []
        for filename in selected_filenames:
            rule_path = self.project_manager.rules_root / filename
            if rule_path.exists():
                header = f"\n{'='*60}\n# Rules from: {filename}\n{'='*60}\n"
                content = rule_path.read_text(encoding="utf-8")
                content = _strip_llm_useless_frontmatter(content)
                contents.append(header + content)
            else:
                log.warning(f"Selected rule file not found: {filename}")
        
        return "\n".join(contents) if contents else None
    
    def send_task(
        self,
        task: LLMTask,
        user_message: Optional[str] = None,
        strict_mode: bool = False
    ) -> LLMResponse:
        """
        Send an LLM task with tab-specific context.
        
        This is the main method for executing LLM requests in a tab context.
        It builds the prompt with:
        - Task instruction
        - Tab-specific output contract
        - Filtered rules (only selected ones)
        - Current artifacts
        - Session summary
        
        Args:
            task: LLM task to execute
            user_message: Optional user message for ad-hoc chat
            strict_mode: Whether to enforce strict validation
            
        Returns:
            LLMResponse with result
        """
        log.info(f"TabContext {self.tab_id}: sending task {task.value}")
        
        # Build and send request
        request = self._build_request(task, user_message, strict_mode)
        
        # Store user message in history
        if user_message:
            user_msg = ChatMessage(
                role="user",
                content=user_message,
                full_prompt=None  # Will be filled after backend builds it
            )
            self.messages.append(user_msg)
        
        # Send request
        response = self.backend.send_request(request)
        
        # Only confirm delivery if the backend actually succeeded
        if response.success:
            self.confirm_request_delivered(request)
            response = self._validate_contract(response)
        
        # Store assistant response in history
        if response.success and response.assistant_message:
            assistant_msg = ChatMessage(
                role="assistant",
                content=response.assistant_message,
                full_response=response.raw_response,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens
            )
            self.messages.append(assistant_msg)
            
            # Update cumulative tokens
            self.cumulative_tokens += response.total_tokens
            log.debug(f"TabContext {self.tab_id}: cumulative tokens = {self.cumulative_tokens}")
        
        return response
    
    def _build_request(
        self,
        task: LLMTask,
        user_message: Optional[str] = None,
        strict_mode: bool = False,
        force: bool = False,
        section_override: Optional[list[str]] = None,
        **kwargs
    ) -> LLMRequest:
        """
        Build LLM request with tab-specific context and conditional artifact sending.
        
        This method can be used by tabs to create requests for async execution.
        
        Implements Phase 2 Architecture (Conditional Artifacts):
        - Only sends artifacts that are required for the task
        - Only sends artifacts if they've changed since last sent
        - First interaction sends all required artifacts
        - Subsequent interactions only send modified artifacts
        
        Args:
            task: LLM task to execute
            user_message: Optional user message
            strict_mode: Whether to enforce strict validation
            force: If True, send all required artifacts regardless of modification state
            **kwargs: Additional arguments passed to LLMRequest
            
        Returns:
            LLMRequest ready to send
        """
        # If force mode, override strict_mode to force LLM output
        if force:
            strict_mode = False
        
        # Track current task for validation
        self._current_task = task
        
        # Check if rules have changed
        current_rules_content = self.get_selected_rules_content()
        rules_changed = self._has_rules_changed(current_rules_content)
        
        # Build request with filtered rules (send on first interaction, force mode, OR when rules changed)
        if self._first_interaction or force or rules_changed:
            rules_content = current_rules_content
            reason = "first" if self._first_interaction else ("force" if force else "changed")
            log.debug(f"TabContext {self.tab_id}: Including rules (reason={reason})")
        else:
            rules_content = None  # Rules sent once, LLM remembers via conversation history
            log.debug(f"TabContext {self.tab_id}: Skipping rules (already sent, unchanged)")
        
        # Get tab-specific output contract. Resolve section ownership from the
        # bundle map (same source reconstruction enforces) so the prompt's
        # emit-list and reconstruction never diverge. ``section_override`` is
        # the per-task TaskConfig.llm_owned_sections (None → bundle default).
        ownership = reconstruction.pipeline_ownership(
            self.project_manager.project_root, section_override
        )
        output_contract = get_contract_for_tab(self.tab_id, ownership=ownership)
        
        # Safety check: session_state might not be initialized yet
        if not self.session_state:
            log.warning(f"TabContext {self.tab_id}: session_state not available")
            session_summary = ""
        else:
            session_summary = self.session_state.get_summary_for_llm()
        
        # Determine required artifacts for this task
        required_artifacts = self._get_required_artifacts_for_task(task)
        
        # Build context from artifacts with conditional sending
        context = self._build_conditional_artifact_context(required_artifacts, force=force)
        
        # Create request
        request = LLMRequest(
            task=task,
            strict_mode=kwargs.get('strict_mode', strict_mode),
            user_message=user_message or "",
            procedure_text=context.get("procedure_text"),
            procedure_json=context.get("procedure_json"),
            test_code=context.get("test_code"),
            session_summary=session_summary,
            rules_content=rules_content,  # Filtered rules only, sent once
            output_contract=output_contract,  # Tab-specific contract
        )
        
        return request
    
    def record_response(self, response: LLMResponse, validate_contract: bool = True):
        """
        Record an LLM response in message history.
        
        Use this when executing requests asynchronously (not through send_task).
        
        Args:
            response: The LLM response to record
            validate_contract: Whether to validate output contract (default True)
        """
        # Store assistant response in history BEFORE validation
        # This ensures users see the message even if validation fails
        if response.assistant_message:
            assistant_msg = ChatMessage(
                role="assistant",
                content=response.assistant_message,
                full_response=response.raw_response,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens
            )
            self.messages.append(assistant_msg)
            
            # Update cumulative tokens
            self.cumulative_tokens += response.total_tokens
            log.debug(f"TabContext {self.tab_id}: cumulative tokens = {self.cumulative_tokens}")
        
        # Validate contract if requested (may set response.success = False)
        if validate_contract and response.success:
            response = self._validate_contract(response)
        
        return response
    
    def _get_required_artifacts_for_task(self, task: LLMTask) -> list[str]:
        """
        Determine which artifacts are required for a given task.
        
        Args:
            task: The LLM task to execute
            
        Returns:
            List of artifact names required for this task
        """
        # Check task-specific requirements
        required = TASK_ARTIFACT_REQUIREMENTS.get(task)
        
        # If task has no specific requirements (e.g., AD_HOC_CHAT), use all tab artifacts
        if required is None:
            allowed = get_allowed_artifacts(self.tab_id)
            return allowed
        
        return required
    
    def _build_conditional_artifact_context(self, required_artifacts: list[str], force: bool = False) -> dict:
        """
        Build artifact context with conditional sending logic.
        
        Only includes artifacts in the context if:
        - They are required for the current task
        - They have been modified since last sent (or this is first interaction)
        
        Args:
            required_artifacts: List of artifact names required for current task
            force: If True, send all artifacts regardless of modification state
            
        Returns:
            Dictionary mapping artifact names to content (only modified/new artifacts)
        """
        # Safety check: artifact_manager might not be initialized yet
        if not self.artifact_manager:
            log.warning(f"TabContext {self.tab_id}: artifact_manager not available")
            return {}
        
        context = {}
        skipped = []
        
        # Check each required artifact
        if "procedure_text" in required_artifacts:
            content = self.artifact_manager.procedure_text.content
            if content:
                if self.should_send_artifact("procedure_text", force=force):
                    context["procedure_text"] = content
                    log.debug(f"TabContext {self.tab_id}: sending procedure_text (modified or first)")
                else:
                    skipped.append("procedure_text")
                    log.debug(f"TabContext {self.tab_id}: skipping procedure_text (unchanged)")
        
        if "procedure_json" in required_artifacts:
            content = self.artifact_manager.procedure_json.content
            if content:
                if self.should_send_artifact("procedure_json", force=force):
                    context["procedure_json"] = content
                    log.debug(f"TabContext {self.tab_id}: sending procedure_json (modified or first)")
                else:
                    skipped.append("procedure_json")
                    log.debug(f"TabContext {self.tab_id}: skipping procedure_json (unchanged)")
        
        if "test_code" in required_artifacts:
            content = self.artifact_manager.test_code.content
            if content:
                if self.should_send_artifact("test_code", force=force):
                    context["test_code"] = content
                    log.debug(f"TabContext {self.tab_id}: sending test_code (modified or first)")
                else:
                    skipped.append("test_code")
                    log.debug(f"TabContext {self.tab_id}: skipping test_code (unchanged)")
        
        # If artifacts were skipped, add a note to the user message
        if skipped:
            skip_note = f"\n\n[Note: The following artifacts are unchanged and not included: {', '.join(skipped)}]"
            # This note will be visible in the prompt but we'll handle it in the backend
            # For now, just log it
            log.info(f"TabContext {self.tab_id}: {len(skipped)} artifacts skipped (unchanged)")
        
        return context
    
    def _validate_contract(self, response: LLMResponse) -> LLMResponse:
        """
        Validate that response complies with tab's output contract.
        
        Performs two levels of validation:
        - Tab-level: Check against allowed artifacts for this tab
        - Task-level: Check against expected artifacts for current task
        
        Args:
            response: LLM response to validate
            
        Returns:
            Modified response with success=False if contract violated
        """
        # Level 1: Tab-level validation (existing)
        allowed = get_allowed_artifacts(self.tab_id)
        
        tab_violations = []
        
        # Check each proposal type against tab contract
        if response.procedure_text and response.procedure_text.mode and "procedure_text" not in allowed:
            tab_violations.append("procedure_text")
        
        if response.procedure_json and response.procedure_json.mode and "procedure_json" not in allowed:
            tab_violations.append("procedure_json")
        
        if response.test_code and response.test_code.mode and "test_code" not in allowed:
            tab_violations.append("test_code")
        
        if tab_violations:
            log.warning(f"TabContext {self.tab_id}: tab-level contract violation - proposed forbidden artifacts: {tab_violations}")
            response.success = False
            response.error_message = (
                f"Output contract violation: This tab ({self.tab_id}) does not allow "
                f"proposals for: {', '.join(tab_violations)}. "
                f"Allowed artifacts: {', '.join(allowed)}. "
                f"Please check the Raw Response tab for the full LLM output."
            )
            return response
        
        # Level 2: Task-level validation (new)
        if self._current_task:
            expected = get_task_expected_artifacts(self._current_task)
            
            # If task has specific expectations, validate them
            if expected is not None:
                task_violations = []
                
                # Check each proposal type against task expectations
                if response.procedure_text and response.procedure_text.mode and "procedure_text" not in expected:
                    task_violations.append("procedure_text")
                
                if response.procedure_json and response.procedure_json.mode and "procedure_json" not in expected:
                    task_violations.append("procedure_json")
                
                if response.test_code and response.test_code.mode and "test_code" not in expected:
                    task_violations.append("test_code")
                
                if task_violations:
                    log.warning(
                        f"TabContext {self.tab_id}: task-level contract violation for {self._current_task.value} - "
                        f"proposed unexpected artifacts: {task_violations}, expected: {expected}"
                    )
                    response.success = False
                    response.error_message = (
                        f"Task contract violation: The task '{self._current_task.value}' should only produce "
                        f"{', '.join(expected)}, but the LLM proposed: {', '.join(task_violations)}. "
                        f"Expected artifacts: {', '.join(expected)}. "
                        f"This may indicate the LLM misunderstood the task. "
                        f"Please check the Raw Response tab and consider re-running the task."
                    )
                    return response
        
        return response

    # Artifact tracking methods for conditional sending
    
    def should_send_artifact(self, artifact_name: str, force: bool = False) -> bool:
        """
        Determine if artifact should be sent (respecting force mode).
        
        Args:
            artifact_name: Name of artifact to check
            force: If True, always send (force mode)
            
        Returns:
            True if artifact should be sent
        """
        # Force mode: always send
        if force:
            return True
        
        # First interaction: send all required artifacts
        if self._first_interaction:
            return True
        
        # Otherwise, only send if modified
        content = self._get_artifact_content(artifact_name)
        if content is None:
            return False
        
        return self.is_artifact_modified(artifact_name, content)
    
    def _get_artifact_content(self, artifact_name: str) -> Optional[str]:
        """Get current content of an artifact."""
        if artifact_name == "procedure_text":
            return self.artifact_manager.procedure_text.content
        elif artifact_name == "procedure_json":
            return self.artifact_manager.procedure_json.content
        elif artifact_name == "test_code":
            return self.artifact_manager.test_code.content
        return None
    
    def is_artifact_modified(self, artifact_name: str, content: str) -> bool:
        """
        Check if artifact changed since last sent to LLM.
        
        Args:
            artifact_name: Name of artifact
            content: Current artifact content
            
        Returns:
            True if artifact has been modified, False otherwise
        """
        import hashlib
        current_hash = hashlib.md5(content.encode()).hexdigest()
        previous_hash = self._artifact_checksums.get(artifact_name)
        return previous_hash != current_hash
    
    def mark_artifact_sent(self, artifact_name: str, content: str):
        """
        Mark artifact as sent to LLM (store checksum).
        
        Args:
            artifact_name: Name of artifact
            content: Artifact content that was sent
        """
        import hashlib
        self._artifact_checksums[artifact_name] = hashlib.md5(
            content.encode()
        ).hexdigest()
        self._first_interaction = False
    
    def _has_rules_changed(self, current_rules_content: Optional[str]) -> bool:
        """
        Check if rules have changed since last sent to LLM.
        
        Args:
            current_rules_content: Current rules content (or None if no rules)
            
        Returns:
            True if rules have been modified, False otherwise
        """
        import hashlib
        
        # If no current rules, check if we previously had rules
        if current_rules_content is None:
            return self._rules_checksum is not None
        
        # If we have current rules but no stored checksum, rules have changed
        # This handles two cases:
        # 1. First time sending rules (checksum never set)
        # 2. Checksum deliberately cleared (e.g., rule selection changed)
        # In both cases, rules should be sent to the LLM
        if self._rules_checksum is None:
            return True  # Treat None checksum as "rules changed"
        
        # Compare checksums
        current_hash = hashlib.md5(current_rules_content.encode()).hexdigest()
        return current_hash != self._rules_checksum
    
    def _update_rules_checksum(self, rules_content: str):
        """
        Update stored checksum for rules content.
        
        Args:
            rules_content: Rules content that was sent
        """
        import hashlib
        self._rules_checksum = hashlib.md5(rules_content.encode()).hexdigest()
        log.debug(f"TabContext {self.tab_id}: rules checksum updated")
    
    def mark_artifact_modified(self, artifact_name: str):
        """
        Mark artifact as modified (will be sent on next request).
        
        This removes the checksum so the artifact will be re-sent.
        
        Args:
            artifact_name: Name of artifact that was modified
        """
        if artifact_name in self._artifact_checksums:
            del self._artifact_checksums[artifact_name]
            log.debug(f"TabContext {self.tab_id}: marked {artifact_name} as modified")
    
    def add_system_message(self, content: str, metadata: dict = None):
        """
        Add a system message to the conversation history.
        
        System messages record user actions and system events for LLM context.
        
        Args:
            content: System message content
            metadata: Optional metadata dictionary
        """
        from datetime import datetime
        import uuid
        
        system_msg = ChatMessage(
            role="system",
            content=content,
            msg_id=str(uuid.uuid4())
        )
        
        self.messages.append(system_msg)
        log.debug(f"TabContext {self.tab_id}: added system message: {content}")
