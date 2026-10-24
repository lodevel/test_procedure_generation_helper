"""Help → Word Export Keywords (board S12): the Word-export template variable reference.

Single-sourced from the main app's canonical ``procedo/ui/help/word_export_keywords.md``
(the editor exports the SAME report, so the variables are identical).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QPushButton

from procedo.ui import ui_kit
from procedo.ui import help as help_pkg


def keywords_markdown() -> str:
    """The reference's Markdown, from the main app's help folder."""
    return (Path(help_pkg.__file__).parent / "word_export_keywords.md").read_text(encoding="utf-8")


class WordKeywordsDialog(QDialog):
    """The reference on a white card under the header; Close."""

    def __init__(self, markdown: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Word Export Keywords")
        self.resize(760, 600)
        close_btn = ui_kit.primary(QPushButton("Close"))
        close_btn.clicked.connect(self.accept)
        body = ui_kit.framed(self, ui_kit.HeaderBand("Word export keywords"),
                             ui_kit.FooterBand([close_btn]))
        self.page = ui_kit.MarkdownView(markdown)
        body.addWidget(self.page, 1)


def show_word_keywords(parent) -> None:
    """Open the reference over *parent*."""
    WordKeywordsDialog(keywords_markdown(), parent).exec()
