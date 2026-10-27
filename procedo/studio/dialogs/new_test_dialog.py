"""Studio's "+ New test…" (board StudioNew): the test folder's name and what it is written as —
a text procedure (today's format) or Python (OpenHTF), started from the code starter."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from procedo.core.test_format import CODE, TEXT
from procedo.ui import ui_kit

#: The two ways a test is written: its format, the option's label and what it says.
FORMATS = (
    (
        TEXT,
        "Text procedure",
        "procedure_text.md — today's format, guided run, media, Sync Equipment.",
    ),
    (
        CODE,
        "Python (OpenHTF)",
        "openhtf/test.py from a starter: one step, one measurement, one instrument, one sub-step.",
    ),
)


class _Option(QFrame):
    """One format: its radio button over what it says, in a bordered box (accent when chosen);
    a click anywhere in it chooses it."""

    def __init__(self, label: str, about: str) -> None:
        super().__init__()
        self.setObjectName("formatOption")
        self.radio = QRadioButton(label)
        self.radio.toggled.connect(lambda on: ui_kit.set_state(self, "selected", on))
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(6)
        column.addWidget(self.radio)
        column.addWidget(ui_kit.note(about))
        ui_kit.set_state(self, "selected", False)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        self.radio.setChecked(True)
        super().mousePressEvent(event)


class NewTestDialog(QDialog):
    """The new test's folder name (:attr:`name`) and format (:attr:`active`)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New test")
        self.setFixedWidth(560)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.create_button = ui_kit.primary(QPushButton("Create"))
        self.create_button.clicked.connect(self.accept)
        body = ui_kit.framed(
            self,
            ui_kit.HeaderBand("New test", stacked=True),
            ui_kit.FooterBand([cancel, self.create_button]),
            margins=(24, 16, 24, 16),
            spacing=10,
        )
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(
            lambda text: self.create_button.setEnabled(bool(text.strip()))
        )
        body.addWidget(QLabel("Test folder name"))
        body.addWidget(self.name_edit)
        body.addWidget(QLabel("Written as"))
        self.options = {active: _Option(label, about) for active, label, about in FORMATS}
        group = QButtonGroup(self)
        row = ui_kit.hbox()
        row.setSpacing(12)
        for option in self.options.values():
            group.addButton(option.radio)
            row.addWidget(option, 1)
        body.addLayout(row)
        self.options[TEXT].radio.setChecked(True)
        self.create_button.setEnabled(False)
        self.setMinimumHeight(self.heightForWidth(560))  # the formats' wrapped lines in full

    @property
    def name(self) -> str:
        return self.name_edit.text().strip()

    @property
    def active(self) -> str:
        """:data:`TEXT` or :data:`CODE`: the format chosen."""
        return next(active for active, option in self.options.items() if option.radio.isChecked())
