"""Skill context picker — the skill chat's "Context to push" card (board S9).

A white card titled *Context to push* (Select all / Deselect all on the right, acting on the
visible tab), one underlined tab per :class:`ContextSource` (Rules / Documents / Artifacts,
each named with its ticked count), each tab a check list over ``source.list_items()``, and a
live readout of the assembled payload's size at the bottom. The picker is a thin view: it owns
no context content, it renders the items the pure data layer (:mod:`procedo.studio.authoring`)
hands it and reports the ticked selection back via :meth:`selections`.

The embedding window reads :meth:`selections` to push context and may mirror its own state
with :meth:`set_checked` (e.g. pre-tick the rules already chosen in the text tab). Nothing here
imports a manager — sources carry their own dependencies.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QPushButton, QTabWidget, QVBoxLayout, QWidget

from procedo.core import ui_kit

from ..authoring import ContextSource, assemble

log = logging.getLogger(__name__)


class _SourceTab(QWidget):
    """One tab: a check list over a single source's items (a toolbar above it when a host
    adds buttons). It can rebuild the list on demand (a source's items can change, e.g. after
    dropping a file into the documents folder)."""

    def __init__(self, source: ContextSource, parent=None) -> None:
        super().__init__(parent)
        self._source = source
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._toolbar = ui_kit.hbox(None)  # empty (no room taken) until a host adds a button
        column.addLayout(self._toolbar)
        self.items = ui_kit.CheckList(mono=True, flush=True)
        column.addWidget(self.items, 1)
        self._reload_items()

    @property
    def source(self) -> ContextSource:
        return self._source

    def add_toolbar_button(self, text: str, on_click) -> QPushButton:
        """Add a button to this tab's toolbar (used for Open folder / Refresh)."""
        btn = QPushButton(text)
        btn.clicked.connect(on_click)
        self._toolbar.setContentsMargins(12, 8, 12, 8)
        self._toolbar.insertWidget(self._toolbar.count() - 1, btn)  # before the stretch
        return btn

    def refresh(self) -> None:
        """Re-read the source's items, keeping the ticks of the keys that still exist."""
        keep = self.items.checked()
        self._reload_items()
        self.items.set_checked(keep)

    def _reload_items(self) -> None:
        self.items.clear()
        items = self._source.list_items()
        for item in items:
            self.items.add(item.key, item.label, item.detail or "")
        if not items:
            self.items.add_message("No items available.")


class SkillContextPicker(ui_kit.Card):
    """The "Context to push" card: one tab per source, the ticked selection, and a live
    "Context: N items · ~M tokens (est.)" readout (recomputed through :func:`assemble`)."""

    selectionChanged = Signal()
    # (generation, readout text) from the worker that sized the selection.
    _readout_ready = Signal(int, str)

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
        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: self._current_tab().items.set_all(True))
        deselect_all_btn = QPushButton("Deselect all")
        deselect_all_btn.clicked.connect(lambda: self._current_tab().items.set_all(False))
        super().__init__("Context to push", actions=[select_all_btn, deselect_all_btn])
        if parent is not None:
            self.setParent(parent)
        # The tabs and the rows run edge to edge; the title and the readout keep the inset.
        self.layout().setContentsMargins(0, 10, 0, 10)
        self.head_row.setContentsMargins(14, 0, 12, 0)
        self._documents_dir = Path(documents_dir) if documents_dir else None
        self._tabs_by_source: dict[str, _SourceTab] = {}

        self._tabs = QTabWidget()
        self._tabs.setObjectName("contextTabs")
        self._tabs.setProperty("underline", True)  # the app QSS: underlined tabs
        for source in sources:
            tab = _SourceTab(source)
            tab.items.toggled.connect(self._on_selection_changed)
            self._tabs.addTab(tab, source.title)
            self._tabs_by_source[source.source_id] = tab
            if self._documents_dir and source.source_id == "documents":
                tab.add_toolbar_button("Open folder", self._open_documents_dir)
                tab.add_toolbar_button("Refresh", lambda _=False, t=tab: self._refresh_tab(t))
        self.body.addWidget(self._tabs, 1)

        self._readout = ui_kit.note(wrap=False)
        self._readout.setContentsMargins(12, 0, 12, 0)
        self.body.addWidget(self._readout)

        # Debounce the readout: it re-materializes the payload (assemble) to
        # size it, which can be costly; coalesce rapid toggles into one update.
        self._readout_timer = QTimer(self)
        self._readout_timer.setSingleShot(True)
        self._readout_timer.setInterval(150)
        self._readout_timer.timeout.connect(self._update_readout)
        self._readout_generation = 0
        self._sizing = self.loading.hold("Sizing the context\u2026")  # the title row's cue
        self._readout_ready.connect(self._show_readout)

        self._update_readout()

    # -- public API -----------------------------------------------------------

    def selections(self) -> list[tuple[ContextSource, list[str]]]:
        """Each source paired with its checked item keys (in tab order)."""
        return [(tab.source, tab.items.checked()) for tab in self._iter_tabs()]

    def set_checked(self, source_id: str, keys: list[str]) -> None:
        """Pre-tick ``keys`` in the source identified by ``source_id``.

        Used to mirror external state (e.g. the text tab's rules selection).
        No-op for an unknown ``source_id``."""
        tab = self._tabs_by_source.get(source_id)
        if tab is not None:
            tab.items.set_checked(keys or ())
            self._readout_timer.start()

    def tick(self, keys) -> None:
        """Tick ``keys`` in whichever tab offers them, keeping the ticks already made (a
        skill's ``needs-context`` defaults)."""
        wanted = set(keys)
        for tab in self._iter_tabs():
            tab.items.set_checked(set(tab.items.checked()) | (wanted & set(tab.items.keys())))
        self._readout_timer.start()

    def unmet(self, keys) -> str:
        """Why the items ``keys`` cannot all ride the context — one not offered, unticked or
        empty — in a sentence for the user; ``""`` when they can."""
        for key in keys:
            tab = next((t for t in self._iter_tabs() if key in t.items.keys()), None)
            if tab is None:
                return f"This skill needs '{key}', which this project does not offer."
            item = next((i for i in tab.source.list_items() if i.key == key), None)
            label = item.label if item else key
            if key not in tab.items.checked():
                return (f"This skill needs '{label}': tick it under Context to push "
                        f"→ {tab.source.title}.")
            if not tab.source.materialize([key]).strip():
                why = f" ({item.empty_hint})" if item and item.empty_hint else ""
                return f"This skill needs '{label}', but it is empty for this project{why}."
        return ""

    # -- internals ------------------------------------------------------------

    def _current_tab(self) -> _SourceTab:
        return self._tabs.currentWidget()

    def _open_documents_dir(self) -> None:
        if self._documents_dir is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._documents_dir)))

    def _refresh_tab(self, tab: _SourceTab) -> None:
        tab.refresh()
        self._readout_timer.start()

    def _on_selection_changed(self) -> None:
        self._readout_timer.start()   # debounced (assemble can be costly)
        self.selectionChanged.emit()

    def _update_readout(self) -> None:
        """Each tab's ticked count ("Rules 2") now, and the readout once a worker thread has
        sized the selection: materializing it can load the board (an ODB CLI run, ~0.5 s and
        more), which must not freeze the window. The card's cue until then; only the latest
        sizing is shown."""
        selections = self.selections()
        for i, (source, keys) in enumerate(selections):
            self._tabs.setTabText(i, f"{source.title} {len(keys)}")
        count = sum(len(keys) for _, keys in selections)
        self._readout_generation += 1
        generation = self._readout_generation
        if not count:
            self._sizing.set(False)
            self._readout.setText("Context: 0 items · ~0 tokens (est.)")
            return
        self._readout.setText(f"Context: {count} items")
        self._sizing.set(True)

        def size() -> None:
            try:
                tokens = assemble(selections).approx_tokens
                text = f"Context: {count} items · ~{tokens:,} tokens (est.)"
            except Exception:  # noqa: BLE001 — a failing source must not kill the readout
                log.exception("context picker: could not size the selection")
                text = f"Context: {count} items"
            try:
                self._readout_ready.emit(generation, text)
            except RuntimeError:  # the picker was deleted meanwhile
                pass

        threading.Thread(target=size, name="context-readout", daemon=True).start()

    def _show_readout(self, generation: int, text: str) -> None:
        if generation == self._readout_generation:
            self._sizing.set(False)
            self._readout.setText(text)

    def _iter_tabs(self):
        for i in range(self._tabs.count()):
            yield self._tabs.widget(i)
