"""The Workspace column's "Shared phases (OpenHTF)" (boards StudioPhases, StudioPanel D), under
the tests: the reusable phases of the project's ``functions/`` — a function carrying at least
``@htf.PhaseOptions(name=…)`` (the engine's one detector, ``code_test.phase_name``) — each row
its name, what a test writes (``power_off()``), its description, its file and how many tests use
it, and its own buttons: "⧉ Copy import" (the line a test writes to use it) and "✎ Edit" (its
file in Studio's editor, as a click on the row does); "+ New shared phase…" under the list.

Shown while a code test or a file of shared phases is open (text tests cannot use them yet); read
off the GUI thread behind the shared loading cue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import shiboken6

from procedo.core import code_test_check
from procedo.ui import ui_kit

from ..pack_ops import engine_async

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

TITLE = "Shared phases (OpenHTF)"
COPY, COPIED = "⧉ Copy import", "✓ Copied"
#: How long "✓ Copied" shows (ms).
COPIED_MS = 2000


def used_by_text(n: int) -> str:
    """``used by 1 test``, ``used by 3 tests``."""
    return f"used by {n} test{'' if n == 1 else 's'}"


def phase_lines(phase: dict) -> tuple[str, str, str, str]:
    """A shared phase's four lines: its name; what a test writes (``power_up(voltage,
    current_limit)``); its description; its file and ``used by N tests``."""
    call = f"{phase['function']}({', '.join(phase.get('values') or ())})"
    where = f"{phase['file']} · {used_by_text(len(phase.get('used_by') or ()))}"
    return phase["name"], call, phase.get("description", ""), where


def import_line(phase: dict) -> str:
    """What a test writes to use *phase*: ``from common_phases import power_off``."""
    return f"from {phase['module']} import {phase['function']}"


class _Row(QFrame):
    """One phase's lines and its buttons; a click on the row edits it."""

    def __init__(self, phase: dict, edit) -> None:
        super().__init__()
        self.setObjectName("sharedPhase")
        self._edit = edit
        name, call, about, where = phase_lines(phase)
        self.copy_button = QPushButton(COPY)
        self.copy_button.setObjectName("rowButton")
        self.copy_button.setToolTip(f"Copies: {import_line(phase)}")
        self.copy_button.clicked.connect(lambda: self._copy(import_line(phase)))
        self.edit_button = QPushButton("✎ Edit")
        self.edit_button.setObjectName("rowButton")
        self.edit_button.setToolTip(f"Edit {phase['file']} in Studio's editor")
        self.edit_button.clicked.connect(lambda: edit())
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(2)
        column.addWidget(ui_kit.bold(name))
        column.addWidget(ui_kit.mono(QLabel(call, wordWrap=True)))
        if about:
            column.addWidget(QLabel(about, wordWrap=True))
        column.addWidget(ui_kit.note(where))
        buttons = ui_kit.hbox(self.copy_button, self.edit_button, None)
        buttons.setContentsMargins(0, 4, 0, 0)
        column.addLayout(buttons)
        ui_kit.set_state(self, "selected", False)

    def _copy(self, line: str) -> None:
        QApplication.clipboard().setText(line)
        self.copy_button.setText(COPIED)
        QTimer.singleShot(
            COPIED_MS, lambda: shiboken6.isValid(self) and self.copy_button.setText(COPY)
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            self._edit()
        super().mousePressEvent(event)


#: The list's tallest (it scrolls past): the tests above keep their room.
_MAX_HEIGHT = 300


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
    """The shared phases of the open project, while a code test or a file of them is open.
    ``loaded``: the list was read again."""

    loaded = Signal()

    def __init__(self, ws: StudioWorkspace) -> None:
        super().__init__()
        self._ws = ws
        self.phases: list[dict] = []
        self._rows: list[_Row] = []
        self.current: tuple[str, str] | None = None  # the phase being edited: file, function
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
        self.new_button = QPushButton("+ New shared phase…")
        self.new_button.setToolTip("Write a new phase the project's tests can use")
        self.new_button.clicked.connect(lambda: ws._code_test_controller.new_shared_phase())
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 8, 12, 8)
        column.setSpacing(8)
        column.addLayout(ui_kit.hbox(ui_kit.bold(TITLE), None, self.cue))
        column.addWidget(self.list)
        column.addLayout(ui_kit.hbox(self.new_button, None))
        self.hide()

    def load(self) -> None:
        """Read the project's shared phases again (off the GUI thread) and show them — only
        while a code test or a file of them is open."""
        root = self._ws.project_manager.project_root
        if root is None or not self._ws.code_view.is_open:
            self.clear()
            return
        self._asked += 1
        asked = self._asked

        def done(answer) -> None:
            if shiboken6.isValid(self) and asked == self._asked:  # not closed meanwhile
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
        self.current = None
        self._show([])
        self.hide()

    def _show(self, phases: list[dict]) -> None:
        self.phases = phases
        ui_kit.clear_layout(self._column)
        self._rows = [
            _Row(phase, lambda p=phase: self._ws._code_test_controller.edit_shared_phase(p))
            for phase in phases
        ]
        for row in self._rows:
            self._column.addWidget(row)
        self._column.addStretch(1)
        self.list.setVisible(bool(phases))
        self.list.updateGeometry()
        self.mark(self.current)
        self.loaded.emit()

    def mark(self, current: tuple[str, str] | None) -> None:
        """The phase being edited (its file, its function), tinted; None: none."""
        self.current = current
        for phase, row in zip(self.phases, self._rows, strict=True):
            ui_kit.set_state(row, "selected", (phase["file"], phase["function"]) == current)

    def rows(self) -> list[_Row]:
        return list(self._rows)
