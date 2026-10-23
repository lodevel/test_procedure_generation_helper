"""One Word export (File › Export › Word) run off the GUI thread: a window-modal
progress dialog with Cancel, the report rendered on a worker (``procedo.core.off_gui``)
— the GUI thread never asks the project's engine — and its outcome shown when it lands.
A report saved into the project's evidence exports folder goes through the evidence
store (:mod:`procedo.core.project_exports`); any other folder is a plain file."""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from procedo.core import project_exports, report_export
from procedo.core.evidence_store import same_folder
from procedo.core.off_gui import answer_to, run_off_gui

from ..core.full_report import export_outcome


def _export_as_evidence(project_root: Path, test_folders: list[Path], output_path: Path,
                        sidecar: dict, progress) -> dict:
    """:func:`export_outcome` into a temporary folder (removed on every path), then the
    document and its sidecar handed to the project's evidence store (a refusal there
    is the worker's error answer). ``queued``: not pushed yet — it waits in this PC's
    outbox."""
    with tempfile.TemporaryDirectory(prefix="procedo-word-export-") as tmp:
        answer = export_outcome(project_root, test_folders, Path(tmp) / output_path.name,
                                sidecar=sidecar, progress=progress)
        if "path" not in answer:
            return answer
        pushed = project_exports.save_export(project_root, output_path.name, answer["path"].read_bytes(),
                                             report_export.sidecar_bytes(sidecar))
    return {"path": output_path, "queued": not pushed}


class WordExportJob(QObject):
    """Starts at construction; *on_exported(path, queued)* runs on the GUI thread once
    written (``queued``: waiting in this PC's outbox). *evidence_folder*: the project's
    evidence exports folder — an *output_path* inside it goes through the store."""

    def __init__(self, window: QWidget, project_root: Path, test_folders: list[Path], output_path: Path,
                 sidecar: dict, on_exported: Callable[[Path, bool], None],
                 evidence_folder: Path | None = None) -> None:
        super().__init__(window)
        self._window, self._on_exported = window, on_exported
        self._progress = QProgressDialog("Exporting the Word report...", "Cancel", 0, 0, window)
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        cancelled = threading.Event()
        self._progress.canceled.connect(cancelled.set)
        step = answer_to(self._on_step)

        def progress(done: int, total: int) -> bool:  # on the worker, per board image
            step((done, total))
            return not cancelled.is_set()

        def export() -> dict:  # on the worker: the folder rule touches the filesystem
            if evidence_folder is not None and same_folder(output_path.parent, evidence_folder):
                return _export_as_evidence(project_root, test_folders, output_path, sidecar, progress)
            return export_outcome(project_root, test_folders, output_path, sidecar=sidecar, progress=progress)

        run_off_gui(export, self._on_answer, name="word-export")

    def _on_step(self, step: tuple[int, int]) -> None:
        done, total = step
        self._progress.setLabelText("Generating board images...")
        self._progress.setMaximum(max(total, 1))
        self._progress.setValue(done)

    def _on_answer(self, answer: dict) -> None:
        self._progress.close()
        self._progress.deleteLater()
        if "path" in answer:
            self._on_exported(answer["path"], answer.get("queued", False))
        else:
            show = QMessageBox.critical if answer.get("critical", True) else QMessageBox.warning
            show(self._window, answer.get("title", "Export Failed"), answer.get("text") or str(answer.get("error")))
        self.deleteLater()
