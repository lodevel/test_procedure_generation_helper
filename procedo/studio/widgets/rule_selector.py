"""
Rule selector — which rule documents a view sends in its LLM context (board S10, Select rules):
the bundle rules, then the project's own documents (``config/project_rules/``), which
"Add document…" copies in and "Open folder" shows.

Persistence flows through ``TaskConfigManager.set_selected_rules_for_tab`` (which
propagates the selection to each task in the tab) on every tick, so the selection
holds even when the dialog is cancelled.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QPushButton

from procedo.ui import ui_kit

from ..core.project_manager import RuleDocError, kilobytes, rule_doc_detail, warn_above

if TYPE_CHECKING:
    from ..core import ProjectManager
    from ..core.task_config import TaskConfigManager


def current_selection(
    task_config_manager: "TaskConfigManager", tab_id: str, available_keys: list[str]
) -> list[str]:
    """Resolve the tab's current ``selected_rules`` to a concrete list of rule document
    keys, expanding the ``"all"`` sentinel against every available document (the first
    task's: selection is uniform across a tab's tasks)."""
    tasks = task_config_manager.get_all_tasks_for_tab(tab_id)
    raw = tasks[0].selected_rules if tasks else None
    if isinstance(raw, list):
        return list(raw)
    return list(available_keys)


class RuleSelectorDialog(QDialog):
    """One row per rule document (ticked: in the view's context) with its size, the bundle
    rules then a Project section; Select all / Clear all, Add document… / Open folder, the
    count and the selected size in the footer; :meth:`get_selected_rules` when accepted."""

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
        self.project_manager = project_manager
        self.task_config_manager = task_config_manager

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = ui_kit.primary(QPushButton("OK"))
        ok_btn.clicked.connect(self.accept)
        add_btn = QPushButton("Add document…")
        add_btn.setToolTip("Copy a .md, .txt or .pdf document into the project's rules "
                           "(config/project_rules), ticked in every view")
        add_btn.clicked.connect(self._on_add_document)
        folder_btn = QPushButton("Open folder")
        folder_btn.setToolTip("Open the project's rules folder, to edit or delete its documents")
        folder_btn.clicked.connect(self._on_open_folder)
        has_project = project_manager.project_root is not None
        add_btn.setEnabled(has_project)
        folder_btn.setEnabled(has_project)
        footer = ui_kit.FooterBand([cancel_btn, ok_btn], left=[add_btn, folder_btn])
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
        self._fill()
        self.rules.toggled.connect(self._on_selection_changed)

    def _fill(self) -> None:
        """(Re)build the rows from the documents on disk and the saved selection."""
        self._sizes: dict[str, int] = {}
        docs = []
        for doc in self.project_manager.get_rule_docs():
            try:
                self._sizes[doc.key] = doc.path.stat().st_size
            except OSError:  # gone since the scan
                continue
            docs.append(doc)
        chosen = set(current_selection(self.task_config_manager, self.tab_id, list(self._sizes)))
        self.rules.clear()
        bundle = [doc for doc in docs if doc.origin == "bundle"]
        own = [doc for doc in docs if doc.origin == "project"]
        for doc in bundle:
            self.rules.add(doc.key, doc.key, rule_doc_detail(doc, self._sizes[doc.key]),
                           checked=doc.key in chosen)
        if not bundle:
            self.rules.add_message("No bundle rules")
        self.rules.add_message("Project")
        for doc in own:
            self.rules.add(doc.key, doc.path.name, rule_doc_detail(doc, self._sizes[doc.key]),
                           checked=doc.key in chosen)
        if self.project_manager.project_root is None:
            self.rules.add_message("Open a project to add documents")
        elif not own:
            self.rules.add_message("No project documents: Add document… copies one in")
        self._update_count()

    def _on_selection_changed(self) -> None:
        """A tick: saved at once, through the task-config single-writer contract (it
        propagates to every task in the tab)."""
        self.task_config_manager.set_selected_rules_for_tab(self.tab_id, self.get_selected_rules())
        self._update_count()
        self._save()

    def _save(self) -> None:
        """Save the selection; when it could not be (a share down), the footer says why."""
        if not self.task_config_manager.save_config():
            self.count_label.setText(f"Not saved: {self.task_config_manager.save_error}")

    def _update_count(self) -> None:
        checked = self.rules.checked()
        size = sum(self._sizes[key] for key in checked)
        self.count_label.setText(f"{len(checked)} of {len(self.rules.keys())} rules selected"
                                 + (f" · {kilobytes(size)}" if checked else ""))

    def _on_add_document(self) -> None:
        """Copy a document the user picks into the project's rules, then tick it in every
        view's selection (owner decision 2026-10-19); refused when it cannot be added,
        a warning when it is large."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a project rule document", "", "Rule documents (*.md *.txt *.pdf)")
        if not path:
            return
        try:
            doc = self.project_manager.add_project_rule_doc(Path(path))
        except (RuleDocError, OSError) as exc:
            QMessageBox.warning(self, "Add document", str(exc))
            return
        self.task_config_manager.add_rule_to_selections(doc.key)
        self._fill()
        self._save()
        size, limit = doc.path.stat().st_size, warn_above(doc.path)
        if size > limit:
            QMessageBox.warning(
                self, "Add document",
                f"{doc.path.name} is {kilobytes(size)}: every selected document is sent with "
                f"each request, keep each under {kilobytes(limit)}.")

    def _on_open_folder(self) -> None:
        """Show the project's rules folder (created when missing) in the file manager."""
        folder = self.project_manager.project_rules_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Open folder", f"Cannot create {folder}: {exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def get_selected_rules(self) -> list[str]:
        """The ticked rule document keys (e.g. ['rule1.md', 'project/board.md'])."""
        return self.rules.checked()
