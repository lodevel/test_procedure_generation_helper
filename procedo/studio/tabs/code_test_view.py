"""The middle of Procedo Studio for a code test (boards StudioCode and StudioCompare): the
switch Code · Phases · Export preview over the openhtf/test.py editor card (its Saved / ●
Modified pill, "Open in my editor", Save) and, side by side, the phase list the engine's
*describe* reads from it (clicking a phase or a sub-step jumps to its line) or the preview of
its export; under them the action bar: Check and its results line.

The file is the source of truth: the editor writes it as it is, its line endings kept. Every
engine call (describe, Check) runs off the GUI thread behind the shared loading cue.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
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

from procedo.core import atomic_write, code_test_check, test_format
from procedo.ui import ui_kit
from procedo.ui.segmented import segment, segmented

from ..core.code_test_preview import check_lines, phase_rows, preview_html
from ..core.open_in_editor import open_in_editor
from ..pack_ops import engine_async
from ..widgets.editor_card import CodeEditor
from ..widgets.python_highlighter import PythonSyntaxHighlighter

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

#: The views of a code test: the switch's labels.
VIEWS = ("Code", "Phases", "Export preview")
CODE, PHASES, PREVIEW = range(3)
#: The code version's file in its test folder, as the card names it.
FILE = f"{test_format.CODE_DIR}/test.py"


class CodeTestView(QWidget):
    """The open code test's middle column (``test_dir`` None: none is open)."""

    #: The text was edited or saved, or the phases were read: the header follows.
    changed = Signal()

    def __init__(self, ws: StudioWorkspace) -> None:
        super().__init__()
        self._ws = ws
        self.test_dir: Path | None = None
        self.described: dict | None = None
        self._saved = ""  # the file's text as on disk, its line breaks "\n"
        self._crlf = False
        self._asked = 0  # the describe awaited: an older answer is dropped

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
        self.results = QHBoxLayout()
        self.results.setSpacing(12)
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        row.addWidget(self.check_button)
        row.addWidget(self.check_cue)
        row.addLayout(self.results)
        row.addStretch(1)
        return bar

    # ---- the open test ---------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """The editor holds changes not written to openhtf/test.py."""
        return self.test_dir is not None and self.editor.toPlainText() != self._saved

    @property
    def file(self) -> Path:
        return self.test_dir / test_format.CODE_DIR / "test.py"

    def open(self, test_dir: Path) -> bool:
        """Show the code test in *test_dir* (its file read, its phases asked); False when its
        file cannot be read (said to the user)."""
        path = test_dir / test_format.CODE_DIR / "test.py"
        try:
            raw = path.read_bytes().decode("utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(
                self, "Open Test", f"{FILE} of {test_dir.name} cannot be read: {exc}"
            )
            return False
        self.test_dir = test_dir
        self._crlf = "\r\n" in raw
        self._saved = raw.replace("\r\n", "\n")
        self.editor.blockSignals(True)
        self.editor.setPlainText(self._saved)
        self.editor.blockSignals(False)
        self._clear_results()
        self._show_described(None, "")
        self._show_state()
        self.read_phases()
        return True

    def close(self) -> None:
        """No code test open: the editor, the phases and the results cleared."""
        self.test_dir = None
        self._asked += 1  # a describe still running lands nowhere
        self._saved = ""
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._clear_results()
        self._show_described(None, "")

    def show_view(self, index: int) -> None:
        """Code: the editor and the phases; Phases: the phases alone; Export preview: the
        editor and the preview."""
        self.view_buttons.button(index).setChecked(True)
        self.editor_card.setVisible(index != PHASES)
        self.side.setCurrentIndex(1 if index == PREVIEW else 0)

    def save(self) -> bool:
        """Write the editor's text to openhtf/test.py (its line endings kept); False when the write
        failed (said to the user, the editor stays modified)."""
        if self.test_dir is None:
            return True
        text = self.editor.toPlainText()
        try:
            self.file.parent.mkdir(exist_ok=True)
            atomic_write.write_bytes(
                self.file, (text.replace("\n", "\r\n") if self._crlf else text).encode("utf-8")
            )
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"{FILE} could not be written: {exc}")
            return False
        self._saved = text
        self._show_state()
        self._ws.footer.show_message(f"{FILE} saved", 2000)
        self.read_phases()
        self._ws.workspace_widget.shared_phases.load()  # "used by N tests" may have changed
        return True

    def _on_edited(self) -> None:
        self._show_state()

    def _show_state(self) -> None:
        dirty = self.dirty
        ui_kit.set_pill(
            self.pill, *(("warning", "● Modified") if dirty else ("success", "✓ Saved"))
        )
        self.save_button.setEnabled(dirty)
        self.changed.emit()

    def _open_in_editor(self) -> None:
        if self.test_dir is not None and (why := open_in_editor(self.file)):
            QMessageBox.warning(self, "Open in my editor", why)

    # ---- the phases (describe) ---------------------------------------------------------------

    def read_phases(self) -> None:
        """Ask the engine what openhtf/test.py holds, as saved (off the GUI thread)."""
        root, test_dir = self._ws.project_manager.project_root, self.test_dir
        if test_dir is None:
            return
        self._asked += 1
        asked = self._asked

        def done(answer) -> None:
            if asked == self._asked:
                self._show_described(*answer())

        engine_async.call(
            root,
            lambda: code_test_check.describe(root, test_dir.name),
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
        """The Check of openhtf/test.py, saved first when modified (it reads the file), off the
        GUI thread; its results line under the views."""
        if self.test_dir is None or (self.dirty and not self.save()):
            return
        root, test_dir = self._ws.project_manager.project_root, self.test_dir

        def done(answer) -> None:
            if self.test_dir != test_dir:
                return  # another test opened meanwhile
            found = answer()
            self._show_results(check_lines(found))
            if found.describe is not None:
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
        self._clear_results()
        for tone, text, tip in lines:
            label = QLabel(text)
            label.setObjectName("checkResult")
            label.setToolTip(tip)
            ui_kit.set_state(label, "tone", tone)
            self.results.addWidget(label)

    def result_texts(self) -> list[str]:
        """The results line as shown."""
        items = (self.results.itemAt(i).widget() for i in range(self.results.count()))
        return [w.text() for w in items if w is not None]
