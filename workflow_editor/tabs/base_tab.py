"""
Base Tab Widget - Common functionality for all editor tabs.
"""

import json
import logging
from collections.abc import Iterable
from PySide6.QtWidgets import (
    QWidget, QPushButton,
    QLayout, QMessageBox, QToolButton
)
from PySide6.QtCore import Signal
from typing import TYPE_CHECKING, Optional, Callable

from project_services import ui_kit

from ..llm.backend_base import LLMTask
from ..widgets.action_bar import ActionBar, set_task_label, task_button
from ..widgets.editor_card import EditorCard

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
        #: The view's editor cards (``add_card``): loaded, synced and saved together.
        self.cards: list[EditorCard] = []
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
    
    def add_card(self, card: EditorCard) -> EditorCard:
        """Register an editor card: its edits mark the artifact modified for the LLM context
        and repaint the header (``content_changed``)."""
        self.cards.append(card)
        card.changed.connect(lambda: self._on_card_changed(card))
        return card

    def _on_card_changed(self, card: EditorCard) -> None:
        tab_context = getattr(self, "tab_context", None)
        if tab_context is not None:
            tab_context.mark_artifact_modified(card.artifact_name)
        self.content_changed.emit()

    def card_for(self, artifact_name: str) -> Optional[EditorCard]:
        """The card editing *artifact_name* ("procedure_text", …) in this view, if any."""
        return next((c for c in self.cards if c.artifact_name == artifact_name), None)

    def sync_editors_to_artifacts(self):
        """Sync editor content to ArtifactManager without saving to disk.

        Called before dirty checks, tab switches, and saves to ensure
        ArtifactManager has the latest editor content.
        """
        if not self.artifact_manager:
            return
        for card in self.cards:
            card.sync()

    def load_content(self):
        """Show the artifacts' current content in the view's editors."""
        if not self.artifact_manager:
            return
        for card in self.cards:
            card.load()

    def _on_format_json(self):
        """Format JSON with proper indentation (the view's procedure.json editor)."""
        card = self.card_for("procedure_json")
        try:
            parsed = json.loads(card.editor.toPlainText())
            card.set_text(json.dumps(parsed, indent=2))
            self.status_message.emit("JSON formatted successfully")
        except json.JSONDecodeError as e:
            self.show_error("Format Failed", f"Invalid JSON: {e}")
        except Exception as e:
            self.show_error("Format Failed", str(e))

    # ------------------------------------------------------------------
    # Validator buttons (Phase 2/3 — registry-driven)
    # ------------------------------------------------------------------

    def _build_validator_buttons(self, parent_layout: QLayout) -> int:
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

    def _populate_validator_buttons(self, parent_layout: QLayout) -> int:
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
                f"✓ {label}",
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
        """Return the *current* (live editor) content for a validator: the
        view's editor for that artifact when it has one, else the artifact
        manager's copy.
        """
        card = self.card_for(name)
        if card is not None:
            return card.editor.toPlainText() or None
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
        (e.g. Ctrl+S, Save All): each editor card writes its artifact.
        """
        for card in self.cards:
            card.save()

    # ------------------------------------------------------------------
    # Shared by the LLM views: the chat's task line, a procedure.json proposal
    # ------------------------------------------------------------------

    #: The chat's line for a built-in task the configuration does not name.
    _TASK_DESCRIPTIONS = {
        LLMTask.DERIVE_JSON_FROM_TEXT: "Generate structured JSON from procedure text",
        LLMTask.RENDER_TEXT_FROM_JSON: "Generate human-readable text from JSON",
        LLMTask.REVIEW_TEXT_PROCEDURE: "Review procedure text for quality",
        LLMTask.REVIEW_JSON: "Review JSON structure and content",
        LLMTask.REVIEW_TEXT_VS_JSON: "Check coherence between text and JSON",
        LLMTask.GENERATE_CODE_FROM_JSON: "Generate executable test code from JSON",
        LLMTask.DERIVE_JSON_FROM_CODE: "Extract JSON structure from existing code",
        LLMTask.REVIEW_CODE: "Review test code quality and structure",
        LLMTask.REVIEW_CODE_VS_JSON: "Check coherence between JSON and code",
        LLMTask.AD_HOC_CHAT: "General assistance",
    }

    def _get_task_description(
        self, task: LLMTask, user_message: str = None, custom_task_id: str = None
    ) -> str:
        """The chat's line for a task: a custom task's configured name, the
        operator's own message for ad-hoc chat, else the configured task's name."""
        manager = self.task_config_manager
        if custom_task_id and manager:
            task_config = manager.get_task_config(self.tab_id, custom_task_id)
            if task_config:
                return f"Run: {task_config.name}"

        if task == LLMTask.AD_HOC_CHAT and user_message:
            return user_message

        # Built-in tasks are ALSO configurable (they live in the same tasks list),
        # so source the chat message from the configured task's name — editing the
        # button in the Template Manager then updates this message too, not just
        # custom tasks. Falls through to the static map only when unconfigured.
        if manager:
            tc = manager.get_task_config(self.tab_id, task.value)
            if tc and tc.name:
                return tc.name
        return self._TASK_DESCRIPTIONS.get(task, f"Run {task.name}")

    def _handle_json_proposal(self, proposal):
        """Offer a procedure.json proposal (``self.json_card``) as a diff; apply
        it when accepted.

        Accepts "create" alongside "replace" — semantically identical (full
        content for the artifact). "patch" is intentionally NOT included —
        partial-edit semantics need a different handler. Proposed JSON is
        re-dumped with indent=2 and ensure_ascii=False (µ, ±, Ω stay literal);
        content that does not parse is shown as it came.
        """
        from ..dialogs import DiffViewer
        if proposal.mode not in ("replace", "create"):
            return
        if isinstance(proposal.content, dict):
            proposed = dict(proposal.content)
        else:
            try:
                proposed = json.loads(str(proposal.content))
            except json.JSONDecodeError:
                proposed = None
        content_str = (json.dumps(proposed, indent=2, ensure_ascii=False)
                       if proposed is not None else str(proposal.content))

        accepted, final_content = DiffViewer.show_diff(
            self.json_card.editor.toPlainText(),
            content_str,
            "Review Changes: procedure.json",
            self
        )
        if accepted:
            self.json_card.set_text(final_content)
            self.main_window.dock.chat_panel.add_system_message("✓ Applied changes to procedure.json", tone="success")
        else:
            self.main_window.dock.chat_panel.add_system_message("✗ Rejected changes to procedure.json", tone="danger")

    # Button Label Management

    def create_task_button(
        self,
        task: LLMTask,
        callback: Callable,
        enabled: bool = True,
        tooltip: str = "",
        tab_name: Optional[str] = None,
        task_id_override: Optional[str] = None,
        force_callback: Optional[Callable] = None,
        force_tooltip: str = "",
    ) -> QToolButton:
        """
        Create an assistant button for an LLM task, labelled from TaskConfigManager.

        The button stores its task metadata as properties so
        ``refresh_button_labels`` can relabel it when settings change. With
        *force_callback* it is a split button: its ▾ menu offers the task and
        its force variant (validation bypassed).

        Args:
            task: The LLMTask this button triggers
            callback: Function to call when button is clicked
            enabled: Initial enabled state
            tooltip: Optional tooltip text (auto-generated if empty)
            tab_name: Optional tab name for label lookup (uses class name if None)
            task_id_override: Task id for the label lookup instead of task.value
            force_callback: The force variant's callback (a split button)
            force_tooltip: The force variant's tooltip
        """
        # Determine tab name for label lookup
        if tab_name is None:
            tab_name = getattr(self, 'tab_id', self.__class__.__name__.replace("Tab", "").lower())

        # Get label from TaskConfigManager
        label = self._get_task_label(task, tab_name, task_id_override=task_id_override)
        button = task_button(label, callback, force_callback=force_callback,
                             force_tooltip=force_tooltip)
        button.setEnabled(enabled)

        # Store metadata as properties for refresh_button_labels()
        button.setProperty("llm_task", task)
        button.setProperty("tab_name", tab_name)
        button.setProperty("task_id_override", task_id_override)
        button.setToolTip(tooltip or f"{label}\nTask: {task.value}")
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
        buttons = self.findChildren(QToolButton)
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
                set_task_label(button, new_label)
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
    
    def _create_action_bar(self, file_buttons: Iterable[QWidget] = (),
                           pack_buttons: Iterable[QWidget] = ()) -> ActionBar:
        """The view's action bar (``self.action_bar``): the assistant row built
        from TaskConfigManager, the FILE row with *file_buttons*, a divider,
        *pack_buttons* (⚡) and the validator buttons."""
        self.action_bar = ActionBar(file_buttons, pack_buttons)
        self._build_validator_buttons(self.action_bar.file_layout)
        self._build_llm_buttons()
        return self.action_bar

    def _build_llm_buttons(self):
        """Build the assistant row's LLM task buttons from TaskConfigManager.

        Reads enabled tasks for this tab and creates buttons accordingly.
        Known tasks (in _get_task_callback_map) get specific callbacks;
        unknown/custom tasks get a generic handler; a task with a force
        variant (_get_force_callback_map) gets it in its button's ▾ menu.

        AD_HOC_CHAT tasks are filtered out (handled by the chat panel).
        """
        manager = self.task_config_manager
        tab_id = getattr(self, 'tab_id', None)
        if manager is None or tab_id is None:
            self.action_bar.set_assistant_buttons([])
            return

        enabled_tasks = manager.get_enabled_tasks_for_tab(tab_id)
        callback_map = self._get_task_callback_map()
        force_map = self._get_force_callback_map()

        # Filter out AD_HOC_CHAT (handled by chat panel, not a button)
        button_tasks = [t for t in enabled_tasks if t.id != LLMTask.AD_HOC_CHAT.value]
        if not button_tasks:
            self.action_bar.set_assistant_buttons([ui_kit.note("No LLM tasks configured", wrap=False)])
            return

        buttons = []
        for task in button_tasks:
            # Determine callback
            if task.id in callback_map:
                callback, tooltip = callback_map[task.id]
            else:
                callback = self._make_generic_task_callback(task.id)
                tooltip = f"Run custom task: {task.name}"

            # Capability gate (task #22): a task is invocable only when the
            # effective bundle/project config declares a non-empty
            # prompt_template. Undeclared -> greyed button with tooltip
            # (discoverable), mirroring the pack-gated parser buttons.
            invocable = manager.is_task_invocable(tab_id, task.id)
            if not invocable:
                tooltip = "Not provided by this bundle"

            force_callback, force_tooltip = force_map.get(task.id, (None, ""))
            # Create button with task_id_override for correct label lookup
            buttons.append(self.create_task_button(
                self._resolve_llm_task(task.id),
                callback,
                enabled=invocable,
                tooltip=tooltip,
                task_id_override=task.id,
                force_callback=force_callback,
                force_tooltip=force_tooltip,
            ))
        self.action_bar.set_assistant_buttons(buttons)

    def rebuild_llm_buttons(self):
        """Rebuild LLM buttons after settings change.

        Called by MainWindow.refresh_all_button_labels() when the user
        saves changes in the Settings dialog. A view without an action bar
        (Traceability) has none.
        """
        if not hasattr(self, "action_bar"):
            return
        self._build_llm_buttons()
        log.debug(f"Rebuilt LLM buttons for {self.__class__.__name__}")

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
            # Defense in depth (task #22): the button is greyed when the
            # task is undeclared, but re-check so a stale or programmatic
            # click can never run with a substituted chat prompt.
            manager = self.task_config_manager
            tab_id = getattr(self, 'tab_id', None)
            if (manager is None or tab_id is None
                    or not manager.is_task_invocable(tab_id, task_id)):
                log.error(
                    f"Refusing custom task '{task_id}': not declared by the "
                    f"active bundle (no prompt_template)"
                )
                return
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
