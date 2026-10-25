"""File -> Export (Markdown / Word) of the Studio workspace, carved out of
``main_window.StudioWorkspace`` (bodies moved verbatim: ``self`` is this
controller, the window's state is read through ``self._mw``).

With a project open the SELECTED tests are exported (Markdown: their text
concatenated; Word: the full report, rendered on a worker); without one, a
quick dump of the open procedure's live editor text.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from procedo.core.report_export import create_default_sidecar
from procedo.ui.word_export_dialog import WordExportDialog

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

log = logging.getLogger(__name__)


class ExportController:
    """The workspace's Markdown and Word exports."""

    def __init__(self, window: StudioWorkspace) -> None:
        self._mw = window

    def _current_procedure_text(self):
        """Return the live procedure text from the active editor.

        Reads ``text_editor.toPlainText()`` directly (never the artifact
        cache) so unsaved edits are exported. Tries the active tab first,
        then the text/json tab which always owns procedure_text.
        """
        candidates = [
            self._mw.view_stack.currentWidget(),
            getattr(self._mw, "text_json_tab", None),
            getattr(self._mw, "text_only_tab", None),
        ]
        for tab in candidates:
            editor = getattr(tab, "text_editor", None)
            if editor is not None:
                txt = editor.toPlainText()
                if txt and txt.strip():
                    return txt
        return None

    def _export_default_path(self, suffix: str) -> Path:
        """Default save location/name for an export with *suffix* (e.g. '.md')."""
        root = getattr(self._mw.project_manager, "project_root", None)
        if root is not None:
            stem = Path(root).name or "procedure"
            return Path(root) / f"{stem}{suffix}"
        return Path.home() / f"procedure{suffix}"

    def on_export_markdown(self):
        """File -> Export -> Markdown: the SELECTED tests' text concatenated, or a
        quick single-procedure dump when no project is open."""
        root = getattr(self._mw.project_manager, "project_root", None)
        if root is None:
            self._export_markdown_single()
            return
        folders = self._mw.project_manager.enumerate_test_folders()
        if not folders:
            QMessageBox.information(
                self._mw, "Export to Markdown", "This project has no test folders."
            )
            return
        from ..dialogs.test_selection_dialog import TestSelectionDialog

        sel = TestSelectionDialog(folders, require="text", parent=self._mw)
        if sel.exec() != QDialog.Accepted:
            return
        selected = sel.selected_folders()
        if not selected:
            QMessageBox.information(self._mw, "Export to Markdown", "No tests selected.")
            return
        default = self._export_default_path(".md")
        path, _ = QFileDialog.getSaveFileName(
            self._mw, "Export Tests to Markdown", str(default), "Markdown (*.md);;All Files (*)"
        )
        if not path:
            return
        try:
            parts = []
            for f in selected:
                txt = (f.path / "procedure_text.md").read_text(encoding="utf-8")
                parts.append(f"# {f.name}\n\n{txt.strip()}\n")
            Path(path).write_text("\n\n".join(parts), encoding="utf-8")
        except Exception as e:
            log.exception("Markdown export failed")
            QMessageBox.critical(self._mw, "Export Failed", f"Could not write Markdown:\n{e}")
            return
        self._mw.footer.show_message(
            f"Exported {len(selected)} test(s) to Markdown -> {path}", 5000
        )

    def _export_markdown_single(self):
        """Quick dump of the open procedure's text (used when no project is open)."""
        text = self._current_procedure_text()
        if not text:
            QMessageBox.information(
                self._mw,
                "Export to Markdown",
                "There is no procedure text to export.\nOpen or author a procedure first.",
            )
            return
        default = self._export_default_path(".md")
        path, _ = QFileDialog.getSaveFileName(
            self._mw, "Export Procedure to Markdown", str(default), "Markdown (*.md);;All Files (*)"
        )
        if not path:
            return
        try:
            from ..core.procedure_export import write_markdown

            write_markdown(text, Path(path))
        except Exception as e:
            log.exception("Markdown export failed")
            QMessageBox.critical(self._mw, "Export Failed", f"Could not write Markdown:\n{e}")
            return
        self._mw.footer.show_message(f"Exported Markdown -> {path}", 5000)

    def on_export_word(self):
        """File -> Export -> Word: the FULL report (.docx) over the SELECTED tests
        (same engine as the main app), or a quick single-procedure dump when no
        project is open."""
        root = getattr(self._mw.project_manager, "project_root", None)
        if root is None:
            self._export_word_single()
            return
        folders = self._mw.project_manager.enumerate_test_folders()
        if not folders:
            QMessageBox.information(self._mw, "Export to Word", "This project has no test folders.")
            return
        from ..dialogs.test_selection_dialog import TestSelectionDialog

        sel = TestSelectionDialog(folders, require="json", parent=self._mw)
        if sel.exec() != QDialog.Accepted:
            return
        selected = sel.selected_folders()
        if not selected:
            QMessageBox.information(self._mw, "Export to Word", "No tests selected.")
            return
        from ..core.full_report import has_active_bundle

        if not has_active_bundle(root):
            if (
                QMessageBox.question(
                    self._mw,
                    "No active bundle",
                    "This project has no active bundle, so the report will contain "
                    "metadata only — no test steps or expected results.\n\n"
                    "Export anyway?",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return

        from procedo.core import project_exports
        from procedo.core.evidence_store import EvidenceError

        # The default folder is the project's evidence exports folder: a report kept
        # there goes through the evidence store; any other folder is a plain file.
        try:
            export_folder = evidence_folder = project_exports.exports_folder(Path(root))
        except EvidenceError as exc:  # no default inside the project: the user picks one
            evidence_folder = None
            QMessageBox.warning(
                self._mw,
                "Export to Word",
                f"The project's evidence folder cannot be opened:\n{exc}\n\n"
                "Choose another folder for the report.",
            )
            chosen = QFileDialog.getExistingDirectory(self._mw, "Export to Word", str(Path.home()))
            if not chosen:
                return
            export_folder = Path(chosen)
        sidecar = create_default_sidecar()
        dlg = WordExportDialog(
            sidecar,
            Path(root),
            export_folder,
            default_docx_name="test_report.docx",
            parent=self._mw,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        output_path = dlg.get_output_path()
        sidecar = dlg.get_sidecar()
        from ..dialogs.word_export_job import WordExportJob

        def exported(path: Path, queued: bool) -> None:
            later = " (waiting in this PC's outbox: Bench uploads it once the evidence folder is reachable)"
            self._mw.footer.show_message(
                f"Exported {len(selected)}-test report -> {path}" + (later if queued else ""), 5000
            )

        self._word_export = WordExportJob(  # rendered on a worker: the GUI never asks the engine
            self._mw,
            Path(root),
            [f.path for f in selected],
            output_path,
            sidecar,
            on_exported=exported,
            evidence_folder=evidence_folder,
        )

    def _export_word_single(self):
        """Quick dump of the open procedure as a plain .docx (used when no project)."""
        text = self._current_procedure_text()
        if not text:
            QMessageBox.information(
                self._mw,
                "Export to Word",
                "There is no procedure text to export.\nOpen or author a procedure first.",
            )
            return
        default = self._export_default_path(".docx")
        path, _ = QFileDialog.getSaveFileName(
            self._mw,
            "Export Procedure to Word",
            str(default),
            "Word Document (*.docx);;All Files (*)",
        )
        if not path:
            return
        from ..core.procedure_export import write_word, WordExportUnavailable

        try:
            write_word(text, Path(path), title=None)
        except WordExportUnavailable as e:
            QMessageBox.warning(self._mw, "Word Export Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Word export failed")
            QMessageBox.critical(self._mw, "Export Failed", f"Could not write Word document:\n{e}")
            return
        self._mw.footer.show_message(f"Exported Word -> {path}", 5000)
