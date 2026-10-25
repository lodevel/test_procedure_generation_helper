"""The procedure's steps in a card, as on the approved boards S3 (JSON–Code) and S4
(Traceability): a ✓ / ✗ per step (test.py has a ``# Step N`` block for it or not), its number,
its text; a pill counting the steps with code in the card's header, a note under the list.
Clicking a step emits ``step_clicked(n)``."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from procedo.ui import theme, ui_kit


@dataclass(frozen=True)
class StepRow:
    n: int
    text: str
    #: The code block for the step (a ``StepBlock``), None when test.py has none.
    block: object | None
    #: A Call step's function as its tooltip shows it (signature, declared units, doc line).
    hint: str = ""


def step_rows(
    json_steps: list, blocks_by_n: dict, procedure_text: str, call_hints: dict | None = None
) -> list[StepRow]:
    """One row per numbered JSON step: its text from procedure_text.md (op-call JSON steps carry
    no ``text``), else the step's own ``text``, else its op; its code block when there is one; a
    Call step's function from ``call_hints`` (Call name -> text) when it is there."""
    from ..pack_ops.pack_parsers import step_texts_from_canonical

    canonical = step_texts_from_canonical(procedure_text or "")
    rows = []
    for step in json_steps:
        if not isinstance(step, dict) or not isinstance(step.get("n"), int):
            continue
        n = step["n"]
        text = canonical.get(n) or step.get("text", "") or f"(no text — op={step.get('op', '?')})"
        function = step.get("function") if step.get("op") == "call" else None
        hint = (call_hints or {}).get(function, "") if isinstance(function, str) else ""
        rows.append(StepRow(n, text, blocks_by_n.get(n), hint))
    return rows


class StepListCard(QFrame):
    """The steps in a white card titled *title*."""

    step_clicked = Signal(int)

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("card")
        self.pill = QLabel()
        head = QHBoxLayout()
        head.setContentsMargins(12, 10, 12, 8)
        head.addWidget(ui_kit.bold(title))
        head.addStretch(1)
        head.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.tree = QTreeWidget()
        self.tree.setObjectName("stepTree")
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemClicked.connect(
            lambda item, _col: self.step_clicked.emit(item.data(0, Qt.ItemDataRole.UserRole))
        )
        self.note = ui_kit.note()
        column = QVBoxLayout(self)
        column.setContentsMargins(1, 0, 1, 1)  # inside the card's border
        column.setSpacing(0)
        column.addLayout(head)
        column.addWidget(self.tree, 1)
        column.addWidget(ui_kit.row_rule())
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 7, 12, 7)
        footer.addWidget(self.note)
        column.addLayout(footer)

    def show_steps(
        self, rows: list[StepRow], note: str, pill: tuple[str, str] | None = None
    ) -> None:
        """*rows* in the list (the selected step stays selected), *note* under it, *pill*
        (tone, text) in the header."""
        current = self.tree.currentItem()
        selected = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        self.tree.clear()
        ok, missing = QColor(theme.success_color()), QColor(theme.error_color())
        muted = QColor(theme.muted_color())
        bold = self.tree.font()
        bold.setBold(True)
        for row in rows:
            item = QTreeWidgetItem(["✓" if row.block else "✗", str(row.n), row.text])
            item.setData(0, Qt.ItemDataRole.UserRole, row.n)
            item.setForeground(0, ok if row.block else missing)
            item.setFont(0, bold)
            item.setForeground(1, muted)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if row.block is None:
                item.setForeground(2, missing)
            lines = (
                f"Code lines {row.block.start_line}-{row.block.end_line}"
                if row.block
                else "(no code block — regenerate test.py)"
            )
            tip = f"{row.text}\n\n{row.hint}\n\n{lines}" if row.hint else f"{row.text}\n\n{lines}"
            for column in range(3):
                item.setToolTip(column, tip)
            self.tree.addTopLevelItem(item)
        self.tree.setColumnWidth(0, 22)
        self.tree.setColumnWidth(1, self.tree.fontMetrics().horizontalAdvance("000") + 8)
        self.note.setText(note)
        if selected is not None:
            self.select(selected)
        self.pill.setVisible(pill is not None)
        if pill is not None:
            ui_kit.set_pill(self.pill, *pill)

    def rows(self) -> list[tuple[str, int, str]]:
        """What the list shows: (✓ or ✗, number, text) per step."""
        items = (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))
        return [(item.text(0), int(item.text(1)), item.text(2)) for item in items]

    def select(self, n: int) -> None:
        """Show step *n* as the selected one."""
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == n:
                self.tree.setCurrentItem(item)
                return
