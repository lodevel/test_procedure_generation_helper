"""Studio's "+ New shared phase…" (board StudioPanel F): the phase's name, its function (filled
from the name until edited), and its file — an existing one of ``functions/`` or a new one. A
function the file already has, or a name Python cannot import, is said under its field and
Create stays off."""

from __future__ import annotations

import keyword
import re

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from procedo.core.starter_test import phase_function
from procedo.ui import ui_kit

#: A function or a module name Python takes.
_NAME = re.compile(r"[A-Za-z_]\w*", re.ASCII)


def defined_functions(text: str) -> set[str]:
    """The functions a file's *text* defines at its top level (it need not parse)."""
    return set(re.findall(r"^(?:async\s+)?def\s+(\w+)", text, re.MULTILINE))


def _is_name(text: str) -> bool:
    return bool(_NAME.fullmatch(text)) and not keyword.iskeyword(text)


class NewPhaseDialog(QDialog):
    """The new phase: :attr:`name`, :attr:`function`, :attr:`file` (``x.py``) and whether that
    file is new (:attr:`is_new`). *files*: each existing file of ``functions/`` (``x.py``) and
    its text; *current*: the one chosen first."""

    def __init__(self, files: dict[str, str], current: str = "", parent=None) -> None:
        super().__init__(parent)
        self._defined = {name: defined_functions(text) for name, text in files.items()}
        self._followed = (
            ""  # the function as last filled from the name: edited by hand once it differs
        )
        self.setWindowTitle("New shared phase")
        self.setFixedWidth(560)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.create_button = ui_kit.primary(QPushButton("Create"))
        self.create_button.clicked.connect(self.accept)
        body = ui_kit.framed(
            self,
            ui_kit.HeaderBand("New shared phase", stacked=True),
            ui_kit.FooterBand([cancel, self.create_button]),
            margins=(24, 16, 24, 16),
        )
        self.name_edit = QLineEdit()
        self.function_edit = QLineEdit()
        self.function_note = ui_kit.note()
        self.function_problem = ui_kit.note()
        self.existing = QRadioButton("Existing")
        self.file_combo = QComboBox()
        self.file_combo.addItems([f"functions/{name}" for name in files])
        self.new = QRadioButton("New")
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("____")
        self.new_problem = ui_kit.note()
        group = QButtonGroup(self)
        group.addButton(self.existing)
        group.addButton(self.new)
        function = QVBoxLayout()
        function.setSpacing(4)
        function.addWidget(self.function_edit)
        function.addWidget(self.function_note)
        function.addWidget(self.function_problem)
        file = QVBoxLayout()
        file.setSpacing(6)
        file.addLayout(ui_kit.hbox(self.existing, self.file_combo))
        file.addLayout(
            ui_kit.hbox(self.new, ui_kit.mono(QLabel("functions/")), self.new_edit, QLabel(".py"))
        )
        file.addWidget(self.new_problem)
        grid = ui_kit.field_grid()
        ui_kit.add_field(grid, "Phase name", self.name_edit)
        ui_kit.add_field(grid, "Function", function)
        ui_kit.add_field(grid, "File", file)
        body.addLayout(grid)
        body.addStretch(1)
        self.existing.setEnabled(bool(files))
        self.file_combo.setEnabled(bool(files))
        if files:
            self.file_combo.setCurrentIndex(list(files).index(current) if current in files else 0)
        (self.existing if files else self.new).setChecked(True)
        self.name_edit.textChanged.connect(self._follow_name)
        for signal in (
            self.function_edit.textChanged,
            self.file_combo.currentIndexChanged,
            self.existing.toggled,
            self.new_edit.textChanged,
        ):
            signal.connect(self._check)
        self.new_edit.textEdited.connect(lambda: self.new.setChecked(True))
        self._check()

    def _follow_name(self) -> None:
        """The function follows the name, until it is edited by hand."""
        if self.function_edit.text() == self._followed:
            self._followed = phase_function(self.name)
            self.function_edit.setText(self._followed)
        self._check()

    @property
    def name(self) -> str:
        return self.name_edit.text().strip()

    @property
    def function(self) -> str:
        return self.function_edit.text().strip()

    @property
    def is_new(self) -> bool:
        return self.new.isChecked()

    @property
    def file(self) -> str:
        """``common_phases.py``: the file chosen ("" when a new one is not named yet)."""
        if self.is_new:
            stem = self.new_edit.text().strip().removesuffix(".py")
            return f"{stem}.py" if stem else ""
        return self.file_combo.currentText().removeprefix("functions/")

    def _check(self) -> None:
        file, function = self.file, self.function
        module = file.removesuffix(".py") or "____"
        self.function_note.setText(
            f"Filled from the name; what a test writes: from {module} import {function or '____'}"
        )
        if function and not _is_name(function):
            problem = "A function name is letters, digits and _, not starting with a digit."
        elif function in self._defined.get(file, ()):
            problem = f"{function} is already in {file}."
        else:
            problem = ""
        new_problem = ""
        if self.is_new and file:
            if not _is_name(module):
                new_problem = "A file name is letters, digits and _, not starting with a digit."
            elif file in self._defined:
                new_problem = f"functions/{file} already exists."
        for label, text in ((self.function_problem, problem), (self.new_problem, new_problem)):
            label.setText(text)
            label.setVisible(bool(text))
        self.create_button.setEnabled(
            bool(self.name and function and file) and not problem and not new_problem
        )
