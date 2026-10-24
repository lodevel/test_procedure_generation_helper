"""A view's LLM task buttons: built from TaskConfigManager, labelled from it, rebuilt when
the settings change. Held by ``BaseTab`` as ``task_buttons``."""

import logging
from typing import TYPE_CHECKING, Callable, Optional

from PySide6.QtWidgets import QToolButton

from procedo.ui import ui_kit

from ..llm.backend_base import LLMTask
from ..widgets.action_bar import ActionBar, task_button

if TYPE_CHECKING:
    from ..core.task_config import TaskConfigManager

log = logging.getLogger(__name__)


class TaskButtonRow:
    """The assistant row of a view's action bar: one button per task the view's tab enables."""

    def __init__(
        self,
        action_bar: ActionBar,
        *,
        tab_id: Optional[str],
        owner_name: str,
        config: Optional["TaskConfigManager"],
        callbacks: dict,
        force_callbacks: dict,
        run_task: Optional[Callable],
    ) -> None:
        """*tab_id* names the tab's tasks (None: no buttons); *owner_name* is the view's class
        name, for the logs and the label lookup of a tab without an id. *callbacks* and
        *force_callbacks* map a task id to its (callback, tooltip) and its force variant's;
        *run_task* runs a custom task (``_run_task_async``), None in a view without LLM."""
        self._action_bar = action_bar
        self._tab_id = tab_id
        self._owner_name = owner_name
        self._tab_name = tab_id if tab_id is not None else owner_name.replace("Tab", "").lower()
        self._config = config
        self._callbacks = callbacks
        self._force_callbacks = force_callbacks
        self._run_task = run_task

    def create_button(
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

        With *force_callback* it is a split button: its ▾ menu offers the task
        and its force variant (validation bypassed).

        Args:
            task: The LLMTask this button triggers
            callback: Function to call when button is clicked
            enabled: Initial enabled state
            tooltip: Optional tooltip text (auto-generated if empty)
            tab_name: Optional tab name for label lookup (the row's tab if None)
            task_id_override: Task id for the label lookup instead of task.value
            force_callback: The force variant's callback (a split button)
            force_tooltip: The force variant's tooltip
        """
        if tab_name is None:
            tab_name = self._tab_name

        # Get label from TaskConfigManager
        label = self._label(task, tab_name, task_id_override=task_id_override)
        button = task_button(label, callback, force_callback=force_callback,
                             force_tooltip=force_tooltip)
        button.setEnabled(enabled)
        button.setToolTip(tooltip or f"{label}\nTask: {task.value}")
        return button

    def rebuild(self):
        """Build the assistant row's LLM task buttons from TaskConfigManager.

        Reads enabled tasks for this tab and creates buttons accordingly.
        Known tasks (in the callbacks) get specific callbacks;
        unknown/custom tasks get a generic handler; a task with a force
        variant (the force callbacks) gets it in its button's ▾ menu.
        Called again by MainWindow.refresh_all_button_labels() when the
        user saves changes in the Settings dialog.

        AD_HOC_CHAT tasks are filtered out (handled by the chat panel).
        """
        manager = self._config
        tab_id = self._tab_id
        if manager is None or tab_id is None:
            self._action_bar.set_assistant_buttons([])
            return

        enabled_tasks = manager.get_enabled_tasks_for_tab(tab_id)
        callback_map = self._callbacks
        force_map = self._force_callbacks

        # Filter out AD_HOC_CHAT (handled by chat panel, not a button)
        button_tasks = [t for t in enabled_tasks if t.id != LLMTask.AD_HOC_CHAT.value]
        if not button_tasks:
            self._action_bar.set_assistant_buttons(
                [ui_kit.note("No LLM tasks configured", wrap=False)])
            return

        buttons = []
        for task in button_tasks:
            # Determine callback
            if task.id in callback_map:
                callback, tooltip = callback_map[task.id]
            else:
                callback = self._generic_callback(task.id)
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
            buttons.append(self.create_button(
                self._resolve_llm_task(task.id),
                callback,
                enabled=invocable,
                tooltip=tooltip,
                task_id_override=task.id,
                force_callback=force_callback,
                force_tooltip=force_tooltip,
            ))
        self._action_bar.set_assistant_buttons(buttons)

    @staticmethod
    def _resolve_llm_task(task_id: str) -> LLMTask:
        """Resolve a task_id string to an LLMTask enum, with fallback."""
        try:
            return LLMTask(task_id)
        except ValueError:
            return LLMTask.AD_HOC_CHAT  # fallback for custom tasks

    def _generic_callback(self, task_id: str):
        """Create a generic callback for custom/unknown tasks.

        Custom tasks use AD_HOC_CHAT as the LLM routing task but pass their
        custom_task_id so the correct prompt_template is looked up.
        """
        def _callback():
            # Defense in depth (task #22): the button is greyed when the
            # task is undeclared, but re-check so a stale or programmatic
            # click can never run with a substituted chat prompt.
            manager = self._config
            tab_id = self._tab_id
            if (manager is None or tab_id is None
                    or not manager.is_task_invocable(tab_id, task_id)):
                log.error(
                    f"Refusing custom task '{task_id}': not declared by the "
                    f"active bundle (no prompt_template)"
                )
                return
            log.info(f"Running custom task: {task_id}")
            if self._run_task is not None:
                self._run_task(LLMTask.AD_HOC_CHAT, custom_task_id=task_id)
            else:
                log.warning(f"Tab {self._owner_name} has no _run_task_async method")
        return _callback

    def _label(self, task: LLMTask, tab_name: str, task_id_override: str = None) -> str:
        """
        Get task button label from TaskConfigManager with fallback.

        Args:
            task: The LLMTask to get label for
            tab_name: Tab identifier (e.g., "text_json", "json_code")
            task_id_override: Optional task ID to use for lookup instead of task.value

        Returns:
            Button label string
        """
        manager = self._config

        # Try TaskConfigManager first
        if manager is not None:
            lookup_id = task_id_override if task_id_override else task.value
            task_config = manager.get_task_config(tab_name, lookup_id)
            if task_config is not None:
                return task_config.button_label
            else:
                log.warning(f"Task config not found for {tab_name}.{lookup_id}, using fallback label")

        # Fallback: generate label from task name
        fallback_label = task.value.replace('_', ' ').title()
        log.debug(f"Using fallback label '{fallback_label}' for task {task.value}")
        return fallback_label
