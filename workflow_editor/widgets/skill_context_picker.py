"""Skill context picker — a 3-tab checkable browser for the skill-chat window.

One tab per :class:`ContextSource` (Rules / Documents / Artifacts); each tab is a
scrollable list of checkboxes over ``source.list_items()``. The picker is a thin
view: it owns no context content, it merely renders the items the pure data layer
(:mod:`workflow_editor.authoring`) hands it and reports the checked selection back
via :meth:`selections`. A live readout shows the assembled payload size.

The embedding window reads :meth:`selections` to push context and may mirror its
own state with :meth:`set_checked` (e.g. pre-tick the rules already chosen in the
text tab). Nothing here imports a manager — sources carry their own dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, QPushButton, QLabel,
    QScrollArea, QTabWidget,
)

from .. import theme
from ..authoring import ContextSource, assemble


class _SourceTab(QWidget):
    """One tab: a scrollable column of checkboxes over a single source's items.

    Owns the checkbox-per-key mapping for its source and can rebuild it on
    demand (a source's items can change, e.g. after dropping a file into the
    documents folder). Toggling any checkbox emits :attr:`toggled`."""

    toggled = Signal()

    def __init__(self, source: ContextSource, parent=None) -> None:
        super().__init__(parent)
        self._source = source
        self._checks: dict[str, QCheckBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._toolbar = QHBoxLayout()
        outer.addLayout(self._toolbar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._list = QVBoxLayout(self._container)
        self._list.setContentsMargins(6, 6, 6, 6)
        self._list.setSpacing(4)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._reload_items()

    @property
    def source(self) -> ContextSource:
        return self._source

    def add_toolbar_button(self, text: str, on_click) -> QPushButton:
        """Add a button to this tab's toolbar (used for Open folder / Refresh)."""
        btn = QPushButton(text)
        btn.clicked.connect(on_click)
        self._toolbar.addWidget(btn)
        return btn

    def selected_keys(self) -> list[str]:
        """Keys of the currently-checked items, in list order."""
        return [key for key, cb in self._checks.items() if cb.isChecked()]

    def set_checked(self, keys: list[str]) -> None:
        """Tick exactly ``keys`` (others unticked); unknown keys are ignored."""
        wanted = set(keys or ())
        for key, cb in self._checks.items():
            cb.setChecked(key in wanted)

    def refresh(self) -> None:
        """Re-read the source's items, preserving the current selection where
        the same keys still exist."""
        keep = set(self.selected_keys())
        self._reload_items()
        self.set_checked([k for k in self._checks if k in keep])

    # -- internals ------------------------------------------------------------

    def _reload_items(self) -> None:
        for cb in self._checks.values():
            cb.deleteLater()
        self._checks.clear()
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        items = self._source.list_items()
        if not items:
            empty = QLabel("No items available.")
            empty.setStyleSheet(
                f"color:{theme.disabled_text()}; font-style:italic;")
            self._list.addWidget(empty)
            self._list.addStretch(1)
            return

        for item in items:
            cb = QCheckBox(item.label)
            if item.detail:
                cb.setText(f"{item.label}   —   {item.detail}")
                cb.setStyleSheet(self._detail_style())
            cb.toggled.connect(self.toggled)
            self._list.addWidget(cb)
            self._checks[item.key] = cb
        self._list.addStretch(1)

    @staticmethod
    def _detail_style() -> str:
        # Item label normal, trailing detail greyed via a softer overall tint.
        return f"QCheckBox {{ color:{theme.muted_color()}; }}"


class SkillContextPicker(QWidget):
    """3-tab context picker for the skill-chat window.

    Renders one tab per source, exposes the checked selection, and shows a live
    "Context: N items, ~M tokens" readout (recomputed through
    :func:`assemble`). The widget is purely a view over the injected sources.
    """

    selectionChanged = Signal()

    def __init__(
        self,
        sources: list[ContextSource],
        documents_dir: Optional[Path] = None,
        parent=None,
    ) -> None:
        """
        Args:
            sources: Context sources, one tab each (label = ``source.title``).
            documents_dir: If given, the documents tab gains *Open folder* /
                *Refresh* buttons over this directory. Matched against the
                source whose ``source_id`` is ``"documents"``.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._documents_dir = Path(documents_dir) if documents_dir else None
        self._tabs_by_source: dict[str, _SourceTab] = {}

        outer = QVBoxLayout(self)

        self._tabs = QTabWidget()
        for source in sources:
            tab = _SourceTab(source)
            tab.toggled.connect(self._on_selection_changed)
            self._tabs.addTab(tab, source.title)
            self._tabs_by_source[source.source_id] = tab
            if self._documents_dir and source.source_id == "documents":
                self._add_documents_controls(tab)
        outer.addWidget(self._tabs, 1)

        self._readout = QLabel()
        self._readout.setStyleSheet(
            f"color:{theme.muted_color()}; font-size:9pt;")
        outer.addWidget(self._readout)

        self._update_readout()

    # -- public API -----------------------------------------------------------

    def selections(self) -> list[tuple[ContextSource, list[str]]]:
        """Each source paired with its checked item keys (in tab order)."""
        return [
            (tab.source, tab.selected_keys())
            for tab in self._iter_tabs()
        ]

    def set_checked(self, source_id: str, keys: list[str]) -> None:
        """Pre-tick ``keys`` in the source identified by ``source_id``.

        Used to mirror external state (e.g. the text tab's rules selection).
        No-op for an unknown ``source_id``."""
        tab = self._tabs_by_source.get(source_id)
        if tab is not None:
            tab.set_checked(keys)
            self._update_readout()

    # -- internals ------------------------------------------------------------

    def _add_documents_controls(self, tab: _SourceTab) -> None:
        tab.add_toolbar_button("Open folder", self._open_documents_dir)
        tab.add_toolbar_button("Refresh", lambda: self._refresh_tab(tab))

    def _open_documents_dir(self) -> None:
        if self._documents_dir is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._documents_dir)))

    def _refresh_tab(self, tab: _SourceTab) -> None:
        tab.refresh()
        self._update_readout()

    def _on_selection_changed(self) -> None:
        self._update_readout()
        self.selectionChanged.emit()

    def _update_readout(self) -> None:
        selections = self.selections()
        item_count = sum(len(keys) for _, keys in selections)
        tokens = assemble(selections).approx_tokens
        self._readout.setText(
            f"Context: {item_count} items, ~{tokens:,} tokens (est.)")

    def _iter_tabs(self):
        for i in range(self._tabs.count()):
            yield self._tabs.widget(i)
