"""DSL Syntax Reference — cheat-sheet + full bundle rule docs (board S8).

A non-modal singleton window the operator keeps open beside the Text editor
while authoring. The header names the active bundle and holds *Find in page*;
the page list on the left has the auto-extracted **Cheat sheet** (landing page)
plus every full rule doc from the active bundle; the page on the right renders
the selected page's Markdown on a white card (``ui_kit.MarkdownView``).

Optional feature: with no project / no installed bundle it shows a friendly
placeholder, never an error.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton

from project_services import ui_kit
from project_services.page_nav import group_heading_item

from ..core import cheatsheet

_ROLE_CONTENT = Qt.ItemDataRole.UserRole
#: The page list's width (the board's).
_LIST_WIDTH = 270
#: The reading size of the Markdown page, in points (at the least).
_PAGE_POINTS = 11


class SyntaxReferenceDialog(QDialog):
    """Non-modal singleton. Use :meth:`show_reference` / :meth:`refresh_if_open`."""

    _instance: Optional["SyntaxReferenceDialog"] = None

    @classmethod
    def show_reference(cls, rules_root, parent=None, bundle: str = "") -> "SyntaxReferenceDialog":
        """Open (or raise) the window on *rules_root*; *bundle* names the active bundle
        (``shell.bundle_name``) in the header."""
        inst = cls._instance
        if inst is None:
            inst = cls(parent)
            cls._instance = inst
        inst.set_rules_root(rules_root, bundle)
        inst.show()
        inst.raise_()
        inst.activateWindow()
        return inst

    @classmethod
    def refresh_if_open(cls, rules_root, bundle: str = "") -> None:
        """Re-read the bundle into the window if it's currently open (e.g. after
        the project / rules_root changes). No-op when closed."""
        inst = cls._instance
        if inst is None:
            return
        try:
            if inst.isVisible():
                inst.set_rules_root(rules_root, bundle)
        except RuntimeError:        # underlying C++ dialog already gone
            cls._instance = None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DSL Syntax Reference")
        # A real top-level window (own taskbar entry), non-modal so it can sit
        # beside the editor.
        self.setWindowFlags(Qt.WindowType.Window)
        self.setModal(False)
        self.resize(1240, 820)
        self._build_ui()

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._search = ui_kit.EntryField()  # Enter finds once (not the Next button too)
        self._search.setPlaceholderText("Find in page…  (Enter)")
        self._search.setMinimumWidth(300)
        self._search.returnPressed.connect(self._on_find)
        next_btn = QPushButton("Next")
        next_btn.clicked.connect(self._on_find)
        self._header = ui_kit.HeaderBand("DSL Syntax Reference", "", [self._search, next_btn],
                                         stacked=True)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        body = ui_kit.framed(self, self._header, ui_kit.FooterBand([close_btn]),
                             margins=(0, 0, 0, 0), spacing=0)

        self._pages = QListWidget()
        self._pages.setObjectName("pageList")  # the app QSS's page list (Settings, …)
        self._pages.setFixedWidth(_LIST_WIDTH)
        self._pages.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pages.setTextElideMode(Qt.TextElideMode.ElideRight)  # the full title is the tooltip
        self._pages.currentItemChanged.connect(self._on_nav)
        self._browser = ui_kit.MarkdownView()
        self._browser.setOpenExternalLinks(False)
        font = self._browser.font()
        font.setPointSize(max(_PAGE_POINTS, font.pointSize()))
        self._browser.setFont(font)
        row = QHBoxLayout()
        row.setSpacing(0)
        row.addWidget(self._pages)
        page = QHBoxLayout()
        page.setContentsMargins(18, 16, 18, 16)
        page.addWidget(self._browser)
        row.addLayout(page, 1)
        body.addLayout(row, 1)

    # -- content --------------------------------------------------------------

    def set_rules_root(self, rules_root, bundle: str = "") -> None:
        self._pages.clear()
        self._header.subtitle.setText(f"bundle {bundle} · F1" if bundle else "F1")
        sheet = cheatsheet.build_cheatsheet(rules_root)
        docs = cheatsheet.list_docs(rules_root)

        if not sheet and not docs:
            self._browser.setMarkdown(
                "# No bundle rules found\n\nOpen a project with an installed "
                "bundle to see its procedure-DSL syntax reference.\n\n"
                "_The reference is rendered from the active bundle's rule docs._")
            return

        if sheet:
            self._add_page("Cheat sheet", sheet)
        if docs:
            self._pages.addItem(group_heading_item("Full rules"))
            for doc in docs:
                tip = doc["filename"]
                if doc.get("source"):
                    tip += f"  ({doc['source']})"
                self._add_page(doc["title"], doc["text"], tip)

        self._pages.setCurrentRow(0 if sheet else 1)   # the cheat sheet is the landing page

    def _add_page(self, title: str, content: str, tip: str = "") -> None:
        item = QListWidgetItem(title)
        item.setData(_ROLE_CONTENT, content)
        item.setToolTip(tip)
        self._pages.addItem(item)

    def _on_nav(self, current, _previous) -> None:
        if current is None:
            return
        content = current.data(_ROLE_CONTENT)
        if content:
            self._browser.setMarkdown(str(content))

    def _on_find(self) -> None:
        text = self._search.text().strip()
        if not text:
            return
        if not self._browser.find(text):                 # wrap to top and retry
            cur = self._browser.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            self._browser.setTextCursor(cur)
            self._browser.find(text)

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override
        type(self)._instance = None
        super().closeEvent(event)
