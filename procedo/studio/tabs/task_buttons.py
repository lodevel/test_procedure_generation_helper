"""A view's LLM task buttons: built from TaskConfigManager, labelled from it, relabelled and
rebuilt when the settings change. Mixed into ``BaseTab``."""

import logging
from typing import Callable, Optional

from PySide6.QtWidgets import QToolButton

from procedo.core import ui_kit

from ..llm.backend_base import LLMTask
from ..widgets.action_bar import set_task_label, task_button

log = logging.getLogger(__name__)


class TaskButtonsMixin:
    """The LLM task buttons of a ``BaseTab``. Subclasses override
    ``_get_task_callback_map()`` and ``_get_force_callback_map()``."""

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
