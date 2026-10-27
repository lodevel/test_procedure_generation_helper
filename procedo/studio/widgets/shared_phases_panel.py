"""The left column's Shared phases list (boards StudioPhases, StudioPanel D, StudioLeftTabs): the
reusable phases of the project's ``functions/`` — a function carrying at least
``@htf.PhaseOptions(name=…)`` (the engine's one detector, ``code_test.phase_name``) — each row
its name, what a test writes (``power_off()``), its description, its file and how many tests use
it, and its own buttons: "⧉ Copy import" (the line a test writes to use it) and "✎ Edit" (its
file in Studio's editor, as a click on the row does); ``new_button``, "+ New shared phase…", is
laid out under the list by the column.

Read for the open project, whatever is open in the middle, off the GUI thread behind the shared
loading cue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
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


class SharedPhasesPanel(QWidget):
    """The shared phases of the open project. ``loaded``: the list was read again."""

    loaded = Signal()

    def __init__(self, ws: StudioWorkspace) -> None:
        super().__init__()
        self._ws = ws
        self.phases: list[dict] = []
        self._rows: list[_Row] = []
        self.current: tuple[str, str] | None = None  # the phase being edited: file, function
        self._root = None  # the project the list is of
        self._asked = 0
        self.cue = ui_kit.LoadingCue()
        rows = QWidget()
        self._column = QVBoxLayout(rows)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(2)
        self.list = QScrollArea()
        self.list.setWidget(rows)
        self.list.setWidgetResizable(True)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.new_button = QPushButton("+ New shared phase…")
        self.new_button.setToolTip("Write a new phase the project's tests can use")
        self.new_button.clicked.connect(lambda: ws._code_test_controller.new_shared_phase())
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(self.cue)
        column.addWidget(self.list, 1)

    def load(self) -> None:
        """Read the open project's shared phases again (off the GUI thread) and show them;
        another project's are cleared at once, never shown while its own are read."""
        root = self._ws.project_manager.project_root
        self._asked += 1
        asked = self._asked
        switched, self._root = root != self._root, root
        if root is not None:

            def done(answer) -> None:
                if shiboken6.isValid(self) and asked == self._asked:  # not closed meanwhile
                    self._show(answer()[0])

            engine_async.call(
                root,
                lambda: code_test_check.shared_phases(root),
                done,
                cue=self.cue,
                cue_label="Reading…",
            )
        if switched or root is None:  # after the call: its cue says the list is being read
            self.current = None
            self._show([])

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
        self.mark(self.current)
        self.loaded.emit()

    def mark(self, current: tuple[str, str] | None) -> None:
        """The phase being edited (its file, its function), tinted; None: none."""
        self.current = current
        for phase, row in zip(self.phases, self._rows, strict=True):
            ui_kit.set_state(row, "selected", (phase["file"], phase["function"]) == current)

    def rows(self) -> list[_Row]:
        return list(self._rows)
