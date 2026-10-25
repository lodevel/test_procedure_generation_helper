"""Reusable test-folder selection checklist for the editor's exports (board S12, Export procedures).

Both the full Word report and the multi-test Markdown export run over the SAME
selection (mirroring the main app's checked-procedure list). Rows lacking the
required artifact (``procedure.json`` for Word, ``procedure_text.md`` for Markdown)
are shown greyed/disabled; each row names the test's artifacts on its right.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QPushButton

from procedo.ui import ui_kit

#: What the selection exports, by the artifact it requires (the window's subtitle).
_EXPORTS = {"json": "Procedure to Word", "text": "Procedure to Markdown"}


class TestSelectionDialog(QDialog):
    """Pick which test folders to export. ``require`` is ``"json"`` or ``"text"`` —
    folders missing that artifact are disabled."""

    def __init__(self, folders, require: str = "json", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export procedures")
        self.resize(430, 520)
        self._folders = {f.name: f for f in folders}

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        export_btn = ui_kit.primary(QPushButton("Export"))
        export_btn.clicked.connect(self.accept)
        footer = ui_kit.FooterBand([cancel_btn, export_btn])
        self._count = footer.note
        body = ui_kit.framed(
            self,
            ui_kit.HeaderBand("Export procedures", _EXPORTS.get(require, ""), stacked=True),
            footer,
            margins=(16, 12, 16, 12),
            spacing=8,
        )

        all_btn = QPushButton("All")
        all_btn.clicked.connect(lambda: self.tests.set_all(True))
        none_btn = QPushButton("None")
        none_btn.clicked.connect(lambda: self.tests.set_all(False))
        body.addLayout(
            ui_kit.hbox(
                ui_kit.note("Tick the tests to include.", wrap=False), None, all_btn, none_btn
            )
        )
        self.tests = ui_kit.CheckList()
        for f in folders:
            available = bool(getattr(f, f"has_{require}", False))
            detail = f.artifact_state if available else f"{f.artifact_state} — no {require}"
            self.tests.add(f.name, f.name, detail, checked=available, enabled=available)
        self.tests.toggled.connect(self._update_count)
        body.addWidget(self.tests, 1)
        self._update_count()

    def _update_count(self) -> None:
        self._count.setText(f"{len(self.tests.checked())} of {len(self.tests.keys())}")

    def selected_folders(self) -> list:
        """The checked ``TestFolderInfo`` rows."""
        return [self._folders[name] for name in self.tests.checked()]
