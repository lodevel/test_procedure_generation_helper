"""Board netlist reference panel for the Text tab.

A read-only ODB++ browser (Components | Nets) shown beside the procedure-text
editor to help author tests: search components/pins/nets, **double-click to
insert** the name at the editor cursor. Auto-generated net names (default
``Net*``, configurable per project) are hidden unless the *Show Net\\** toggle
is on. No image rendering — this is a names reference. Empty-and-friendly when
there is no ODB++ archive (never an error).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLineEdit, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QLabel, QCheckBox,
)

from ..core import odb_inspect

# Keep loader threads referenced until they finish, so a fast close doesn't
# destroy a running QThread. Connections to the panel auto-disconnect if it dies.
_LIVE_LOADERS: set = set()

_ROLE_INSERT = Qt.ItemDataRole.UserRole


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
    """Nets matching *query*, with auto-named nets (hide_prefixes) dropped unless
    *show_hidden*."""
    q = (query or "").strip().lower()
    out = []
    for n in nets:
        name = n.get("net", "") or ""
        if not show_hidden and odb_inspect.is_hidden_net(name, hide_prefixes):
            continue
        if not q:
            out.append(n)
            continue
        if q in name.lower():
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
        super().__init__()                  # no parent -> kept alive via _LIVE_LOADERS
        self._root = project_root

    def run(self) -> None:  # noqa: D401
        try:
            data = odb_inspect.load_board(self._root)
        except Exception:  # noqa: BLE001
            data = {"components": [], "nets": []}
        self.done.emit(data)


class NetlistPanel(QGroupBox):
    """Components | Nets browser; double-click inserts a name at the cursor."""

    insert_text = Signal(str)               # name to insert into the editor

    def __init__(self, parent=None) -> None:
        super().__init__("Board netlist", parent)
        self._components: list = []
        self._nets: list = []
        self._hide_prefixes: list = ["Net"]
        self._build_ui()
        self.set_board([], [])

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter components / pins / nets…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._refilter)
        lay.addWidget(self._search)

        self._tabs = QTabWidget()
        self._comp_tree = QTreeWidget()
        self._comp_tree.setHeaderLabels(["Component / pin", "Net"])
        self._comp_tree.setColumnWidth(0, 130)
        self._comp_tree.setUniformRowHeights(True)
        self._comp_tree.itemDoubleClicked.connect(self._on_double_click)
        self._net_tree = QTreeWidget()
        self._net_tree.setHeaderLabels(["Net / node", ""])
        self._net_tree.setColumnWidth(0, 180)
        self._net_tree.setUniformRowHeights(True)
        self._net_tree.itemDoubleClicked.connect(self._on_double_click)
        self._tabs.addTab(self._comp_tree, "Components")
        self._tabs.addTab(self._net_tree, "Nets")
        lay.addWidget(self._tabs, 1)

        row = QHBoxLayout()
        self._show_hidden = QCheckBox("Show auto nets (Net*)")
        self._show_hidden.setToolTip(
            "Show auto-generated net names (e.g. NetD16_A) that are hidden by "
            "default. Configure the hidden prefixes in the project settings.")
        self._show_hidden.toggled.connect(self._refilter)
        row.addWidget(self._show_hidden)
        row.addStretch(1)
        lay.addLayout(row)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#777; font-size:9pt;")
        lay.addWidget(self._status)

        hint = QLabel("Double-click a row to insert its name at the cursor.")
        hint.setStyleSheet("color:#999; font-size:9pt;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

    # -- data -----------------------------------------------------------------

    def load(self, project_root) -> None:
        """Load the project's netlist off-thread and populate. Safe to call on
        every project change."""
        self._hide_prefixes = odb_inspect.load_hide_prefixes(project_root)
        self.set_status("Loading board…")
        worker = _LoadWorker(project_root)
        _LIVE_LOADERS.add(worker)
        worker.done.connect(self._on_loaded)                # auto-dropped if we die
        worker.finished.connect(lambda w=worker: _LIVE_LOADERS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_loaded(self, data: dict) -> None:
        comps = data.get("components", []) if isinstance(data, dict) else []
        nets = data.get("nets", []) if isinstance(data, dict) else []
        if comps or nets:
            self.set_status("")
            self.set_board(comps, nets)
        else:
            self.set_board([], [])
            self.set_status("No ODB++ archive for this project.")

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
            self._status.setText(
                f"{len(cs)}/{len(self._components)} components · "
                f"{len(ns)} nets shown")

    def _populate_components(self) -> None:
        self._comp_tree.clear()
        for c in filter_components(self._components, self._search.text()):
            refdes = c.get("refdes", "")
            side = c.get("side", "")
            top = QTreeWidgetItem(
                [f"{refdes}  ({side.lower()})" if side else refdes, ""])
            top.setData(0, _ROLE_INSERT, refdes)        # insert refdes
            for p in c.get("pins", []) or ():
                name, net = _pin_name(p), _pin_net(p)
                child = QTreeWidgetItem([f"pin {name}", net])
                child.setData(0, _ROLE_INSERT, net or name)  # pin -> its net (signal)
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
            top.setData(0, _ROLE_INSERT, net)           # insert net name
            for nd in nodes:
                rd, pin = nd.get("refdes", ""), str(nd.get("pin", ""))
                child = QTreeWidgetItem([f"{rd} pin {pin}", ""])
                child.setData(0, _ROLE_INSERT, rd)      # node -> refdes
                top.addChild(child)
            self._net_tree.addTopLevelItem(top)

    def _on_double_click(self, item, _col=0) -> None:
        text = item.data(0, _ROLE_INSERT)
        if text:
            self.insert_text.emit(str(text))
