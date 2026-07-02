"""
Base Tab Widget - Common functionality for all editor tabs.
"""

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLabel, QFrame, QSplitter, QMessageBox, QGroupBox,
    QSizePolicy
)
from PySide6.QtCore import Signal, Qt
from typing import TYPE_CHECKING, Optional, Callable

from ..llm.backend_base import LLMTask
from ..theme import groupbox_bg, groupbox_border, groupbox_text, muted_text

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)


class BaseTab(QWidget):
    """
    Base class for all editor tabs.
    
    Provides common functionality:
    - Access to main window and managers
    - Button creation helpers
    - Status updates
    - Error handling
    """
    
    # Signals
    status_message = Signal(str)  # Emit status bar messages
    content_changed = Signal()     # Emit when content is modified
    artifact_saved = Signal()      # Emit after any artifact is written to disk
    
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._setup_ui()
    
    def _setup_ui(self):
        """Override in subclasses to setup UI."""
        pass
    
    @property
    def artifact_manager(self):
        """Get the artifact manager."""
        return self.main_window.artifact_manager
    
    @property
    def session_state(self):
        """Get the session state."""
        return self.main_window.session_state
    
    @property
    def project_manager(self):
        """Get the project manager."""
        return self.main_window.project_manager
    
    @property
    def task_config_manager(self):
        """Get the task configuration manager."""
        from ..core.task_config import TaskConfigManager
        manager = getattr(self.main_window, 'task_config_manager', None)
        # Only use task_config_manager if it's actually a TaskConfigManager instance
        if manager is not None and isinstance(manager, TaskConfigManager):
            return manager
        # Fallback to button_label_manager for backward compatibility
        manager = getattr(self.main_window, 'button_label_manager', None)
        if manager is None:
            log.warning("Neither task_config_manager nor button_label_manager found")
        return manager
    
    @property
    def button_label_manager(self):
        """
        Get the button label manager (backward compatibility alias).
        
        This property maintains backward compatibility with existing code
        that references button_label_manager. During migration:
        - If main_window has button_label_manager, return it directly
        - Otherwise return task_config_manager (which provides same interface)
        """
        # Check for button_label_manager first (during migration period)
        manager = getattr(self.main_window, 'button_label_manager', None)
        if manager is not None:
            return manager
        # Fall back to task_config_manager (after Task 5 completes)
        return getattr(self.main_window, 'task_config_manager', None)
    
    def create_button_row(self, *buttons) -> QHBoxLayout:
        """Create a horizontal layout with buttons."""
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 5, 0, 5)
        
        for button in buttons:
            if isinstance(button, str):
                # Create button from label
                btn = QPushButton(button)
                layout.addWidget(btn)
            elif isinstance(button, QPushButton):
                layout.addWidget(button)
            elif button is None:
                # Add stretch
                layout.addStretch()
        
        return layout
    
    def create_button(
        self, 
        text: str, 
        callback=None, 
        enabled: bool = True,
        tooltip: str = ""
    ) -> QPushButton:
        """Create a button with optional callback."""
        button = QPushButton(text)
        button.setEnabled(enabled)
        
        if tooltip:
            button.setToolTip(tooltip)
        
        if callback:
            button.clicked.connect(callback)
        
        return button
    
    def create_separator(self) -> QFrame:
        """Create a horizontal separator line."""
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line
    
    def show_error(self, title: str, message: str):
        """Show an error dialog."""
        QMessageBox.critical(self, title, message)
    
    def show_warning(self, title: str, message: str):
        """Show a warning dialog."""
        QMessageBox.warning(self, title, message)
    
    def show_info(self, title: str, message: str):
        """Show an info dialog."""
        QMessageBox.information(self, title, message)
    
    def ask_yes_no(self, title: str, message: str) -> bool:
        """Show a yes/no question dialog."""
        result = QMessageBox.question(
            self, title, message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        return result == QMessageBox.Yes
    
    def on_activated(self):
        """Called when this tab becomes active. Override as needed."""
        pass
    
    def on_deactivated(self):
        """Called when this tab is deactivated. Override as needed."""
        pass
    
    def refresh(self):
        """Refresh the tab content. Override as needed."""
        pass
    
    def sync_editors_to_artifacts(self):
        """Sync editor content to ArtifactManager without saving to disk.

        Called before dirty checks, tab switches, and saves to ensure
        ArtifactManager has the latest editor content.

        Override in subclasses that have editors.
        """
        pass

    # ------------------------------------------------------------------
    # Validator buttons (Phase 2/3 — registry-driven)
    # ------------------------------------------------------------------

    def _build_validator_buttons(self, parent_layout: QHBoxLayout) -> int:
        # Remember the layout so ``rebuild_validator_buttons`` can refresh
        # it when the active project (and therefore the validator config)
        # changes.
        self._validator_button_layout = parent_layout
        return self._populate_validator_buttons(parent_layout)

    def rebuild_validator_buttons(self) -> None:
        """Rebuild this tab's validator buttons against the current
        TaskConfigManager state. Called by
        ``MainWindow.refresh_all_button_labels`` after
        ``task_config_manager.reload(project_root)`` so that opening a
        project surfaces its configured validators."""
        layout = getattr(self, "_validator_button_layout", None)
        if layout is None:
            return
        # Remove existing validator buttons (tagged with the validator_id
        # property). Leave any non-validator widgets the tab put in the
        # same row (e.g. Format JSON) in place.
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QPushButton) and widget.property("validator_id"):
                layout.removeWidget(widget)
                widget.deleteLater()
        self._populate_validator_buttons(layout)

    def _populate_validator_buttons(self, parent_layout: QHBoxLayout) -> int:
        """Populate *parent_layout* with one button per registered validator
        listed in ``workflows.<tab>.validators``.

        Returns the count of buttons actually added. Skips disabled
        specs and validators whose id has no matching registry entry
        (with a warning log so the operator can see why a config-
        declared validator failed to appear).

        Modal feedback is dropped universally (plan decision 2026-05-10);
        outcomes route to the dock panel only. The tab's
        ``status_message`` signal carries a one-line summary for the
        status bar.
        """
        from ..core.validators_registry import (
            ensure_builtins_registered,
            is_registered,
        )

        manager = self.task_config_manager
        tab_id = getattr(self, "tab_id", None)
        if manager is None or tab_id is None:
            return 0
        if not hasattr(manager, "get_validator_specs_for_tab"):
            return 0  # older TaskConfigManager — Phase 1 install

        # Probe validator availability so unusable buttons don't render.
        # Two levels (Phase 4.6):
        #   - Master toggle (validator_loop.enabled=false): hide ALL
        #     validator buttons. Operator explicitly opted out.
        #   - validate_procedure-specific availability: hide ONLY the
        #     "Validate Procedure" button when its deps (text_renderer +
        #     pack-side bijective handler) aren't present. Other
        #     validators (python_syntax) are stateless and
        #     keep showing — they only "skip" if the relevant artifact
        #     is empty at click time.
        project_root = getattr(self.project_manager, "project_root", None)
        hide_procedure_button = False
        try:
            if project_root is not None:
                from ..llm.validator_loop_settings import is_enabled
                if not is_enabled(project_root):
                    return 0
                from ..llm.validator_dispatch import is_loop_available
                loop_avail, _ = is_loop_available(project_root)
                hide_procedure_button = not loop_avail
        except Exception:
            log.debug("validator availability probe failed; rendering buttons",
                      exc_info=True)

        ensure_builtins_registered()
        specs = manager.get_validator_specs_for_tab(tab_id)

        added = 0
        for spec in specs:
            vid = spec.get("id")
            if not vid or not spec.get("enabled", True):
                continue
            if not is_registered(vid):
                log.warning(
                    "Tab %s: validator id %r is not in the registry; skipping button.",
                    tab_id, vid,
                )
                continue
            # Hide the Validate Procedure button when its deps aren't
            # available — config-issue case (Phase 4.6 fix).
            if hide_procedure_button and vid.endswith(".validate_procedure"):
                continue
            label = spec.get("label") or self._derive_validator_label(vid)
            tooltip = spec.get("tooltip") or f"Run validator: {vid}"
            btn = self.create_button(
                label,
                # default-arg capture so the loop variable isn't late-bound.
                callback=lambda _checked=False, _vid=vid: self._run_validator(_vid),
                tooltip=tooltip,
            )
            btn.setProperty("validator_id", vid)
            parent_layout.addWidget(btn)
            added += 1
        return added

    def _derive_validator_label(self, validator_id: str) -> str:
        """Fallback label for a validator without an explicit ``label`` —
        e.g. ``rules_packager_base.validate_procedure`` → ``Validate Procedure``."""
        short = validator_id.rsplit(".", 1)[-1]
        return short.replace("_", " ").title()

    def _get_artifact_for_validation(self, name: str) -> Optional[str]:
        """Return the *current* (live editor) content for a validator.

        Default implementation reads from the artifact manager (which may
        be slightly behind the live editor if the user hasn't synced).
        Tabs with editors should override to read the editor directly
        OR call ``sync_editors_to_artifacts()`` before validation.
        """
        am = getattr(self.main_window, "artifact_manager", None)
        if am is None:
            return None
        artifact = None
        if name == "procedure_text":
            artifact = getattr(am, "procedure_text", None)
        elif name == "procedure_json":
            artifact = getattr(am, "procedure_json", None)
        elif name == "test_code":
            artifact = getattr(am, "test_code", None)
        return artifact.content if artifact else None

    def _run_validator(self, validator_id: str) -> None:
        """Look up *validator_id*, build a ``ValidatorContext`` from the
        tab's live state, dispatch, and route the outcome to the dock."""
        from ..core.validators_registry import (
            ValidatorContext,
            get as get_validator,
        )
        from ..llm.validator_dispatch import render_validation_outcome_summary

        # Push editor → artifact_manager so live content is visible to
        # validators reading via the default ``_get_artifact_for_validation``.
        try:
            self.sync_editors_to_artifacts()
        except Exception:
            log.debug("sync_editors_to_artifacts raised; continuing", exc_info=True)

        try:
            validator = get_validator(validator_id)
        except KeyError:
            log.error("Validator %r not registered at click time", validator_id)
            self.status_message.emit(
                f"Validator {validator_id!r} is not registered (see logs)."
            )
            return

        ctx = ValidatorContext(
            artifact_text=self._get_artifact_for_validation("procedure_text"),
            artifact_json=self._get_artifact_for_validation("procedure_json"),
            artifact_code=self._get_artifact_for_validation("test_code"),
            project_root=self.project_manager.project_root,
            tab_id=getattr(self, "tab_id", ""),
        )

        try:
            outcome = validator(ctx)
        except Exception as exc:
            log.exception("Validator %s raised: %s", validator_id, exc)
            self.status_message.emit(f"Validator {validator_id} crashed; see logs.")
            return

        dock = getattr(self.main_window, "dock", None)
        chat_panel = getattr(dock, "chat_panel", None) if dock else None
        label = self._derive_validator_label(validator_id)

        if outcome.skipped:
            # Surface the skip reason in the dock's findings panel so
            # "Validate Procedure" doesn't appear to be a silent no-op
            # (Phase 4.6 fix). A single info-severity row with the
            # validator id + skip reason replaces the stale findings.
            reason_text = outcome.reason or f"{label}: skipped."
            if dock is not None:
                dock.show_validation_result_from_list([{
                    "severity": "info",
                    "code": "VALIDATOR_SKIPPED",
                    "message": reason_text,
                    "location": validator_id,
                }])
            self.status_message.emit(reason_text)
            # Post a system message in the chat so the operator sees
            # that the click did something — the findings panel can be
            # hidden / off-screen.
            if chat_panel is not None:
                chat_panel.add_system_message(f"⚙️ {label}: skipped — {reason_text}")
            return

        if dock is not None:
            dock.show_validation_result_from_list(
                [issue.to_dock_dict() for issue in outcome.issues]
            )
        summary = render_validation_outcome_summary(outcome)
        self.status_message.emit(summary)
        # Chat-panel echo: makes it obvious the button fired and
        # whether the run passed / found issues. Without this, a clean
        # pass clears the findings panel silently and users assume the
        # button didn't work.
        if chat_panel is not None:
            chat_panel.add_system_message(f"⚙️ {label}: {summary}")
    
    def save_all_artifacts(self):
        """Save all artifacts managed by this tab (sync + save + reset dirty).
        
        This is the correct entry point for saving from outside the tab
        (e.g. Ctrl+S, Save All). It syncs editor content first, saves to
        disk, resets dirty flags, and updates status labels.
        
        Override in subclasses. Default implementation does nothing.
        """
        pass
    
    def has_unsaved_changes(self) -> bool:
        """Check if this tab has unsaved changes in its editors.
        
        Returns True if any editor has been modified since last save.
        Override in subclasses. Default returns False.
        """
        return False
    
    # Button Label Management
    
    def create_action_group(self, title: str, style: str = "file") -> QGroupBox:
        """
        Create a styled action group box for visually separating button types.
        
        Args:
            title: The group box title
            style: Visual style - "file" (light blue) or "llm" (light purple)
            
        Returns:
            QGroupBox with appropriate styling applied
            
        Example:
            >>> file_group = self.create_action_group("File Actions", "file")
            >>> llm_group = self.create_action_group("LLM Actions", "llm")
        """
        group = QGroupBox(title)
        
        # Define style colors
        bg_color = groupbox_bg(style)
        border_col = groupbox_border(style)
        
        # Apply stylesheet
        group.setStyleSheet(f"""
            QGroupBox {{
                background-color: {bg_color};
                border: 1px solid {border_col};
                border-radius: 4px;
                margin-top: 8px;
                padding: 12px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                color: {groupbox_text()};
            }}
        """)
        
        return group
    
    def create_task_button(
        self,
        task: LLMTask,
        callback: Callable,
        force_mode: bool = False,
        enabled: bool = True,
        tooltip: str = "",
        tab_name: Optional[str] = None,
        task_id_override: Optional[str] = None
    ) -> QPushButton:
        """
        Create a button for an LLM task with customizable label from TaskConfigManager.
        
        This method creates a button that:
        - Uses custom labels from TaskConfigManager (or defaults)
        - Stores task metadata as properties for later refresh
        - Supports both strict and force modes
        - Can be refreshed when settings change
        
        Args:
            task: The LLMTask this button triggers
            callback: Function to call when button is clicked
            force_mode: If True, button triggers force mode (bypasses validation)
            enabled: Initial enabled state
            tooltip: Optional tooltip text (auto-generated if empty)
            tab_name: Optional tab name for label lookup (uses class name if None)
            
        Returns:
            QPushButton configured with the task label and metadata
            
        Example:
            >>> btn = self.create_task_button(
            ...     task=LLMTask.REVIEW_JSON,
            ...     callback=self._on_review_clicked,
            ...     tooltip="Review the JSON for errors"
            ... )
        """
        # Determine tab name for label lookup
        if tab_name is None:
            tab_name = getattr(self, 'tab_id', self.__class__.__name__.replace("Tab", "").lower())
        
        # Get label from TaskConfigManager
        label = self._get_task_label(task, tab_name, task_id_override=task_id_override)
        
        # Create button with label
        button = QPushButton(label)
        button.setEnabled(enabled)
        button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        
        # Store metadata as properties for refresh_button_labels()
        button.setProperty("llm_task", task)
        button.setProperty("force_mode", force_mode)
        button.setProperty("tab_name", tab_name)
        button.setProperty("task_id_override", task_id_override)
        
        # Auto-generate tooltip if not provided
        if not tooltip:
            mode_suffix = " (Force Mode)" if force_mode else ""
            tooltip = f"{label}{mode_suffix}\nTask: {task.value}"
        
        button.setToolTip(tooltip)
        
        # Connect callback
        if callback:
            button.clicked.connect(callback)
        
        return button
    
    def refresh_button_labels(self):
        """
        Refresh all LLM button labels after settings change.
        
        This method finds all buttons with llm_task property and updates
        their text to reflect current TaskConfigManager settings.
        Should be called after:
        - Settings dialog changes button labels
        - Tab contexts are modified
        - Switching themes/profiles that affect labels
        
        Example:
            >>> # After user changes button labels in settings
            >>> self.refresh_button_labels()
        """
        # Find all buttons with llm_task property
        buttons = self.findChildren(QPushButton)
        refreshed_count = 0
        
        for button in buttons:
            task = button.property("llm_task")
            if task is not None:
                # This is an LLM task button, refresh its label
                tab_name = button.property("tab_name")
                if tab_name is None:
                    tab_name = self.__class__.__name__.replace("Tab", "").lower()
                task_id_override = button.property("task_id_override")
                
                # Get updated label from TaskConfigManager
                new_label = self._get_task_label(task, tab_name, task_id_override=task_id_override)
                button.setText(new_label)
                refreshed_count += 1
        
        if refreshed_count > 0:
            log.debug(f"Refreshed {refreshed_count} button label(s) in {self.__class__.__name__}")
    
    # --- Dynamic LLM Button Building ---
    # Subclasses must override _get_task_callback_map() and _get_force_callback_map()
    
    def _get_task_callback_map(self) -> dict:
        """Return mapping of task_id -> (callback, tooltip).
        
        Must be overridden by subclasses to provide tab-specific callbacks.
        Keys are LLMTask enum values (strings), values are (callable, str) tuples.
        """
        return {}
    
    def _get_force_callback_map(self) -> dict:
        """Return mapping of task_id -> (force_callback, tooltip) for force-mode buttons.
        
        Override in subclasses that support force-mode for specific tasks.
        """
        return {}
    
    def _create_llm_action_group(self) -> QGroupBox:
        """Create the LLM Actions group box with dynamically built buttons.
        
        Call this from _create_actions() in subclasses to get the LLM button group.
        The group is stored as self._llm_group with layout self._llm_group_layout.
        
        Returns:
            QGroupBox containing dynamically built LLM buttons
        """
        self._llm_group = self.create_action_group("LLM Actions", "llm")
        self._llm_group_layout = QVBoxLayout(self._llm_group)
        self._build_llm_buttons()
        return self._llm_group
    
    def _build_llm_buttons(self):
        """Build LLM action buttons dynamically from TaskConfigManager.
        
        Reads enabled tasks for this tab and creates buttons accordingly.
        Known tasks (in _get_task_callback_map) get specific callbacks;
        unknown/custom tasks get a generic handler.
        
        AD_HOC_CHAT tasks are filtered out (handled by the chat panel).
        """
        layout = self._llm_group_layout
        
        # Clear existing LLM buttons
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())
        
        # Get enabled tasks from config
        manager = self.task_config_manager
        if manager is None:
            return
        
        tab_id = getattr(self, 'tab_id', None)
        if tab_id is None:
            return
        
        enabled_tasks = manager.get_enabled_tasks_for_tab(tab_id)
        callback_map = self._get_task_callback_map()
        force_map = self._get_force_callback_map()
        
        # Filter out AD_HOC_CHAT (handled by chat panel, not a button)
        button_tasks = [t for t in enabled_tasks if t.id != LLMTask.AD_HOC_CHAT.value]
        
        if not button_tasks:
            placeholder = QLabel("No LLM tasks configured")
            placeholder.setStyleSheet(f"color: {muted_text()}; font-style: italic;")
            layout.addWidget(placeholder)
            return
        
        # Build rows of buttons (up to 4 widgets per row)
        row_layout = None
        col_count = 0
        for task in button_tasks:
            # Start new row if current row is full or doesn't exist
            if col_count % 4 == 0:
                if row_layout:
                    row_layout.addStretch()
                row_layout = QHBoxLayout()
                layout.addLayout(row_layout)
                col_count = 0
            
            # Determine callback
            if task.id in callback_map:
                callback, tooltip = callback_map[task.id]
            else:
                callback = self._make_generic_task_callback(task.id)
                tooltip = f"Run custom task: {task.name}"
            
            # Create button with task_id_override for correct label lookup
            btn = self.create_task_button(
                self._resolve_llm_task(task.id),
                callback,
                tooltip=tooltip,
                task_id_override=task.id
            )
            row_layout.addWidget(btn)
            col_count += 1
            
            # Force-mode button (if available for this task)
            if task.id in force_map:
                # Start new row if needed
                if col_count % 4 == 0:
                    row_layout.addStretch()
                    row_layout = QHBoxLayout()
                    layout.addLayout(row_layout)
                    col_count = 0
                
                force_callback, force_tooltip = force_map[task.id]
                force_btn = self.create_task_button(
                    self._resolve_llm_task(task.id),
                    force_callback,
                    force_mode=True,
                    tooltip=force_tooltip,
                    task_id_override=task.id
                )
                row_layout.addWidget(force_btn)
                col_count += 1
        
        # Add stretch to last row
        if row_layout:
            row_layout.addStretch()
    
    def rebuild_llm_buttons(self):
        """Rebuild LLM buttons after settings change.
        
        Called by MainWindow.refresh_all_button_labels() when the user
        saves changes in the Settings dialog.
        """
        self._build_llm_buttons()
        log.debug(f"Rebuilt LLM buttons for {self.__class__.__name__}")
    
    @staticmethod
    def _clear_layout(layout):
        """Recursively clear all items from a layout."""
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                BaseTab._clear_layout(child.layout())
    
    @staticmethod
    def _resolve_llm_task(task_id: str) -> LLMTask:
        """Resolve a task_id string to an LLMTask enum, with fallback."""
        try:
            return LLMTask(task_id)
        except ValueError:
            return LLMTask.AD_HOC_CHAT  # fallback for custom tasks
    
    def _make_generic_task_callback(self, task_id: str):
        """Create a generic callback for custom/unknown tasks.
        
        Custom tasks use AD_HOC_CHAT as the LLM routing task but pass their
        custom_task_id so the correct prompt_template is looked up.
        """
        def _callback():
            log.info(f"Running custom task: {task_id}")
            if hasattr(self, '_run_task_async'):
                self._run_task_async(LLMTask.AD_HOC_CHAT, custom_task_id=task_id)
            else:
                log.warning(f"Tab {self.__class__.__name__} has no _run_task_async method")
        return _callback
    
    def _get_task_label(self, task: LLMTask, tab_name: str, task_id_override: str = None) -> str:
        """
        Get task button label from TaskConfigManager with fallback.
        
        Args:
            task: The LLMTask to get label for
            tab_name: Tab identifier (e.g., "text_json", "json_code")
            task_id_override: Optional task ID to use for lookup instead of task.value
        
        Returns:
            Button label string
        """
        manager = self.task_config_manager
        
        # Try TaskConfigManager first
        if manager is not None:
            from ..core.task_config import TaskConfigManager
            if isinstance(manager, TaskConfigManager):
                lookup_id = task_id_override if task_id_override else task.value
                task_config = manager.get_task_config(tab_name, lookup_id)
                if task_config is not None:
                    return task_config.button_label
                else:
                    log.warning(f"Task config not found for {tab_name}.{lookup_id}, using fallback label")
            # Handle old ButtonLabelManager for backward compatibility
            elif hasattr(manager, 'get_label'):
                label = manager.get_label(task, tab_name)
                # Ensure we return a string
                if isinstance(label, str):
                    return label
        
        # Fallback: generate label from task name
        fallback_label = task.value.replace('_', ' ').title()
        log.debug(f"Using fallback label '{fallback_label}' for task {task.value}")
        return fallback_label
