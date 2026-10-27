"""A code test open in Studio, a file of shared phases open in Studio, and the header's Text |
Code switch (design openhtf_results_design.md §2 points 3-4, slices 13-14; boards StudioCode,
StudioCompare, StudioPanel, StudioLeftTabs).

Opening a code test, or a file of ``functions/`` (a shared phase's "✎ Edit", a shared phase
clicked in the Phases tab, "+ New shared phase…"), shows the code view in the middle
(``tabs.code_test_view``), its phases in the right panel's Phases tab and — for a file of shared
phases — the left column's Shared phases list; the text views, their tools and the Assistant
(which works on them) stay closed. The switch writes the test's ``test.json`` and opens the test
again in the version it now runs; nothing is converted, the other version is kept as it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QDialog, QMessageBox

from procedo.core import starter_test, test_format

from .. import shell
from ..core.code_file import CodeFile
from ..dialogs.new_phase_dialog import NewPhaseDialog
from ..tabs.workspace_tab import PHASES_LIST

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace


class CodeTestController:
    """Opens and closes the code view; switches a test's active version; edits and adds shared
    phases."""

    def __init__(self, window: StudioWorkspace) -> None:
        self._mw = window

    def open(self, path: Path) -> None:
        """Show the code test in *path* (the text side already closed)."""
        mw = self._mw
        problem = test_format.read_format(path)[1]
        if problem:  # its test.json cannot be read: taken as a code test, the switch rewrites it
            QMessageBox.warning(mw, "Open Test", problem)
        if not mw.code_view.open(path):
            return
        self._shown(path, None)
        shell.show_versions(mw, path, test_format.CODE)

    def open_phase_file(
        self, path: Path, line: int = 0, phase: tuple[str, str] | None = None
    ) -> bool:
        """Show *path*, a file of ``functions/``, in the code view at *line* (*phase*: the one
        being edited, its file and function) — the open test or file left first (its unsaved
        changes asked about); False when it did not open."""
        mw = self._mw
        doc = mw._document_controller
        if mw.code_view.phase_file == path:
            mw.workspace_widget.shared_phases.mark(phase)
        else:
            if not doc.may_leave_test():
                return False
            doc.leave_open_test()
            doc.close_text_test()
            if not mw.code_view.open_file(path):
                return False
            self._shown(None, phase)
            shell.show_versions(mw, None, "")
        mw.workspace_widget.show_list(PHASES_LIST)
        if line:
            mw.code_view.editor.jump_to_line(line)
        return True

    def _shown(self, test: Path | None, phase: tuple[str, str] | None) -> None:
        """The code view shows a test (*test*) or a file of shared phases (*phase*: the one
        being edited, tinted in the list)."""
        mw = self._mw
        shell.show_code_view(mw, True)  # the Assistant column says it works on text tests
        mw.workspace_widget.set_opened_test(test)
        mw.workspace_widget.shared_phases.mark(phase)
        mw.workspace_widget.shared_phases.load()

    def edit_shared_phase(self, phase: dict) -> None:
        """A shared phase's "✎ Edit" (or a click on its row): its file in the code view, at it."""
        path = Path(self._mw.project_manager.project_root) / phase["file"]
        self.open_phase_file(path, phase["line"], (phase["file"], phase["function"]))

    def new_shared_phase(self) -> None:
        """The Shared phases' "+ New shared phase…": its name, function and file asked; the
        starter phase appended to that file in the code view (a new file starts with the usual
        imports), the cursor in its body. Nothing is written until Save: it shows Modified."""
        mw = self._mw
        functions = Path(mw.project_manager.project_root) / "functions"
        view = mw.code_view
        files, read_only = {}, set()  # each file's text; those Studio cannot write back as they are
        for path in sorted(functions.glob("*.py")):
            try:
                code_file = CodeFile(path)
            except (OSError, UnicodeDecodeError):
                read_only.add(path.name)
                files[path.name] = ""
                continue
            files[path.name] = code_file.text
            if code_file.encode(code_file.text) != code_file.disk:
                read_only.add(path.name)
        current = view.phase_file.name if view.shared_file else ""
        if current:  # what the editor holds, a new file not saved yet too
            files[current] = view.text()
            read_only.discard(current)
            if view.read_only:
                read_only.add(current)
        dialog = NewPhaseDialog(files, current, read_only, mw)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path = functions / dialog.file  # a new one is not on disk: it opens empty
        if not self.open_phase_file(path, 0, (f"functions/{path.name}", dialog.function)):
            return
        if view.read_only:  # said in its pill; the dialog refused the ones it knew of
            return
        text = view.text()
        head = "" if text else starter_test.PHASE_FILE_HEAD
        if text and (missing := starter_test.missing_import(text)):
            view.insert_line(*missing)
            text = view.text()
        view.append(
            head
            + ("" if (text or head).endswith("\n") else "\n")
            + starter_test.starter_phase(dialog.name, dialog.function)
        )

    def close(self) -> None:
        """Nothing open in the code view any more: the text views come back (empty until a test
        opens)."""
        mw = self._mw
        if not mw.code_view.is_open:
            return
        mw.code_view.close()
        mw.workspace_widget.shared_phases.mark(None)
        shell.show_code_view(mw, False)
        shell.show_versions(mw, None, "")

    def on_version_clicked(self, active: str) -> None:
        """The header's switch: the open test runs its *active* version from now on — its
        unsaved changes saved or discarded first, then it opens again in that version."""
        mw = self._mw
        doc = mw._document_controller
        path = doc.open_test_dir()
        if path is None or test_format.active_format(path) == active:
            return
        if not doc.may_leave_test():
            shell.show_versions(mw, path, test_format.active_format(path))  # the switch as it was
            return
        try:
            test_format.write_active(path, active)
        except OSError as exc:
            QMessageBox.critical(mw, "Switch Version", f"test.json could not be written: {exc}")
            shell.show_versions(mw, path, test_format.active_format(path))
            return
        mw.workspace_widget.refresh_opened_test()  # its TXT / HTF badge
        doc.open_now(path)
        version = "code" if active == test_format.CODE else "text"
        mw.footer.show_message(f"{path.name} now runs its {version} version", 3000)
