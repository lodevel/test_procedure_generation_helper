"""Board netlist reference card for the Text tab (board S1).

Split vertically: a tabbed **browser** on top (Components | Nets, underlined) and
an inline **board-image viewer** below — a read-only reference while authoring. **Select**
a component or pin to render its board image into the viewer (cached or generated
on demand; nets are names-only, no image); click the image for a full view.

Auto-generated net names (default ``Net*``, per-project configurable) are hidden
unless the *Show auto nets* toggle is on. Empty-and-friendly with no ODB++
archive (never an error).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThread, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QLineEdit,
    QTabWidget,
    QTreeWidget,
    QCheckBox,
    QSplitter,
    QApplication,
)

from procedo.core import board_view, odb_archive
from procedo.ui import ui_kit
from procedo.core.board_netlist import filter_components, filter_nets, read_hide_prefixes
from procedo.ui.board_viewer import (
    ROLE_COPY,
    BoardImageWell,
    component_item,
    net_item,
    selected_target,
)

from ..core import odb_inspect

# Keep worker threads referenced until they finish, so a fast close doesn't
# destroy a running QThread. Connections to the panel auto-disconnect if it dies.
_LIVE_WORKERS: set = set()


def _caption(refdes: str, pad: str) -> str:
    return refdes + (f" pin {pad}" if pad else "")


class _LoadWorker(QThread):
    """Loads {components, nets} off the UI thread; never raises."""

    done = Signal(dict)

    def __init__(self, project_root):
        super().__init__()
        self._root = project_root

    def run(self) -> None:  # noqa: D401
        try:
            board_view.rotation(self._root)  # the board's angle, read here: off the UI thread
            data = odb_inspect.load_board(self._root)
        except Exception:  # noqa: BLE001
            data = {"components": [], "nets": []}
        self.done.emit(data)


class _RenderWorker(QThread):
    """Renders one component/pin image off the UI thread; never raises. Emits
    (zoomed_path, wide_path, refdes, pad) — empty strings when not rendered."""

    done = Signal(str, str, str, str)

    def __init__(self, project_root, refdes, pad):
        super().__init__()
        self._root, self._refdes, self._pad = project_root, refdes, pad

    def run(self) -> None:  # noqa: D401
        try:
            z, w = odb_inspect.render_target(self._root, self._refdes, self._pad)
        except Exception:  # noqa: BLE001
            z, w = None, None
        self.done.emit(str(z) if z else "", str(w) if w else "", self._refdes, self._pad or "")


class NetlistPanel(ui_kit.Card):
    """Components | Nets browser over an inline board-image viewer, in a white card titled
    "Board netlist · <the project's archive>"."""

    def __init__(self, parent=None) -> None:
        super().__init__("Board netlist")
        if parent is not None:
            self.setParent(parent)
        self._components: list = []
        self._nets: list = []
        self._hide_prefixes: list = ["Net"]
        self._project_root = None
        self._load_worker = None
        self._rendering = False
        self._render_pending = None  # (refdes, pad) to render after current
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.timeout.connect(self._render_selection)
        self._build_ui()
        self._board_read = self.loading.hold("Loading board\u2026")  # the title row's cue
        self.set_board([], [])

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.body.setSpacing(8)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setHandleWidth(8)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter components / pins / nets…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._refilter)
        tl.addWidget(self._search)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("netlistTabs")  # underlined, on the card's white (app QSS)
        self._tabs.setProperty("underline", True)
        self._comp_tree = QTreeWidget()
        self._comp_tree.setHeaderLabels(["Component / pin", "Net"])
        self._comp_tree.setColumnWidth(0, 177)  # Component/pin wider; Net (stretch col) shrinks
        self._comp_tree.setUniformRowHeights(True)
        self._comp_tree.itemSelectionChanged.connect(self._on_sel_changed)
        self._comp_tree.itemDoubleClicked.connect(self._on_double_click)
        self._net_tree = QTreeWidget()
        self._net_tree.setHeaderLabels(["Net / node", ""])
        self._net_tree.setColumnWidth(0, 190)
        self._net_tree.setUniformRowHeights(True)
        self._net_tree.itemSelectionChanged.connect(self._on_sel_changed)
        self._net_tree.itemDoubleClicked.connect(self._on_double_click)
        for tree in (self._comp_tree, self._net_tree):
            tree.setObjectName("netlistTree")
        self._tabs.addTab(self._comp_tree, "Components")
        self._tabs.addTab(self._net_tree, "Nets")
        self._tabs.currentChanged.connect(self._on_sel_changed)
        tl.addWidget(self._tabs, 1)

        row = QHBoxLayout()
        self._show_hidden = QCheckBox("Show auto nets (Net*)")
        self._show_hidden.setToolTip(
            "Show auto-generated net names (e.g. NetD16_A) hidden by default. "
            "Configure the hidden prefixes in the project settings."
        )
        self._show_hidden.toggled.connect(self._refilter)
        row.addWidget(self._show_hidden)
        self._status = ui_kit.note()
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._status, 1)
        tl.addLayout(row)
        tl.addWidget(
            ui_kit.note("Double-click a row to copy its name · select it to see it on the board.")
        )
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        caption = ui_kit.monospace(ui_kit.note(wrap=False))
        self._viewer = BoardImageWell(caption, fit_height=True)
        self._viewer.setObjectName("boardImage")  # a framed, rounded well (app QSS)
        full_view = ui_kit.link("Full view…", self._viewer.open_full_view)
        full_view.setEnabled(False)
        self._viewer.image_shown.connect(full_view.setEnabled)
        bl.addLayout(ui_kit.hbox(ui_kit.bold("Board image"), caption, None, full_view))
        bl.addWidget(self._viewer, 1)
        split.addWidget(bottom)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 320])
        self.body.addWidget(split, 1)

    # -- data -----------------------------------------------------------------

    def load(self, project_root) -> None:
        """Load the project's netlist off-thread and populate. Safe to call on
        every project change."""
        self._project_root = project_root
        if project_root is not None:
            board_view.forget(project_root)  # a project (re)loaded reads its angle again
        tgz = odb_archive.find(project_root) if project_root is not None else None
        self.set_hint(tgz.name if tgz is not None else "")
        self._viewer.board = project_root
        self._viewer.subtitle = tgz.name if tgz is not None else ""
        self._hide_prefixes = read_hide_prefixes(
            project_root / "config" if project_root is not None else None
        )
        self.set_status("")
        self._board_read.set(True)
        worker = _LoadWorker(project_root)
        self._load_worker = worker
        _LIVE_WORKERS.add(worker)
        worker.done.connect(self._on_loaded)  # auto-dropped if we die
        worker.finished.connect(lambda w=worker: _LIVE_WORKERS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_loaded(self, data: dict) -> None:
        # An earlier project/startup load may finish after the current one.
        if self.sender() is not self._load_worker:
            return
        self._board_read.set(False)
        comps = data.get("components", []) if isinstance(data, dict) else []
        nets = data.get("nets", []) if isinstance(data, dict) else []
        if comps or nets:
            self.set_status("")
            self.set_board(comps, nets)
        else:
            self.set_board([], [])
            self.set_status(
                data.get("error")
                if isinstance(data, dict) and data.get("error")
                else "No ODB++ archive for this project."
            )

    def set_board(self, components: list, nets: list) -> None:
        self._components = list(components or [])
        self._nets = list(nets or [])
        self._refilter()

    def set_status(self, text: str) -> None:
        self._status.setText(text or "")

    # -- populate / filter ----------------------------------------------------

    def _refilter(self, *_) -> None:
        cs = filter_components(self._components, self._search.text())
        ns = filter_nets(
            self._nets, self._search.text(), self._hide_prefixes, self._show_hidden.isChecked()
        )
        for tree, make, rows in (
            (self._comp_tree, component_item, cs),
            (self._net_tree, net_item, ns),
        ):
            tree.clear()
            tree.addTopLevelItems([make(row) for row in rows])
        if self._components or self._nets:
            parts = (
                f"{len(cs)} parts"
                if len(cs) == len(self._components)
                else f"{len(cs)} of {len(self._components)} parts"
            )
            self._status.setText(f"{parts} · {len(ns)} nets")

    def _on_double_click(self, item, _col=0) -> None:
        """Copy the row's identifier (refdes / refdes.pin / net) to the clipboard
        for easy paste into the procedure text."""
        text = item.data(0, ROLE_COPY)
        if text:
            QApplication.clipboard().setText(str(text))
            self.set_status(f"\U0001f4cb  Copied '{text}' to clipboard")

    # -- selection -> render --------------------------------------------------

    def _on_sel_changed(self, *_) -> None:
        self._sel_timer.start(250)  # debounce

    def _render_selection(self) -> None:
        sel = selected_target(self._tabs.currentWidget())
        if sel is not None and self._project_root is not None:
            self._render(*sel)

    def _render(self, refdes: str, pad: str) -> None:
        """The location's picture: cached, else rendered off the UI thread — after the current
        render, the latest asked winning."""
        pin, cap = pad or None, _caption(refdes, pad)
        z, w = odb_inspect.cached_image_paths(self._project_root, refdes, pin)
        if z or w:
            self._render_pending = None
            self._viewer.show_image(
                QPixmap(str(z)) if z else None, QPixmap(str(w)) if w else None, cap
            )
            return
        self._viewer.show_message(f"Generating {cap}…")
        if self._rendering:
            self._render_pending = (refdes, pad)  # latest wins
            return
        self._rendering = True
        worker = _RenderWorker(self._project_root, refdes, pin)
        _LIVE_WORKERS.add(worker)
        worker.done.connect(self._on_rendered)
        worker.finished.connect(lambda w=worker: _LIVE_WORKERS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_rendered(self, z: str, w: str, refdes: str, pad: str) -> None:
        self._rendering = False
        cap = _caption(refdes, pad)
        zpix = QPixmap(z) if z else None
        wpix = QPixmap(w) if w else None
        if (zpix is not None and not zpix.isNull()) or (wpix is not None and not wpix.isNull()):
            self._viewer.show_image(zpix, wpix, cap)
        else:
            self._viewer.show_message(f"Could not render {cap}.")
        pend = self._render_pending
        self._render_pending = None
        if pend is not None and pend != (refdes, pad):
            self._render(*pend)  # the latest target asked while this one rendered
