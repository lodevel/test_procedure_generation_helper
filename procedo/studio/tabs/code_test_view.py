"""The middle of Procedo Studio for a code test (boards StudioCode and StudioCompare): the
switch Code · Phases · Export preview over the openhtf/test.py editor card (its Saved / ●
Modified pill, "Open in my editor", Save) and, side by side, the phase list the engine's
*describe* reads from the editor's text (clicking a phase or a sub-step jumps to its line) or the
preview of its export; under them the action bar: Check and its results line.

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
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import shiboken6

from procedo.core import code_test_check, test_format
from procedo.ui import ui_kit
from procedo.ui.segmented import segment, segmented

from ..core.code_file import CodeFile
from ..core.code_test_preview import check_lines, phase_rows, preview_html
from ..core.open_in_editor import open_in_editor
from ..pack_ops import engine_async
from ..widgets.editor_card import CodeEditor
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts
from ..widgets.python_highlighter import PythonSyntaxHighlighter

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

#: The views of a code test: the switch's labels.
VIEWS = ("Code", "Phases", "Export preview")
CODE, PHASES, PREVIEW = range(3)
#: The code version's file in its test folder, as the card names it.
FILE = test_format.CODE_SCRIPT
#: How long the phases wait after the last keystroke before they are read again (ms).
_PHASES_DELAY_MS = 400
#: Why a file opens read-only.
READ_ONLY_TIP = (
    f"Studio cannot keep {FILE} exactly as it is (mixed line endings or special characters): "
    "edit it in your editor."
)
#: The question when the file changed outside Studio before a save.
CHANGED_TITLE = "Changed outside Studio"
CHANGED_TEXT = f"{FILE} was changed outside Studio since it was opened."
DELETED_TEXT = f"{FILE} was deleted outside Studio."
RELOAD, OVERWRITE = "reload", "overwrite"


def ask_changed_outside(parent: QWidget) -> str:
    """:data:`RELOAD`, :data:`OVERWRITE`, or "" (Cancel): what to do with a file changed outside
    Studio before a save."""
    box = QMessageBox(QMessageBox.Icon.Warning, CHANGED_TITLE, CHANGED_TEXT, parent=parent)
    reload = box.addButton("Reload", QMessageBox.ButtonRole.DestructiveRole)
    overwrite = box.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return {reload: RELOAD, overwrite: OVERWRITE}.get(box.clickedButton(), "")


class CodeTestView(QWidget):
    """The open code test's middle column (``test_dir`` None: none is open)."""

    #: The text was edited or saved, or the phases were read: the header follows.
    changed = Signal()

    def __init__(self, ws: StudioWorkspace) -> None:
        super().__init__()
        self._ws = ws
        self.test_dir: Path | None = None
        self.described: dict | None = None
        self.code_file: CodeFile | None = None
        self.read_only = False  # the editor cannot give the file's bytes back
        # The editor's text when the file was read or written (None: the file was deleted).
        self._saved: str | None = ""
        self._asked = 0  # the describe awaited: an older answer is dropped
        self._phases_timer = QTimer(self)  # the phases follow the editor, once typing pauses
        self._phases_timer.setSingleShot(True)
        self._phases_timer.setInterval(_PHASES_DELAY_MS)
        self._phases_timer.timeout.connect(self.read_phases)
        # Back from another app (the user's editor): a file changed there is reloaded.
        QCoreApplication.instance().applicationStateChanged.connect(self._on_app_state)

        self.view_buttons = QButtonGroup(self)
        buttons = [segment(label) for label in VIEWS]
        for index, button in enumerate(buttons):
            self.view_buttons.addButton(button, index)
        self.view_buttons.idClicked.connect(self.show_view)

        self.editor_card = self._editor_card()
        self.phases_card = ui_kit.Card("Phases", "read from the code")
        self.phase_tree = QTreeWidget()
        self.phase_tree.setObjectName("stepTree")
        self.phase_tree.setHeaderHidden(True)
        self.phase_tree.setIndentation(18)
        self.phase_tree.setRootIsDecorated(False)
        self.phase_tree.itemClicked.connect(self._on_phase_clicked)
        self.phases_note = ui_kit.note()
        self.phases_card.body.addWidget(self.phase_tree, 1)
        self.phases_card.body.addWidget(self.phases_note)
        self.preview_card = ui_kit.Card("Preview of the export", "read from the code, not run")
        self.preview = QTextBrowser()
        self.preview.setObjectName("exportPreview")
        self.preview_card.body.addWidget(self.preview, 1)
        self.side = QStackedWidget()
        self.side.addWidget(self.phases_card)
        self.side.addWidget(self.preview_card)
        pair = QHBoxLayout()
        pair.setSpacing(16)
        pair.addWidget(self.editor_card, 1)
        pair.addWidget(self.side, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(segmented(*buttons, large=True))
        self.find_bar = FindReplaceBar(self)  # Edit › Find (Ctrl+F) finds in the code
        layout.addWidget(self.find_bar)
        install_find_shortcuts(self, [self.editor], self.find_bar)
        layout.addLayout(pair, 1)
        layout.addWidget(self._action_bar())
        self.show_view(CODE)

    def _editor_card(self) -> QFrame:
        card = ui_kit.card_frame()
        name = QLabel(FILE)
        name.setObjectName("cardFileName")
        self.pill = QLabel()
        self.open_button = QPushButton("Open in my editor")
        self.open_button.setToolTip(f"Open {FILE} in this PC's editor for Python files")
        self.open_button.clicked.connect(self._open_in_editor)
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip(f"Save {FILE} to disk")
        self.save_button.clicked.connect(self.save)
        head = ui_kit.hbox(name, self.pill, None, self.open_button, self.save_button)
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
        self.check_button.setToolTip(
            f"Check {FILE}: its syntax, its load in the project's environment, what it holds"
        )
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

    # ---- the open test ---------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """The editor holds changes not written to openhtf/test.py."""
        return self.test_dir is not None and self.text() != self._saved

    @property
    def file(self) -> Path:
        return test_format.script_of(self.test_dir, test_format.CODE)

    def text(self) -> str:
        """The editor's text exactly (a non-breaking space stays one), its line breaks "\n"."""
        return self.editor.document().toRawText().replace("\u2029", "\n")

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
        self.test_dir = test_dir
        self._load(code_file)
        self._clear_results()
        self._show_described(None, "")
        self.check_button.setEnabled(not engine_async.busy(self.check_button))
        self.read_phases()
        return True

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
        """No code test open: the editor, the phases and the results cleared."""
        self.test_dir, self.code_file = None, None
        self._asked += 1  # a describe still running lands nowhere
        self._phases_timer.stop()
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._saved = ""
        self._clear_results()
        self._show_described(None, "")

    def show_view(self, index: int) -> None:
        """Code: the editor and the phases; Phases: the phases alone; Export preview: the
        editor and the preview."""
        self.view_buttons.button(index).setChecked(True)
        self.editor_card.setVisible(index != PHASES)
        self.side.setCurrentIndex(1 if index == PREVIEW else 0)

    def save(self) -> bool:
        """Write the editor's text to openhtf/test.py (nothing when unmodified); False when it was
        not written (a failed write said, the editor still modified)."""
        if not self._write():
            return False
        self._ws.workspace_widget.shared_phases.load()  # "used by N tests" may have changed
        return True

    def _write(self) -> bool:
        """:meth:`save` without what follows it: a file changed outside Studio since it was read
        is asked about first (Reload, Overwrite, Cancel)."""
        if not self.dirty:
            return True
        try:
            changed = self.code_file.changed_outside()
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"{FILE} could not be read: {exc}")
            return False
        if changed:
            choice = ask_changed_outside(self)
            if choice == RELOAD:
                self.reload()
                return False
            if choice != OVERWRITE:
                return False
        text = self.text()
        try:
            self.code_file.write(text)
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"{FILE} could not be written: {exc}")
            return False
        self._saved = text
        self._show_state()
        self._ws.footer.show_message(f"{FILE} saved", 2000)
        return True

    def reload(self) -> None:
        """Read the file again (what the editor held is dropped) — unless it was deleted: then
        that is said and the editor keeps its text, modified, for a save to write it back."""
        if self.code_file.gone():
            QMessageBox.warning(self, CHANGED_TITLE, DELETED_TEXT)
            self.code_file.deleted()
            self._saved = None  # whatever the editor holds is unsaved
            self._show_state()
            return
        try:
            code_file = CodeFile(self.file)
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Reload", f"{FILE} cannot be read: {exc}")
            return
        self._load(code_file)
        self.read_phases()

    def reload_if_changed(self) -> None:
        """The file changed outside Studio and nothing is edited here: show it as it is now."""
        if self.test_dir is None or self.dirty:
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

    def _show_state(self) -> None:
        dirty = self.dirty
        if self.read_only:
            ui_kit.set_pill(self.pill, "warning", "Read-only")
            self.pill.setToolTip(READ_ONLY_TIP)
        else:
            ui_kit.set_pill(
                self.pill, *(("warning", "● Modified") if dirty else ("success", "✓ Saved"))
            )
            self.pill.setToolTip("")
        self.save_button.setEnabled(dirty)
        self.changed.emit()

    def _open_in_editor(self) -> None:
        """Open the file in the user's editor, saved first: the editor there shows what is here."""
        if self.test_dir is None or not self.save():
            return
        if why := open_in_editor(self.file):
            QMessageBox.warning(self, "Open in my editor", why)

    # ---- the phases (describe) ---------------------------------------------------------------

    def read_phases(self) -> None:
        """Ask the engine what the editor's text holds (off the GUI thread)."""
        root, test_dir, source = self._ws.project_manager.project_root, self.test_dir, self.text()
        if test_dir is None:
            return
        self._phases_timer.stop()
        self._asked += 1
        asked = self._asked

        def done(answer) -> None:
            if shiboken6.isValid(self) and asked == self._asked:  # not closed meanwhile
                self._show_described(*answer())

        engine_async.call(
            root,
            lambda: code_test_check.describe(root, test_dir.name, source),
            done,
            cue=self.phases_card.loading,
            cue_label="Reading the phases…",
        )

    def _show_described(self, described: dict | None, why: str) -> None:
        self.described = described
        self.phase_tree.clear()
        parents: list[QTreeWidgetItem] = []
        for row in phase_rows(described or {}):
            item = QTreeWidgetItem([row.text])
            item.setData(0, Qt.ItemDataRole.UserRole, row.line)
            item.setToolTip(0, row.tip or f"{FILE}, line {row.line}: click to go there")
            if row.depth == 0:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
                self.phase_tree.addTopLevelItem(item)
                parents.append(item)
            else:
                parents[-1].addChild(item)
        self.phase_tree.expandAll()
        self.phases_note.setText(f"The phases cannot be read: {why}" if why else "")
        self.phases_note.setVisible(bool(why))
        self.preview.setHtml(preview_html(described) if described else "")
        self.changed.emit()

    def _on_phase_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        line = item.data(0, Qt.ItemDataRole.UserRole)
        if not line:
            return
        if self.editor_card.isHidden():
            self.show_view(CODE)
        self.editor.jump_to_line(line)
        self.editor.setFocus()

    # ---- the Check ---------------------------------------------------------------------------

    def check(self) -> None:
        """The Check of openhtf/test.py, written first when modified (its load reads the file),
        off the GUI thread; its results line under the views, its describe in the phases."""
        if self.test_dir is None or not self._write():
            return
        root, test_dir = self._ws.project_manager.project_root, self.test_dir
        self._phases_timer.stop()
        self._asked += 1  # the Check's describe is the phases': no other one lands over it
        asked = self._asked

        def done(answer) -> None:
            if not shiboken6.isValid(self) or self.test_dir != test_dir:
                return  # the window closed, or another test opened, meanwhile
            found = answer()
            self._show_results(check_lines(found))
            if found.describe is not None and asked == self._asked:
                self._show_described(found.describe, "")

        self._clear_results()
        engine_async.call(
            root,
            lambda: code_test_check.check(root, test_dir.name),
            done,
            button=self.check_button,
            cue=self.check_cue,
            cue_label="Checking…",
        )

    def _clear_results(self) -> None:
        ui_kit.clear_layout(self.results)

    def _show_results(self, lines: list[tuple[str, str, str]]) -> None:
        """The ✓ / ✗ results side by side as on the board, each ⚠ warning on its own line."""
        self._clear_results()
        row = QWidget()
        checks = QHBoxLayout(row)
        checks.setContentsMargins(0, 0, 0, 0)
        checks.setSpacing(12)
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
        checks.addStretch(1)

    def result_texts(self) -> list[str]:
        """The results as shown: the ✓ / ✗ row, then the ⚠ lines."""
        return [label.text() for label in self.results.parentWidget().findChildren(QLabel)]
