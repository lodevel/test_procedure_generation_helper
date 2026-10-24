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
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
from pathlib import Path

from . import reconstruction
from .backend_base import LLMBackend, LLMRequest, LLMTask
from .backend_factory import request_backend
from .output_contracts import get_contract_for_tab, get_allowed_artifacts
from ..core.fileio import content_digest
from ..core.project_manager import RuleDoc
from ..core.rule_context import attached_files, chosen_rule_docs, rules_prompt_text
from ..core.session_state import SessionState

if TYPE_CHECKING:
    from ..core import ProjectManager, ArtifactManager
    from ..core.task_config import TaskConfigManager
    from .backend_factory import BackendFactory

log = logging.getLogger(__name__)


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
    tone: str = "neutral"  # a system line's pill tone: success / warning / danger / neutral
    
    def __post_init__(self):
        """Generate UUID if msg_id not provided."""
        import uuid
        self.msg_id = self.msg_id or str(uuid.uuid4())


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
        """
        self.tab_id = tab_id
        self.board_data_enabled = False
        self.board_data_project = None
        self._backend_factory = backend_factory
        self._backend: Optional[LLMBackend] = None  # started on first use: start_backend
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
        
        
        log.info(
            f"TabContext initialized for {tab_id} (project_root={self.project_manager.project_root})"
        )
    
    @property
    def backend(self) -> Optional[LLMBackend]:
        """The tab's backend once one was started and kept (:meth:`start_backend`), else None."""
        return self._backend

    def start_backend(self, send) -> None:
        """Start a backend for this tab, the GUI thread never waiting on the server
        (``request_backend``): ``send`` gets ``(factory, backend, keep)``, for
        :meth:`adopt_backend`."""
        factory = self._backend_factory
        request_backend(factory, self.tab_id, lambda started: send((factory, *started)))

    def adopt_backend(self, answer) -> LLMBackend:
        """The backend :meth:`start_backend` answered: kept as the tab's as ``start_backend``
        says, unless the tab's factory changed meanwhile (a settings swap): then it serves the
        one send that waited for it."""
        factory, backend, keep = answer
        if keep and factory is self._backend_factory:
            self._backend = backend
        return backend
    
    def update_managers(self, artifact_manager: "ArtifactManager"):
        """Update manager references after test is opened.
        
        This fixes the issue where TabContext is created with None managers
        during tab initialization, but real managers are created later when
        a test is opened.
        
        Also reloads the tab configuration now that project_root is available,
        replacing the default 'all' config with the actual project's config file.
        
        Args:
            artifact_manager: Updated artifact manager instance
        """
        self.artifact_manager = artifact_manager

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
        log.info(f"TabContext {self.tab_id}: conversation reset (_first_interaction=True, optimization state cleared)")
    
    def get_selected_rules(self) -> list[str]:
        """Resolve the rule document keys to send for the current LLM request.

        After Phase 1 of the workflows-to-project-config refactor,
        ``selected_rules`` is per-task. We pick the task that's actively
        being executed (``self._current_task``) when one is set, and fall
        back to the tab's first task otherwise. The ``"all"`` sentinel
        expands against every rule document: the bundle's and the project's.
        """
        raw = self._raw_selected_rules_for_current_task()
        if isinstance(raw, list):
            return list(raw)
        if raw is not None and raw != "all":
            log.warning(
                f"TabContext {self.tab_id}: unexpected selected_rules type %r; falling back to all",
                type(raw).__name__,
            )
        return [doc.key for doc in self.project_manager.get_rule_docs()]

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
    
    def update_backend_factory(self, new_factory: "BackendFactory"):
        """
        Update backend factory and recreate backend on next use.
        
        This is the preferred way to change backend configuration.
        The existing backend is discarded and a new one will be created
        on the next send (:meth:`start_backend`).
        
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
        self._first_interaction = True
        
        log.info(f"TabContext {self.tab_id}: backend session reset, optimization state cleared")
    
    
    def _update_optimization_state(self, request: LLMRequest):
        """
        Update optimization state after building request.
        
        Tracks which content was sent so we can skip unchanged content
        in subsequent requests.
        
        Args:
            request: The request that was built and will be sent
        """
        # Track artifact checksums and first-interaction flag
        for name in ("procedure_text", "procedure_json", "test_code"):
            content = getattr(request, name, None)
            if content:
                self.mark_artifact_sent(name, content)
        
        # Track rules content if included
        if request.rules_content:
            self._update_rules_checksum(request.rules_content, request.attachments)
            log.debug(f"TabContext {self.tab_id}: Updated rules checksum")
    
    def confirm_request_delivered(self, request: LLMRequest) -> None:
        """Mark artifacts and rules in *request* as successfully delivered.

        Call this **only** after the backend has returned a successful
        response.  This prevents the optimisation state from advancing
        when the server is unreachable and a ``NoneBackend`` is used.
        """
        self._update_optimization_state(request)
        log.debug(f"TabContext {self.tab_id}: Optimization state confirmed after successful delivery")
    
    def set_selected_rules(self, rule_filenames: list[str]) -> bool:
        """Update selected rules for this tab and persist via TaskConfigManager; False when
        the save failed (the selection still applies now).

        Phase 1 keeps the UI tab-level: the same list propagates to every
        task in the tab. Per-task selection lands in Phase 4's editor.
        """
        self.task_config_manager.set_selected_rules_for_tab(self.tab_id, rule_filenames)
        saved = self.task_config_manager.save_config()

        # Clear rules checksum so the next LLM request resends rules.
        self._rules_checksum = None

        log.info(
            f"TabContext {self.tab_id}: rules updated to {len(rule_filenames)} selected, "
            "checksum cleared"
        )
        return saved
    
    def _selected_rule_docs(self) -> list[RuleDoc]:
        """The selected rule documents on disk, bundle rules first (missing ones logged)."""
        return chosen_rule_docs(self.project_manager.get_rule_docs(), self.get_selected_rules())

    def get_selected_rules_files(self) -> list[Path]:
        """The selected documents sent as they are, attached as files (PDFs)."""
        return attached_files(self._selected_rule_docs())

    def get_selected_rules_content(self) -> Optional[str]:
        """The selected rule documents' text for the LLM (None: none), see
        ``rule_context.rules_prompt_text``."""
        return rules_prompt_text(self._selected_rule_docs())

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
        
        - Only sends artifacts that are required for the task
        - Only sends artifacts if they've changed since last sent
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
        current_rules_files = self.get_selected_rules_files()
        rules_changed = self._has_rules_changed(current_rules_content, current_rules_files)
        
        # Build request with filtered rules (send on first interaction, force mode, OR when rules changed)
        if self._first_interaction or force or rules_changed:
            rules_content, rules_files = current_rules_content, current_rules_files
            reason = "first" if self._first_interaction else ("force" if force else "changed")
            log.debug(f"TabContext {self.tab_id}: Including rules (reason={reason})")
        else:
            rules_content, rules_files = None, []  # sent once, the conversation remembers them
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
            attachments=rules_files,  # the selected PDFs, with the rules
            output_contract=output_contract,  # Tab-specific contract
            # Bundle-declared CUSTOM tasks route via AD_HOC_CHAT but carry
            # their own id so prompt resolution uses THEIR prompt_template.
            custom_task_id=kwargs.get('custom_task_id'),
        )
        
        from .board_chat import configure_board_request
        configure_board_request(request, self)
        return request
    
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
        
        if skipped:
            log.info(f"TabContext {self.tab_id}: {len(skipped)} artifacts skipped (unchanged)")
        
        return context
    
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
    
    def _has_rules_changed(self, content: Optional[str], files: list[Path] = ()) -> bool:
        """True when the rules (*content* and the attached *files*) differ from those last
        sent; no checksum yet (never sent, or the selection changed) counts as changed."""
        if content is None:
            return self._rules_checksum is not None
        return self._rules_checksum != content_digest(content, files)

    def _update_rules_checksum(self, content: str, files: list[Path] = ()) -> None:
        """Remember the rules sent: *content* and the attached *files*."""
        self._rules_checksum = content_digest(content, files)
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
