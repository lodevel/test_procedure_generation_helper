"""
Rule selector — which rule documents a view sends in its LLM context (board S10, Select rules).

Persistence flows through ``TaskConfigManager.set_selected_rules_for_tab`` (which
propagates the selection to each task in the tab) on every tick, so the selection
holds even when the dialog is cancelled.
"""

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QDialog, QPushButton

from project_services import ui_kit

if TYPE_CHECKING:
    from ..core import ProjectManager
    from ..core.task_config import TaskConfigManager


def current_selection(
    task_config_manager: "TaskConfigManager", tab_id: str, available_filenames: list[str]
) -> list[str]:
    """Resolve the tab's current ``selected_rules`` to a concrete list of
    filenames, expanding the ``"all"`` sentinel against the project's
    available rule files (the first task's: selection is uniform across a tab's tasks)."""
    tasks = task_config_manager.get_all_tasks_for_tab(tab_id)
    raw = tasks[0].selected_rules if tasks else None
    if isinstance(raw, list):
        return list(raw)
    return list(available_filenames)


class RuleSelectorDialog(QDialog):
    """One row per rule file (ticked: in the view's context), Select all / Clear all, the count
    in the footer; :meth:`get_selected_rules` when accepted."""

    def __init__(
        self,
        tab_id: str,
        project_manager: "ProjectManager",
        task_config_manager: "TaskConfigManager",
        parent=None,
        view: str = "",
    ):
        """
        Args:
            tab_id: Tab identifier ("text_json", "json_code")
            project_manager: ProjectManager for enumerating available rules
            task_config_manager: TaskConfigManager that owns the selection
                under ``workflows.<tab>.tasks[*].selected_rules``
            parent: Parent widget
            view: The view's name (``shell.VIEWS``), the window's subtitle
        """
        super().__init__(parent)
        self.setWindowTitle("Select rules")
        self.resize(640, 560)
        self.tab_id = tab_id
        self.task_config_manager = task_config_manager

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = ui_kit.primary(QPushButton("OK"))
        ok_btn.clicked.connect(self.accept)
        footer = ui_kit.FooterBand([cancel_btn, ok_btn])
        self.count_label = footer.note
        header = ui_kit.HeaderBand("Select rules", f"{view} view" if view else "", stacked=True)
        body = ui_kit.framed(self, header, footer)

        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: self.rules.set_all(True))
        clear_all_btn = QPushButton("Clear all")
        clear_all_btn.clicked.connect(lambda: self.rules.set_all(False))
        body.addLayout(ui_kit.hbox(ui_kit.note("Rules included in the LLM context for this view.",
                                               wrap=False), None, select_all_btn, clear_all_btn))
        self.rules = ui_kit.CheckList(mono=True)
        body.addWidget(self.rules, 1)

        available = [rule.name for rule in project_manager.get_rules_files()]
        chosen = set(current_selection(task_config_manager, tab_id, available))
        for filename in available:
            self.rules.add(filename, filename, checked=filename in chosen)
        if not available:
            self.rules.add_message("No rules available")
        self.rules.toggled.connect(self._on_selection_changed)
        self._update_count()

    def _on_selection_changed(self) -> None:
        """A tick: saved at once, through the task-config single-writer contract (it
        propagates to every task in the tab)."""
        self.task_config_manager.set_selected_rules_for_tab(self.tab_id, self.get_selected_rules())
        self.task_config_manager.save_config()
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(
            f"{len(self.rules.checked())} of {len(self.rules.keys())} rules selected")

    def get_selected_rules(self) -> list[str]:
        """The ticked rule filenames (e.g. ['rule1.md', 'rule2.md'])."""
        return self.rules.checked()
