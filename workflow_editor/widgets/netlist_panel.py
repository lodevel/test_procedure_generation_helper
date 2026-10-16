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
from PySide6.QtGui import QPixmap, QTransform
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLineEdit, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QLabel, QCheckBox, QScrollArea, QSplitter,
    QDialog, QPushButton, QApplication,
)

from project_services import ui_kit
from test_procedure_gui import theme

from ..board_source import find_odb_tgz


class _BoardImagePopup(QDialog):
    """Full board image (board S12): wide (context) + zoomed (detail) side by side, each on
    a card, under a header naming the part (*caption*) and the board (*subtitle*); Rotate
    left / right; click either image, or Close, to close. Mirrors the main GUI's
    interactive-execution popup."""

    _MAX = 560

    def __init__(self, zoomed, wide, caption="", subtitle="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(caption or "Board image")
        self._zoomed, self._wide, self._angle = zoomed, wide, 0

        rotate_left = QPushButton("↶ Rotate left")
        rotate_left.clicked.connect(lambda: self._rotate(-90))
        rotate_right = QPushButton("Rotate right ↷")
        rotate_right.clicked.connect(lambda: self._rotate(90))
        close_btn = ui_kit.primary(QPushButton("Close"))
        close_btn.clicked.connect(self.accept)
        body = ui_kit.framed(
            self, ui_kit.HeaderBand(caption or "Board image", subtitle, stacked=True),
            ui_kit.FooterBand([close_btn], left=[rotate_left, rotate_right]))
        row = ui_kit.hbox()
        row.setSpacing(14)
        self._wide_lbl = (self._image_card(row, "Wide view")
                          if wide is not None and not wide.isNull() else None)
        self._zoom_lbl = self._image_card(row, "Zoomed view")
        body.addLayout(row)

        self._update()

    def _image_card(self, row, title: str) -> QLabel:
        """A card titled *title* in *row*; returns the label its image is drawn on."""
        card = ui_kit.Card(title)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.mousePressEvent = lambda e: self.close()
        card.body.addWidget(label)
        row.addWidget(card)
        return label

    def _rotate(self, delta: int) -> None:
        self._angle = (self._angle + delta) % 360
        self._update()

    def _scaled(self, pm: QPixmap) -> QPixmap:
        if self._angle:
            pm = pm.transformed(QTransform().rotate(self._angle),
                                Qt.TransformationMode.SmoothTransformation)
        if pm.width() > self._MAX or pm.height() > self._MAX:
            pm = pm.scaled(self._MAX, self._MAX,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        return pm

    def _update(self) -> None:
        if self._zoomed is not None and not self._zoomed.isNull():
            self._zoom_lbl.setPixmap(self._scaled(self._zoomed))
        if (self._wide_lbl is not None and self._wide is not None
                and not self._wide.isNull()):
            self._wide_lbl.setPixmap(self._scaled(self._wide))
        self.adjustSize()

from ..core import odb_inspect

# Keep worker threads referenced until they finish, so a fast close doesn't
# destroy a running QThread. Connections to the panel auto-disconnect if it dies.
_LIVE_WORKERS: set = set()

_ROLE_REFDES = Qt.ItemDataRole.UserRole + 1     # render target refdes ("" = none)
_ROLE_PAD = Qt.ItemDataRole.UserRole + 2        # render target pad ("" = whole part)
_ROLE_COPY = Qt.ItemDataRole.UserRole + 3       # text copied to clipboard on dbl-click


def _pin_name(pin) -> str:
    return (pin.get("name") if isinstance(pin, dict) else str(pin)) or ""


def _pin_net(pin) -> str:
    return (pin.get("net") if isinstance(pin, dict) else "") or ""


def filter_components(components: list, query: str) -> list:
    q = (query or "").strip().lower()
    if not q:
        return list(components)
    out = []
    for c in components:
        if q in (c.get("refdes", "") or "").lower():
            out.append(c)
            continue
        for p in c.get("pins", []) or ():
            if q in _pin_name(p).lower() or q in _pin_net(p).lower():
                out.append(c)
                break
    return out


def visible_nets(nets: list, query: str, hide_prefixes, show_hidden: bool) -> list:
    q = (query or "").strip().lower()
    out = []
    for n in nets:
        name = n.get("net", "") or ""
        if not show_hidden and odb_inspect.is_hidden_net(name, hide_prefixes):
            continue
        if not q or q in name.lower():
            out.append(n)
            continue
        for nd in n.get("nodes", []) or ():
            if (q in (nd.get("refdes", "") or "").lower()
                    or q in str(nd.get("pin", "") or "").lower()):
                out.append(n)
                break
    return out


class _LoadWorker(QThread):
    """Loads {components, nets} off the UI thread; never raises."""

    done = Signal(dict)

    def __init__(self, project_root):
        super().__init__()
        self._root = project_root

    def run(self) -> None:  # noqa: D401
        try:
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
        self.done.emit(str(z) if z else "", str(w) if w else "",
                       self._refdes, self._pad or "")


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
        self._cur_zoom: QPixmap | None = None
        self._cur_wide: QPixmap | None = None
        self._cur_cap: str = ""
        self._rendering = False
        self._render_pending = None         # (refdes, pad) to render after current
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.timeout.connect(self._render_selection)
        self._build_ui()
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
            "Configure the hidden prefixes in the project settings.")
        self._show_hidden.toggled.connect(self._refilter)
        row.addWidget(self._show_hidden)
        self._status = ui_kit.note()
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._status, 1)
        tl.addLayout(row)
        tl.addWidget(ui_kit.note("Double-click a row to copy its name · select it to see it "
                                 "on the board."))
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        self._viewer_caption = ui_kit.monospace(ui_kit.note(wrap=False))
        self._full_view = ui_kit.link("Full view…", lambda: self._on_viewer_click(None))
        bl.addLayout(ui_kit.hbox(ui_kit.bold("Board image"), self._viewer_caption, None,
                                 self._full_view))
        self._viewer_scroll = QScrollArea()
        self._viewer_scroll.setObjectName("boardImage")  # a framed, rounded well (app QSS)
        self._viewer_scroll.setWidgetResizable(True)
        self._viewer_label = QLabel("")
        self._viewer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._viewer_label.setWordWrap(True)
        self._viewer_label.setStyleSheet(f"color:{theme.muted_color()};")
        self._viewer_label.mousePressEvent = self._on_viewer_click  # click -> full view
        self._viewer_scroll.setWidget(self._viewer_label)
        bl.addWidget(self._viewer_scroll, 1)
        split.addWidget(bottom)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 320])
        self.body.addWidget(split, 1)
        self._full_view.setEnabled(False)

    # -- data -----------------------------------------------------------------

    def load(self, project_root) -> None:
        """Load the project's netlist off-thread and populate. Safe to call on
        every project change."""
        self._project_root = project_root
        tgz = find_odb_tgz(project_root) if project_root is not None else None
        self.set_hint(tgz.name if tgz is not None else "")
        self._hide_prefixes = odb_inspect.load_hide_prefixes(project_root)
        self.set_status("Loading board…")
        worker = _LoadWorker(project_root)
        self._load_worker = worker
        _LIVE_WORKERS.add(worker)
        worker.done.connect(self._on_loaded)            # auto-dropped if we die
        worker.finished.connect(lambda w=worker: _LIVE_WORKERS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_loaded(self, data: dict) -> None:
        # An earlier project/startup load may finish after the current one.
        if self.sender() is not self._load_worker:
            return
        comps = data.get("components", []) if isinstance(data, dict) else []
        nets = data.get("nets", []) if isinstance(data, dict) else []
        if comps or nets:
            self.set_status("")
            self.set_board(comps, nets)
        else:
            self.set_board([], [])
            self.set_status(
                data.get("error") if isinstance(data, dict) and data.get("error")
                else "No ODB++ archive for this project.")

    def set_board(self, components: list, nets: list) -> None:
        self._components = list(components or [])
        self._nets = list(nets or [])
        self._refilter()

    def set_status(self, text: str) -> None:
        self._status.setText(text or "")

    # -- populate / filter ----------------------------------------------------

    def _refilter(self, *_) -> None:
        self._populate_components()
        self._populate_nets()
        if self._components or self._nets:
            cs = filter_components(self._components, self._search.text())
            ns = visible_nets(self._nets, self._search.text(),
                              self._hide_prefixes, self._show_hidden.isChecked())
            parts = (f"{len(cs)} parts" if len(cs) == len(self._components)
                     else f"{len(cs)} of {len(self._components)} parts")
            self._status.setText(f"{parts} · {len(ns)} nets")

    def _populate_components(self) -> None:
        self._comp_tree.clear()
        for c in filter_components(self._components, self._search.text()):
            refdes = c.get("refdes", "")
            side = c.get("side", "")
            top = QTreeWidgetItem(
                [f"{refdes}  ({side.lower()})" if side else refdes, ""])
            top.setData(0, _ROLE_REFDES, refdes)
            top.setData(0, _ROLE_PAD, "")
            top.setData(0, _ROLE_COPY, refdes)
            for p in c.get("pins", []) or ():
                name, net = _pin_name(p), _pin_net(p)
                child = QTreeWidgetItem([f"pin {name}", net])
                child.setData(0, _ROLE_REFDES, refdes)
                child.setData(0, _ROLE_PAD, name)
                child.setData(0, _ROLE_COPY, f"{refdes}.{name}")
                top.addChild(child)
            self._comp_tree.addTopLevelItem(top)

    def _populate_nets(self) -> None:
        self._net_tree.clear()
        nets = visible_nets(self._nets, self._search.text(),
                            self._hide_prefixes, self._show_hidden.isChecked())
        for n in nets:
            net = n.get("net", "")
            nodes = n.get("nodes", []) or ()
            top = QTreeWidgetItem([net, f"{len(nodes)} pin(s)"])
            top.setData(0, _ROLE_REFDES, "")        # a net name has no image
            top.setData(0, _ROLE_PAD, "")
            top.setData(0, _ROLE_COPY, net)
            for nd in nodes:
                rd, pin = nd.get("refdes", ""), str(nd.get("pin", ""))
                child = QTreeWidgetItem([f"{rd} pin {pin}", ""])
                child.setData(0, _ROLE_REFDES, rd)  # a node IS a pin -> renderable
                child.setData(0, _ROLE_PAD, pin)
                child.setData(0, _ROLE_COPY, f"{rd}.{pin}")
                top.addChild(child)
            self._net_tree.addTopLevelItem(top)

    def _on_double_click(self, item, _col=0) -> None:
        """Copy the row's identifier (refdes / refdes.pin / net) to the clipboard
        for easy paste into the procedure text."""
        text = item.data(0, _ROLE_COPY)
        if text:
            QApplication.clipboard().setText(str(text))
            self.set_status(f"\U0001F4CB  Copied '{text}' to clipboard")

    # -- selection -> render --------------------------------------------------

    def _on_sel_changed(self, *_) -> None:
        self._sel_timer.start(250)                  # debounce

    def _selection_target(self):
        tree = self._tabs.currentWidget()
        items = tree.selectedItems() if tree is not None else []
        if not items:
            return None
        it = items[0]
        refdes = it.data(0, _ROLE_REFDES) or ""
        pad = it.data(0, _ROLE_PAD) or ""
        return (refdes, pad) if refdes else None

    def _render_selection(self) -> None:
        sel = self._selection_target()
        if sel is None or self._project_root is None:
            return
        refdes, pad = sel
        pin = pad or None
        cap = refdes + (f" pin {pad}" if pad else "")
        z, w = odb_inspect.cached_image_paths(self._project_root, refdes, pin)
        if z or w:
            self._render_pending = None
            self.show_image(QPixmap(str(z)) if z else None,
                            QPixmap(str(w)) if w else None, cap)
            return
        if self._rendering:
            self._render_pending = (refdes, pad)    # latest wins
            self.set_viewer_status(f"Generating {cap}…")
            return
        self._rendering = True
        self.set_viewer_status(f"Generating {cap}…")
        worker = _RenderWorker(self._project_root, refdes, pin)
        _LIVE_WORKERS.add(worker)
        worker.done.connect(self._on_rendered)
        worker.finished.connect(lambda w=worker: _LIVE_WORKERS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_rendered(self, z: str, w: str, refdes: str, pad: str) -> None:
        self._rendering = False
        cap = refdes + (f" pin {pad}" if pad else "")
        zpix = QPixmap(z) if z else None
        wpix = QPixmap(w) if w else None
        if (zpix is not None and not zpix.isNull()) or (wpix is not None and not wpix.isNull()):
            self.show_image(zpix, wpix, cap)
        else:
            self.set_viewer_status(f"Could not render {cap}.")
        pend = self._render_pending
        self._render_pending = None
        if pend is not None and pend != (refdes, pad):
            # re-select-driven render of the latest target
            self._render_one(*pend)

    def _render_one(self, refdes: str, pad: str) -> None:
        pin = pad or None
        cap = refdes + (f" pin {pad}" if pad else "")
        z, w = odb_inspect.cached_image_paths(self._project_root, refdes, pin)
        if z or w:
            self.show_image(QPixmap(str(z)) if z else None,
                            QPixmap(str(w)) if w else None, cap)
            return
        self._rendering = True
        self.set_viewer_status(f"Generating {cap}…")
        worker = _RenderWorker(self._project_root, refdes, pin)
        _LIVE_WORKERS.add(worker)
        worker.done.connect(self._on_rendered)
        worker.finished.connect(lambda w=worker: _LIVE_WORKERS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # -- viewer ---------------------------------------------------------------

    def set_viewer_status(self, text: str) -> None:
        self._cur_zoom = self._cur_wide = None
        self._viewer_caption.setText("")
        self._viewer_label.setPixmap(QPixmap())
        self._viewer_label.setText(text or "")
        self._viewer_label.setCursor(Qt.CursorShape.ArrowCursor)
        self._full_view.setEnabled(False)

    def show_image(self, zpix, wpix, caption: str) -> None:
        self._cur_zoom, self._cur_wide, self._cur_cap = zpix, wpix, caption
        self._viewer_caption.setText(caption)
        pm = zpix if (zpix is not None and not zpix.isNull()) else wpix
        if pm is not None and not pm.isNull():
            self._viewer_label.setText("")
            self._render_scaled(pm)
            self._viewer_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self._full_view.setEnabled(True)
        else:
            self._viewer_label.setText("Image unavailable.")
            self._viewer_label.setCursor(Qt.CursorShape.ArrowCursor)

    def _on_viewer_click(self, event) -> None:
        """Click the inline image → full wide+zoomed view (like the interactive
        execution explorer)."""
        if self._cur_zoom is not None or self._cur_wide is not None:
            _BoardImagePopup(self._cur_zoom, self._cur_wide, self._cur_cap,
                             self.hint.text().removeprefix("· "), self).exec()

    def _render_scaled(self, pm: QPixmap) -> None:
        """*pm* fitted into the image well (never enlarged)."""
        well = self._viewer_scroll.viewport().size()
        width = min(max(120, well.width() - 8), pm.width())
        height = min(max(80, well.height() - 8), pm.height())
        self._viewer_label.setPixmap(pm.scaled(
            width, height, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        pm = self._cur_zoom if (self._cur_zoom is not None
                                and not self._cur_zoom.isNull()) else self._cur_wide
        if pm is not None and not pm.isNull():
            self._render_scaled(pm)
