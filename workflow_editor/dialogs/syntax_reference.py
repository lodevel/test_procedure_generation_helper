"""DSL Syntax Reference — cheat-sheet + full bundle rule docs.

A non-modal singleton window the operator keeps open beside the Text editor
while authoring. The left tree has the auto-extracted **Cheat sheet** (landing
page) plus every full rule doc from the active bundle; the right pane renders
the selected page's Markdown (``QTextBrowser.setMarkdown`` — GitHub dialect,
tables; zero extra deps).

Optional feature: with no project / no installed bundle it shows a friendly
placeholder, never an error.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QSplitter, QTreeWidget, QTreeWidgetItem,
    QTextBrowser, QLineEdit,
)

from ..core import cheatsheet

_ROLE_CONTENT = Qt.ItemDataRole.UserRole


class SyntaxReferenceDialog(QDialog):
    """Non-modal singleton. Use :meth:`show_reference` / :meth:`refresh_if_open`."""

    _instance: Optional["SyntaxReferenceDialog"] = None

    @classmethod
    def show_reference(cls, rules_root, parent=None) -> "SyntaxReferenceDialog":
        inst = cls._instance
        if inst is None:
            inst = cls(parent)
            cls._instance = inst
        inst.set_rules_root(rules_root)
        inst.show()
        inst.raise_()
        inst.activateWindow()
        return inst

    @classmethod
    def refresh_if_open(cls, rules_root) -> None:
        """Re-read the bundle into the window if it's currently open (e.g. after
        the project / rules_root changes). No-op when closed."""
        inst = cls._instance
        if inst is None:
            return
        try:
            if inst.isVisible():
                inst.set_rules_root(rules_root)
        except RuntimeError:        # underlying C++ dialog already gone
            cls._instance = None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DSL Syntax Reference")
        # A real top-level window (own taskbar entry), non-modal so it can sit
        # beside the editor.
        self.setWindowFlags(Qt.WindowType.Window)
        self.setModal(False)
        self.resize(940, 720)
        self._build_ui()

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Find in page…  (Enter)")
        self._search.returnPressed.connect(self._on_find)
        root.addWidget(self._search)

        split = QSplitter(Qt.Orientation.Horizontal)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.currentItemChanged.connect(self._on_nav)
        split.addWidget(self._tree)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        font = self._browser.font()
        font.setPointSize(max(11, font.pointSize()))
        self._browser.setFont(font)
        split.addWidget(self._browser)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([280, 660])
        root.addWidget(split, 1)

    # -- content --------------------------------------------------------------

    def set_rules_root(self, rules_root) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        sheet = cheatsheet.build_cheatsheet(rules_root)
        docs = cheatsheet.list_docs(rules_root)
        self._tree.blockSignals(False)

        if not sheet and not docs:
            self._browser.setMarkdown(
                "# No bundle rules found\n\nOpen a project with an installed "
                "bundle to see its procedure-DSL syntax reference.\n\n"
                "_The reference is rendered from the active bundle's rule docs._")
            return

        if sheet:
            item = QTreeWidgetItem(["\U0001F4CB  Cheat sheet"])
            item.setData(0, _ROLE_CONTENT, sheet)
            self._tree.addTopLevelItem(item)

        if docs:
            group = QTreeWidgetItem(["Full rules"])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._tree.addTopLevelItem(group)
            for doc in docs:
                child = QTreeWidgetItem([doc["title"]])
                child.setData(0, _ROLE_CONTENT, doc["text"])
                tip = doc["filename"]
                if doc.get("source"):
                    tip += f"  ({doc['source']})"
                child.setToolTip(0, tip)
                group.addChild(child)
            group.setExpanded(True)

        first = self._tree.topLevelItem(0)
        if first is not None:
            self._tree.setCurrentItem(first)   # cheat-sheet is the landing page

    def _on_nav(self, current, _previous) -> None:
        if current is None:
            return
        content = current.data(0, _ROLE_CONTENT)
        if content:
            self._browser.setMarkdown(str(content))
            self._browser.moveCursor(QTextCursor.MoveOperation.Start)

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
