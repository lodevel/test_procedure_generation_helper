"""The Workspace column's "Shared phases (OpenHTF)" (board StudioPhases), under the tests: the
reusable phases of the project's ``functions/`` — a function carrying at least
``@htf.PhaseOptions(name=…)`` (the engine's one detector, ``code_test.phase_name``) — each with
its name, description, values, file and how many tests use it; "Copy import line" and "Open
file" act on the selected one.

Shown only while a code test is open (text tests cannot use them yet) and the project has some;
read off the GUI thread behind the shared loading cue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from procedo.core import code_test_check
from procedo.ui import ui_kit

from ..core.open_in_editor import open_in_editor
from ..pack_ops import engine_async

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

TITLE = "Shared phases (OpenHTF)"


def phase_lines(phase: dict) -> tuple[str, str, str]:
    """A shared phase's three lines: its name; its description and ``values: a, b``; its file
    and ``used by N tests``."""
    values = phase.get("values") or ()
    about = " · ".join(
        part
        for part in (phase.get("description", ""), f"values: {', '.join(values)}" if values else "")
        if part
    )
    n = phase.get("used_by", 0)
    return phase["name"], about, f"{phase['file']} · used by {n} test{'' if n == 1 else 's'}"


def import_line(phase: dict) -> str:
    """What a test writes to use *phase*: ``from common_phases import power_off``."""
    return f"from {phase['module']} import {phase['function']}"


class _Row(QFrame):
    """One phase's three lines; a click selects it (tinted)."""

    def __init__(self, phase: dict, select) -> None:
        super().__init__()
        self.setObjectName("sharedPhase")
        self._select = select
        name, about, where = phase_lines(phase)
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(2)
        column.addWidget(ui_kit.bold(name))
        if about:
            column.addWidget(QLabel(about, wordWrap=True))
        column.addWidget(ui_kit.note(where))
        ui_kit.set_state(self, "selected", False)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._select(self)
        super().mousePressEvent(event)


#: The list's tallest (it scrolls past): the tests above keep their room.
_MAX_HEIGHT = 260


class _Rows(QScrollArea):
    """The rows, as tall as they are at its width (their lines wrap), at most :data:`_MAX_HEIGHT`."""

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        rows, width = self.widget(), self.viewport().width()
        height = rows.heightForWidth(width)  # -1 before it has a width
        return QSize(width, min(height if height > 0 else rows.sizeHint().height(), _MAX_HEIGHT))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.updateGeometry()  # its rows wrap anew


class SharedPhasesPanel(QWidget):
    """The shared phases of the open project, while a code test is open."""

    def __init__(self, ws: StudioWorkspace) -> None:
        super().__init__()
        self._ws = ws
        self.phases: list[dict] = []
        self._rows: list[_Row] = []
        self._current = -1
        self._asked = 0
        self.cue = ui_kit.LoadingCue()
        rows = QWidget()
        self._column = QVBoxLayout(rows)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(2)
        self.list = _Rows()
        self.list.setWidget(rows)
        self.list.setWidgetResizable(True)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.copy_button = QPushButton("Copy import line")
        self.copy_button.setToolTip("Copy the line a test writes to use the selected phase")
        self.copy_button.clicked.connect(self._copy_import_line)
        self.open_button = QPushButton("Open file")
        self.open_button.setToolTip("Open the selected phase's file in this PC's editor")
        self.open_button.clicked.connect(self._open_file)
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 8, 12, 8)
        column.setSpacing(8)
        column.addLayout(ui_kit.hbox(ui_kit.bold(TITLE), None, self.cue))
        column.addWidget(self.list)
        column.addLayout(ui_kit.hbox(self.copy_button, self.open_button, None))
        self.hide()

    def load(self) -> None:
        """Read the project's shared phases again (off the GUI thread) and show them — only
        while a code test is open."""
        root = self._ws.project_manager.project_root
        if root is None or self._ws.code_view.test_dir is None:
            self.clear()
            return
        self._asked += 1
        asked = self._asked

        def done(answer) -> None:
            if asked == self._asked:
                self._show(answer()[0])

        self.show()  # its title and the cue while the engine reads them
        engine_async.call(
            root,
            lambda: code_test_check.shared_phases(root),
            done,
            cue=self.cue,
            cue_label="Reading…",
        )

    def clear(self) -> None:
        """Hidden (a text test is open, or none): text tests cannot use shared phases yet."""
        self._asked += 1
        self._show([])

    def _show(self, phases: list[dict]) -> None:
        self.phases = phases
        ui_kit.clear_layout(self._column)
        self._rows = [_Row(phase, self._select) for phase in phases]
        for row in self._rows:
            self._column.addWidget(row)
        self._column.addStretch(1)
        self.list.updateGeometry()
        self.select(0 if phases else -1)
        self.setVisible(bool(phases))

    def _select(self, row: _Row) -> None:
        self.select(self._rows.index(row))

    def select(self, index: int) -> None:
        """The phase at *index* is the one the buttons act on (-1: none)."""
        self._current = index
        for i, row in enumerate(self._rows):
            ui_kit.set_state(row, "selected", i == index)
        for button in (self.copy_button, self.open_button):
            button.setEnabled(index >= 0)

    def _selected(self) -> dict | None:
        return self.phases[self._current] if 0 <= self._current < len(self.phases) else None

    def _copy_import_line(self) -> None:
        if (phase := self._selected()) is not None:
            QApplication.clipboard().setText(import_line(phase))
            self._ws.footer.show_message(f"Copied: {import_line(phase)}", 3000)

    def _open_file(self) -> None:
        phase, root = self._selected(), self._ws.project_manager.project_root
        if phase is not None and (why := open_in_editor(root / phase["file"])):
            QMessageBox.warning(self, "Open file", why)
