"""Help → Word Export Keywords (board S12): the Word-export template variable reference.

Single-sourced from the main app's canonical ``test_procedure_gui/help/word_export_keywords.md``
(the editor exports the SAME report, so the variables are identical). Resolved via the
installed package first, then the monorepo-relative path; bare-editor installs without the
main app get a pointer instead of a stale duplicated copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton

from project_services import ui_kit


def keywords_markdown() -> Optional[str]:
    """The reference's Markdown, or None when the main app's copy is not found."""
    md = None
    try:
        from test_procedure_gui import help as _help_pkg  # type: ignore
        _p = Path(_help_pkg.__file__).parent / "word_export_keywords.md"
        md = _p.read_text(encoding="utf-8") if _p.exists() else None
    except Exception:
        md = None
    if md is None:
        _p = (Path(__file__).resolve().parents[4]
              / "src" / "test_procedure_gui" / "help"
              / "word_export_keywords.md")
        try:
            md = _p.read_text(encoding="utf-8") if _p.exists() else None
        except Exception:
            md = None
    return md


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
    """Open the reference over *parent*, or say where it is when this install has none."""
    md = keywords_markdown()
    if md is None:
        QMessageBox.information(
            parent, "Word Export Keywords",
            "The template-variable reference ships with the main "
            "application: open it there via Help → Documentation (F1) "
            "→ Word Export Keywords.")
        return
    WordKeywordsDialog(md, parent).exec()
