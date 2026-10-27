"""The Phases tab of Studio's right panel (board StudioPanel A, E): the one phase list of the open
code test, or of the open file of shared phases, read from its code by the engine — each phase
numbered, its line or ``shared · teardown`` at the right, folded into a summary (``6 sub-steps ·
1 question · J2 pins 1–10``) or open on its details (description, questions, sub-steps,
measurements and limits, test points; in a file of shared phases, the tests that use it); the
Equipment line at the bottom. A click on a phase (or a line of it) goes to its line in the
editor; on a phase of another file, opens that file. Which phases are open and the one clicked
stay as they were while the code is read again.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from procedo.ui import ui_kit

from ..core.code_test_preview import PhaseEntry


class _Line(QLabel):
    """A line of a phase; a click goes to *line* (0: to the phase's own)."""

    clicked = Signal()

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _PhaseRow(QFrame):
    """One phase: ▸ / ▾, ``4 AUX0 voltage, no load`` and its mark; under it its summary (folded)
    or its details (open)."""

    def __init__(self, entry: PhaseEntry, panel: PhasesPanel, open_: bool) -> None:
        super().__init__()
        self.setObjectName("phaseRow")
        self.entry = entry
        self._panel = panel
        self.arrow = QToolButton()
        self.arrow.setObjectName("phaseFold")
        self.arrow.setAutoRaise(True)
        self.arrow.setText("▾" if open_ else "▸")
        self.arrow.setToolTip("Fold this phase" if open_ else "Show this phase's details")
        self.arrow.clicked.connect(lambda: panel.toggle(entry.key))
        title = _Line(f"{entry.number} {entry.name}")
        title.setObjectName("phaseTitle")
        title.clicked.connect(lambda: panel.go(entry, entry.line))
        mark = QLabel()
        if entry.here:
            mark.setText(entry.mark)
            mark.setObjectName("fieldNote")
        else:
            ui_kit.set_pill(mark, "neutral", entry.mark)
        head = ui_kit.hbox(self.arrow, title, None, mark)
        head.setSpacing(4)
        head.setAlignment(mark, Qt.AlignmentFlag.AlignTop)
        head.setAlignment(self.arrow, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setContentsMargins(26, 0, 0, 0)
        body.setSpacing(1)
        lines = (
            entry.details()
            if open_
            else [("summary", text, entry.line) for text in entry.summary()]
        )
        for kind, text, line in lines:
            label = _Line(text)
            label.setObjectName("phaseLine")
            ui_kit.set_state(label, "kind", kind)
            label.clicked.connect(lambda line=line: panel.go(entry, line))
            body.addWidget(label)
        column = QVBoxLayout(self)
        column.setContentsMargins(6, 6, 8, 6)
        column.setSpacing(2)
        column.addLayout(head)
        column.addLayout(body)
        self.setToolTip(
            f"Line {entry.line}: click to go there"
            if entry.here
            else f"In {entry.file}, line {entry.line}: click to open it"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            self._panel.go(self.entry, self.entry.line)
        super().mousePressEvent(event)


class PhasesPanel(QWidget):
    """The Phases tab. ``jump(line)``: go to *line* of the open file; ``open_file(file, function,
    line)``: open *file* (``functions/…``) at *line*, the phase *function* being edited."""

    jump = Signal(int)
    open_file = Signal(str, str, int)

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[PhaseEntry] = []
        self._row_widgets: list[_PhaseRow] = []
        self._open: set[tuple[str, str]] = set()  # the phases shown open, by file and function
        self._current: tuple[str, str] | None = None  # the one clicked last
        self.loading = ui_kit.LoadingCue()
        self.problem = ui_kit.note()
        rows = QWidget()
        self._rows = QVBoxLayout(rows)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(2)
        self.list = QScrollArea()
        self.list.setWidget(rows)
        self.list.setWidgetResizable(True)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.equipment = ui_kit.note()
        self._equipment_rule = ui_kit.row_rule()
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(8)
        column.addWidget(self.loading)
        column.addWidget(self.problem)
        column.addWidget(self.list, 1)
        column.addWidget(self._equipment_rule)
        column.addWidget(self.equipment)
        self.show_entries([], "")

    def show_entries(self, entries: list[PhaseEntry], equipment: str, why: str = "") -> None:
        """List *entries*, *equipment* at the bottom ("" hides it); *why*: why the code cannot be
        read (the list as it was stays)."""
        self.problem.setText(f"The phases cannot be read: {why}" if why else "")
        self.problem.setVisible(bool(why))
        if why:
            return
        self.entries = entries
        self.equipment.setText(equipment)
        self.equipment.setVisible(bool(equipment))
        self._equipment_rule.setVisible(bool(equipment))
        self._paint()

    def _paint(self) -> None:
        bar = self.list.verticalScrollBar()
        top = bar.value()
        ui_kit.clear_layout(self._rows)
        self._row_widgets = [_PhaseRow(e, self, e.key in self._open) for e in self.entries]
        for row in self._row_widgets:
            ui_kit.set_state(row, "selected", row.entry.key == self._current)
            self._rows.addWidget(row)
        self._rows.addStretch(1)
        bar.setValue(top)

    def toggle(self, key: tuple[str, str]) -> None:
        """Open the phase *key*, or fold it."""
        self._open.symmetric_difference_update({key})
        self._paint()

    def go(self, entry: PhaseEntry, line: int) -> None:
        """A click on *entry* (at *line*): it is the current one, open; its line in the editor,
        or its file opened."""
        self._current = entry.key
        self._open.add(entry.key)
        self._paint()
        if entry.here:
            self.jump.emit(line)
        else:
            self.open_file.emit(entry.file, entry.function, line)

    def rows(self) -> list[_PhaseRow]:
        """The rows as shown."""
        return list(self._row_widgets)
