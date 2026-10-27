"""The middle of Procedo Studio for a code test, or for a file of shared phases (boards StudioCode,
StudioPanel A, E): the editor card of ``openhtf/test.py`` or ``functions/<file>.py`` (its Saved /
● Modified pill, "Open in my editor", Save), the find bar (Ctrl+F) over it, and under it the action
bar: Check and its results line. Its phases — the engine's *describe* of the editor's text — are
the right panel's Phases tab (``widgets.phases_panel``, which this view owns): a click there goes
to the line here, or opens the file of a shared phase.

The file is the source of truth (``core.code_file``): it is written back byte for byte as edited
(its BOM and line endings kept) — a file whose bytes the editor cannot give back is shown
read-only —, a change made outside Studio is reloaded when nothing is edited here and asked about
before a save would overwrite it. Every engine call (describe, Check) runs off the GUI thread
behind the shared loading cue.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import shiboken6

from procedo.core import code_test_check, test_format
from procedo.ui import ui_kit
from procedo.ui.flow_layout import FlowLayout

from ..core.code_file import READ_ONLY_TIP, CodeFile
from ..core.code_test_preview import check_lines, equipment, phase_entries
from ..core.open_in_editor import open_in_editor
from ..pack_ops import engine_async
from ..widgets.editor_card import CodeEditor
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts
from ..widgets.phases_panel import PhasesPanel
from ..widgets.python_highlighter import PythonSyntaxHighlighter

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

#: The code version's file in its test folder, as the card names it.
FILE = test_format.CODE_SCRIPT
#: How long the phases wait after the last keystroke before they are read again (ms).
_PHASES_DELAY_MS = 400
#: The question when the file changed outside Studio before a save.
CHANGED_TITLE = "Changed outside Studio"
CHANGED_TEXT = "{file} was changed outside Studio since it was opened."
DELETED_TEXT = "{file} was deleted outside Studio."
RELOAD, OVERWRITE = "reload", "overwrite"
#: The results line once the code was edited after its Check (board StudioPanel B).
NOT_CHECKED = "Not checked since the last edit"


def ask_changed_outside(parent: QWidget, file: str) -> str:
    """:data:`RELOAD`, :data:`OVERWRITE`, or "" (Cancel): what to do with *file*, changed outside
    Studio, before a save."""
    box = QMessageBox(
        QMessageBox.Icon.Warning, CHANGED_TITLE, CHANGED_TEXT.format(file=file), parent=parent
    )
    reload = box.addButton("Reload", QMessageBox.ButtonRole.DestructiveRole)
    overwrite = box.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return {reload: RELOAD, overwrite: OVERWRITE}.get(box.clickedButton(), "")


class CodeTestView(QWidget):
    """The open code test's middle column (``test_dir``), or the open file of shared phases'
    (``phase_file``); neither: nothing is open."""

    #: The text was edited or saved, or the phases were read: the header follows.
    changed = Signal()

    def __init__(self, ws: StudioWorkspace) -> None:
        super().__init__()
        self._ws = ws
        self.test_dir: Path | None = None
        self.phase_file: Path | None = None
        self.name = ""  # the open file as the card names it: openhtf/test.py, functions/x.py
        self.described: dict | None = None
        self.code_file: CodeFile | None = None
        self.read_only = False  # the editor cannot give the file's bytes back
        # The editor's text when the file was read or written (None: the file was deleted).
        self._saved: str | None = ""
        self._asked = 0  # the describe awaited: an older answer is dropped
        self._checked = False  # the results line shows a Check of the text as it is
        self._phases_timer = QTimer(self)  # the phases follow the editor, once typing pauses
        self._phases_timer.setSingleShot(True)
        self._phases_timer.setInterval(_PHASES_DELAY_MS)
        self._phases_timer.timeout.connect(self.read_phases)
        # Back from another app (the user's editor): a file changed there is reloaded.
        QCoreApplication.instance().applicationStateChanged.connect(self._on_app_state)

        self.phases = PhasesPanel()  # the right panel's Phases tab (shell places it)
        self.phases.jump.connect(self._jump)
        self.phases.open_file.connect(self._open_shared)
        self.editor_card = self._editor_card()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.find_bar = FindReplaceBar(self)  # Edit › Find (Ctrl+F) finds in the code
        layout.addWidget(self.find_bar)
        install_find_shortcuts(self, [self.editor], self.find_bar)
        layout.addWidget(self.editor_card, 1)
        layout.addWidget(self._action_bar())

    def _editor_card(self) -> QFrame:
        card = ui_kit.card_frame()
        self.file_label = QLabel()
        self.file_label.setObjectName("cardFileName")
        self.pill = QLabel()
        self.open_button = QPushButton("Open in my editor")
        self.open_button.clicked.connect(self._open_in_editor)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save)
        head = ui_kit.hbox(self.file_label, self.pill, None, self.open_button, self.save_button)
        head.setContentsMargins(12, 8, 12, 8)
        self.editor = CodeEditor(wrap=False)
        self.highlighter = PythonSyntaxHighlighter(self.editor.document())
        self.editor.textChanged.connect(self._on_edited)
        column = QVBoxLayout(card)
        column.setContentsMargins(1, 0, 1, 1)  # inside the card's border
        column.setSpacing(0)
        column.addLayout(head)
        column.addWidget(ui_kit.row_rule())
        column.addWidget(self.editor, 1)
        return card

    def _action_bar(self) -> QFrame:
        bar = ui_kit.card_frame()
        self.check_button = QPushButton("Check")
        ui_kit.set_primary(self.check_button)
        self.check_button.clicked.connect(self.check)
        self.check_cue = ui_kit.LoadingCue()
        results = QWidget()  # the ✓ / ✗ row, then one ⚠ line per warning
        self.results = QVBoxLayout(results)
        self.results.setContentsMargins(0, 0, 0, 0)
        self.results.setSpacing(4)
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        row.addWidget(self.check_button, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.check_cue, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(results, 1)
        return bar

    # ---- the open file -----------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """A code test or a file of shared phases is open."""
        return self.code_file is not None

    @property
    def dirty(self) -> bool:
        """The editor holds changes not written to the open file."""
        return self.is_open and self.text() != self._saved

    @property
    def file(self) -> Path:
        return self.code_file.path

    @property
    def shared_file(self) -> bool:
        """The open file is one of the project's shared phases (directly in ``functions/``)."""
        return self.phase_file is not None and self.phase_file.parent == self._root() / "functions"

    def _root(self) -> Path:
        return Path(self._ws.project_manager.project_root)

    def text(self) -> str:
        """The editor's text exactly (a non-breaking space stays one), its line breaks "\n"."""
        return self.editor.document().toRawText().replace(" ", "\n")

    def open(self, test_dir: Path) -> bool:
        """Show the code test in *test_dir* (its file read, its phases asked); False when its
        file cannot be read (said to the user)."""
        try:
            code_file = CodeFile(test_format.script_of(test_dir, test_format.CODE))
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(
                self, "Open Test", f"{FILE} of {test_dir.name} cannot be read: {exc}"
            )
            return False
        self.test_dir, self.phase_file = test_dir, None
        self._show_file(code_file, FILE)
        return True

    def open_file(self, path: Path) -> bool:
        """Show *path*, a file of phases in the project (``functions/``, a test's own folder), as
        a test is shown, named by its path in the project; False when it cannot be read (said to
        the user)."""
        name = path.relative_to(self._root()).as_posix()
        try:
            code_file = CodeFile(path)
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Open File", f"{name} cannot be read: {exc}")
            return False
        self.test_dir, self.phase_file = None, path
        self._show_file(code_file, name)
        return True

    def _show_file(self, code_file: CodeFile, name: str) -> None:
        self.name = name
        self.file_label.setText(name)
        self.open_button.setToolTip(f"Open {name} in this PC's editor for Python files")
        self.save_button.setToolTip(f"Save {name} to disk")
        self.check_button.setToolTip(
            f"Check {name}: its syntax, its load in the project's environment, what it holds"
            if self.test_dir is not None
            else f"Check {name}: its syntax and what it holds"
        )
        self._load(code_file)
        self._clear_results()
        self._show_described(None, "")
        self.check_button.setEnabled(not engine_async.busy(self.check_button))
        self.read_phases()

    def _load(self, code_file: CodeFile) -> None:
        """Show *code_file*: read-only when the editor cannot give its bytes back."""
        self.code_file = code_file
        self.editor.blockSignals(True)
        self.editor.setPlainText(code_file.text)
        self.editor.blockSignals(False)
        self._saved = self.text()
        self.read_only = code_file.encode(self._saved) != code_file.disk
        self.editor.setReadOnly(self.read_only)
        self._show_state()

    def close(self) -> None:
        """Nothing open: the editor, the phases and the results cleared."""
        self.test_dir, self.phase_file, self.code_file, self.name = None, None, None, ""
        self._asked += 1  # a describe still running lands nowhere
        self._phases_timer.stop()
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._saved = ""
        self._clear_results()
        self._show_described(None, "")

    def insert_line(self, number: int, text: str) -> None:
        """Insert *text* (whole lines) before line *number* (0: the first) of the editor."""
        cursor = QTextCursor(self.editor.document().findBlockByNumber(number))
        cursor.insertText(text)

    def append(self, text: str) -> None:
        """Add *text* at the end of the editor (a new shared phase), the cursor after it."""
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        self.editor.setFocus()

    def save(self) -> bool:
        """Write the editor's text to the open file (nothing when unmodified); False when it was
        not written (a failed write said, the editor still modified)."""
        if not self._write():
            return False
        self._ws.workspace_widget.shared_phases.load()  # "used by N tests" may have changed
        return True

    def _write(self) -> bool:
        """:meth:`save` without what follows it: a file changed outside Studio since it was read
        is asked about first (Reload, Overwrite, Cancel); a read-only one is never written."""
        if not self.dirty:
            return True
        if self.read_only:
            QMessageBox.warning(self, "Save Failed", READ_ONLY_TIP.format(file=self.name))
            return False
        try:
            changed = self.code_file.changed_outside()
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"{self.name} could not be read: {exc}")
            return False
        if changed:
            choice = ask_changed_outside(self, self.name)
            if choice == RELOAD:
                self.reload()
                return False
            if choice != OVERWRITE:
                return False
        text = self.text()
        try:
            self.code_file.write(text)
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"{self.name} could not be written: {exc}")
            return False
        self._saved = text
        self._show_state()
        self._ws.footer.show_message(f"{self.name} saved", 2000)
        return True

    def reload(self) -> None:
        """Read the file again (what the editor held is dropped) — unless it was deleted: then
        that is said and the editor keeps its text, modified, for a save to write it back."""
        if self.code_file.gone():
            QMessageBox.warning(self, CHANGED_TITLE, DELETED_TEXT.format(file=self.name))
            self.code_file.deleted()
            self._saved = None  # whatever the editor holds is unsaved
            self._show_state()
            return
        try:
            code_file = CodeFile(self.file)
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Reload", f"{self.name} cannot be read: {exc}")
            return
        self._load(code_file)
        self.read_phases()

    def reload_if_changed(self) -> None:
        """The file changed outside Studio and nothing is edited here: show it as it is now."""
        if not self.is_open or self.dirty:
            return
        try:
            changed = self.code_file.changed_outside()
        except OSError:
            return  # unreadable now (a share dropped): the next save says so
        if changed:
            self.reload()

    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        if state == Qt.ApplicationState.ApplicationActive:
            self.reload_if_changed()

    def _on_edited(self) -> None:
        self._show_state()
        self._phases_timer.start()
        if self._checked:  # its results are of the text before this edit
            self._clear_results()
            self.results.addWidget(ui_kit.note(NOT_CHECKED))

    def _show_state(self) -> None:
        dirty = self.dirty
        if self.read_only:
            ui_kit.set_pill(self.pill, "warning", "Read-only")
            self.pill.setToolTip(READ_ONLY_TIP.format(file=self.name))
        else:
            ui_kit.set_pill(
                self.pill, *(("warning", "● Modified") if dirty else ("success", "✓ Saved"))
            )
            self.pill.setToolTip("")
        self.save_button.setEnabled(dirty and not self.read_only)
        self.changed.emit()

    def _open_in_editor(self) -> None:
        """Open the file in the user's editor, saved first: the editor there shows what is here."""
        if not self.is_open or not self.save():
            return
        if why := open_in_editor(self.file):
            QMessageBox.warning(self, "Open in my editor", why)

    # ---- the phases (describe) ---------------------------------------------------------------

    def read_phases(self) -> None:
        """Ask the engine what the editor's text holds (off the GUI thread)."""
        if not self.is_open:
            return
        root, test_dir, name, source = (
            self._ws.project_manager.project_root,
            self.test_dir,
            self.name,
            self.text(),
        )
        self._phases_timer.stop()
        self._asked += 1
        asked = self._asked

        def done(answer) -> None:
            if shiboken6.isValid(self) and asked == self._asked:  # not closed meanwhile
                self._show_described(*answer())

        if test_dir is not None:
            read = lambda: code_test_check.describe(root, test_dir.name, source)  # noqa: E731
        else:
            read = lambda: code_test_check.describe_phase_file(root, name, source)  # noqa: E731
        engine_async.call(
            root, read, done, cue=self.phases.loading, cue_label="Reading the phases…"
        )

    def _show_described(self, described: dict | None, why: str) -> None:
        if not why:
            self.described = described
        self.show_phases(why)

    def show_phases(self, why: str = "") -> None:
        """The Phases tab from the last reading (and, for a file of shared phases, the tests
        that use each phase, as the Shared phases list last read them)."""
        described = self.described or {}
        if self.phase_file is None:
            entries = phase_entries(described)
            self.phases.show_entries(entries, equipment(described), why)
        else:
            used_by = {
                (p["file"], p["function"]): p["used_by"]
                for p in self._ws.workspace_widget.shared_phases.phases
            }
            entries = phase_entries(described, self.name, used_by if self.shared_file else None)
            self.phases.show_entries(entries, "", why)
        self.changed.emit()

    def _jump(self, line: int) -> None:
        self.editor.jump_to_line(line)
        self.editor.setFocus()

    def _open_shared(self, file: str, function: str, line: int) -> None:
        """A phase of another file clicked (``functions/…``, or ``openhtf/…`` of the test's own
        folder, as describe names them): that file opens here, at its line — never one that is
        not there."""
        folder, _, rest = file.partition("/")
        base = self._root() / "functions" if folder == "functions" else self.test_dir / folder
        path = base / rest
        if not path.is_file():
            QMessageBox.warning(self, "Open File", f"{file} is not there: {path}")
            return
        self._ws._code_test_controller.open_phase_file(path, line, (file, function))

    # ---- the Check ---------------------------------------------------------------------------

    def check(self) -> None:
        """The Check of the open file, written first when modified (its load reads the file),
        off the GUI thread; its results line under the editor, its describe in the phases."""
        if not self.is_open or not self._write():
            return
        root, test_dir, path, name = (
            self._ws.project_manager.project_root,
            self.test_dir,
            self.file,
            self.name,
        )
        self._phases_timer.stop()
        self._asked += 1  # the Check's describe is the phases': no other one lands over it
        asked = self._asked

        def done(answer) -> None:
            if not shiboken6.isValid(self) or self.code_file is None or self.file != path:
                return  # the window closed, or another file opened, meanwhile
            found = answer()
            self._show_results(check_lines(found, loads=test_dir is not None))
            if found.describe is not None and asked == self._asked:
                self._show_described(found.describe, "")

        if test_dir is not None:
            run = lambda: code_test_check.check(root, test_dir.name)  # noqa: E731
        else:
            run = lambda: code_test_check.check_phase_file(root, name)  # noqa: E731
        self._clear_results()
        engine_async.call(
            root,
            run,
            done,
            button=self.check_button,
            cue=self.check_cue,
            cue_label="Checking…",
        )

    def _clear_results(self) -> None:
        ui_kit.clear_layout(self.results)
        self._checked = False

    def _show_results(self, lines: list[tuple[str, str, str]]) -> None:
        """The ✓ / ✗ results side by side as on the board (wrapping when they do not fit), each
        ⚠ warning on its own line."""
        self._clear_results()
        self._checked = True
        row = QWidget()
        checks = FlowLayout(row, spacing=12)  # they wrap when the row is too narrow
        checks.setContentsMargins(0, 0, 0, 0)
        self.results.addWidget(row)
        for tone, text, tip in lines:
            label = QLabel(text)
            label.setObjectName("checkResult")
            label.setToolTip(tip)
            ui_kit.set_state(label, "tone", tone)
            if tone == "warning":
                label.setWordWrap(True)
                self.results.addWidget(label)
            else:
                checks.addWidget(label)

    def result_texts(self) -> list[str]:
        """The results as shown: the ✓ / ✗ row, then the ⚠ lines."""
        box = self.results.parentWidget()
        return [label.text() for label in box.findChildren(QLabel) if label.isVisibleTo(box)]
