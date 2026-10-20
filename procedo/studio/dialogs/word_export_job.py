"""One Word export (File › Export › Word) run off the GUI thread: a window-modal
progress dialog with Cancel, the report rendered on a worker (``procedo.core.off_gui``)
— the GUI thread never asks the project's engine — and its outcome shown when it lands."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from procedo.core.off_gui import answer_to, run_off_gui

from ..core.full_report import export_outcome


class WordExportJob(QObject):
    """Starts at construction; *on_exported(path)* runs on the GUI thread once written."""

    def __init__(self, window: QWidget, project_root: Path, test_folders: list[Path], output_path: Path,
                 sidecar: dict, on_exported: Callable[[Path], None]) -> None:
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

        run_off_gui(lambda: export_outcome(project_root, test_folders, output_path,
                                           sidecar=sidecar, progress=progress),
                    self._on_answer, name="word-export")

    def _on_step(self, step: tuple[int, int]) -> None:
        done, total = step
        self._progress.setLabelText("Generating board images...")
        self._progress.setMaximum(max(total, 1))
        self._progress.setValue(done)

    def _on_answer(self, answer: dict) -> None:
        self._progress.close()
        self._progress.deleteLater()
        if "path" in answer:
            self._on_exported(answer["path"])
        else:
            show = QMessageBox.critical if answer.get("critical", True) else QMessageBox.warning
            show(self._window, answer.get("title", "Export Failed"), answer.get("text") or str(answer.get("error")))
        self.deleteLater()
