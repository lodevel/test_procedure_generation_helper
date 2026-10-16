"""Help → About (board S12): Procedo Studio's mark and name, what it is, its version."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QPushButton

from procedo.core import ui_kit

from ..shell import mark_label

#: Procedo Studio's version, as About shows it.
VERSION = "0.1.0"
#: What Procedo Studio is, in one sentence.
DESCRIPTION = "Create and manage structured test procedures with LLM assistance."


class AboutDialog(QDialog):
    """The mark, "Procedo" bold and "Studio" in the mark's violet (#studioName), the one-line
    description and the version; OK."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Procedo Studio")
        ok_btn = ui_kit.primary(QPushButton("OK"))
        ok_btn.clicked.connect(self.accept)
        body = ui_kit.framed(self, ui_kit.HeaderBand("About"), ui_kit.FooterBand([ok_btn]),
                             margins=(20, 16, 20, 14), spacing=8)
        procedo = ui_kit.bold("Procedo", 1.25)
        studio = QLabel("Studio")
        studio.setObjectName("studioName")
        studio.setFont(procedo.font())  # the same size; the app QSS sets its weight and colour
        name = ui_kit.hbox(mark_label(32, self.devicePixelRatioF()), 4, procedo, studio, None)
        name.setSpacing(6)
        body.addLayout(name)
        body.addWidget(QLabel(DESCRIPTION))
        self.version = ui_kit.note(f"Version {VERSION}", wrap=False)
        body.addWidget(self.version)
        body.addStretch(1)
