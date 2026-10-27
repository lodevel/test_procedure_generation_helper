"""A code test open in Studio, and the header's Text | Code switch (design
openhtf_results_design.md §2 points 3-4, slices 13-14; boards StudioCode and StudioCompare).

Opening a code test shows the code view in the middle (``tabs.code_test_view``) and the shared
phases in the Workspace column; the text views, their tools and the Assistant (which works on
them, its column says so) stay closed. The switch writes the test's ``test.json`` and opens the test again in the
version it now runs; nothing is converted, the other version is kept as it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from procedo.core import test_format

from .. import shell

if TYPE_CHECKING:
    from pathlib import Path

    from ..main_window import StudioWorkspace


class CodeTestController:
    """Opens and closes the code view; switches a test's active version."""

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
        shell.show_code_view(mw, True)  # the Assistant column says it works on text tests
        mw.workspace_widget.set_opened_test(path)
        mw.workspace_widget.shared_phases.load()
        shell.show_versions(mw, path, test_format.CODE)

    def close(self) -> None:
        """No code test open any more: the text views come back (empty until a test opens)."""
        mw = self._mw
        if mw.code_view.test_dir is None:
            return
        mw.code_view.close()
        mw.workspace_widget.shared_phases.clear()
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
