"""DCDC test wizard — paged QWizard, per-IC sessions that span rail-read → build.

FOUR pages, Next → Next → Next → Finish:

  P1 Classify — one classifier ``SkillChatWidget`` (with its context picker) on the
                right; a CHECKBOX list of the power ICs it found (refdes + part, NO
                rail) on the left. Next unlocks once ≥1 IC is checked.
  P2 Rail-ID  — the HUMAN GATE on the rail-read. One HEADLESS per-IC session reads
                each IC's rail (turn 1); the list fills in progressively
                (``→ ⏳ reading…`` → ``→ +SYS_12V``). The operator REVIEWS each rail
                and can CORRECT a bad one (edit inline, or re-ask the session), then
                ticks which to build. Next unlocks once every checked IC has a rail.
  P3 Build    — the SAME per-IC sessions (re-parented in) build the test (turn 2),
                CAPPED by the shared scheduler. Per-IC status badge, Validate /
                Abandon (switchable until Finish), read-only test view. Next unlocks
                once every IC is accepted or abandoned.
  P4 Create   — per ACCEPTED test: "New test" (name, collision warning) or update an
                EXISTING test. Finish materializes them all. No chat.

The per-IC ``SkillChatWidget`` is created on P2 and REUSED on P3 — its persistent
OpenCode session carries the netlist/datasheet from the rail-read into the build, so
the build never re-fetches. The wizard is the COORDINATOR (owns the shared scheduler +
the per-IC sessions + the signal routing by turn); the pages are VIEWS. All non-Qt
logic (list parse, rail parse, done-signal, validate, materialize, the scheduler) lives
in tested pure leaves under ``authoring/``.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QComboBox, QSpinBox,
    QTextEdit, QSplitter, QStackedWidget, QWidget, QGroupBox, QCheckBox, QCompleter,
    QProgressDialog,
)

from .. import theme
from ..authoring import registry, skill_menu
from ..authoring.wizard.list_parse import (
    parse_classifier_list, parse_rail_reply, parse_rail_probe, IcRow,
)
from ..authoring.wizard.done_signal import find_dcdc_test_block
from ..authoring.wizard.scheduler import ConcurrencyScheduler, SlotState
from ..authoring.wizard import finder_cache
from ..authoring.test_materializer import (
    materialize_test, update_existing_test, list_existing_tests,
)
from .skill_chat_widget import SkillChatWidget

log = logging.getLogger(__name__)

_PARALLEL_DEFAULT = 5
_PARALLEL_MAX = 16


def _load_saved_cap() -> int:
    """Persisted max concurrent LLM sessions (settings ``wizard.max_concurrent_sessions``),
    clamped to ``[1, _PARALLEL_MAX]``; defaults to ``_PARALLEL_DEFAULT``."""
    try:
        from ..dialogs.settings_dialog import load_settings
        n = int(load_settings().get("wizard", {}).get(
            "max_concurrent_sessions", _PARALLEL_DEFAULT))
    except Exception:  # noqa: BLE001 — settings unreadable → fall back to the default
        n = _PARALLEL_DEFAULT
    return max(1, min(_PARALLEL_MAX, n))


def _save_cap(n: int) -> None:
    """Persist the max concurrent LLM sessions to settings (best-effort)."""
    try:
        from ..dialogs.settings_dialog import load_settings, save_settings
        s = load_settings()
        s.setdefault("wizard", {})["max_concurrent_sessions"] = int(n)
        save_settings(s)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        log.exception("dcdc-wizard: could not persist max_concurrent_sessions")
_NEW_TEST = "➕ New test"

# Per-IC settled phases (the scheduler supplies the transient running/queued). The
# lifecycle: PENDING → (rail-read) → RAILED → (build) → NEEDS_INPUT|READY →
# ACCEPTED|ABANDONED. RAIL_FAILED = turn-1 gave no parseable rail (operator corrects).
_PENDING = "pending"
_RAILED = "railed"
_RAIL_FAILED = "rail_failed"
_NEEDS_INPUT = "needs_input"
_READY = "ready"
_ACCEPTED = "accepted"
_ABANDONED = "abandoned"
_TERMINAL = (_ACCEPTED, _ABANDONED)
# Phases where a RUNNING/QUEUED scheduler turn is the RAIL-READ (turn 1), not the build.
_RAIL_PHASES = (_PENDING,)

_BADGE = {
    "running": ("🟢", "running"),
    "queued": ("⏳", "queued"),
    _PENDING: ("•", "pending"),
    _RAILED: ("🔌", "railed"),
    _RAIL_FAILED: ("⚠️", "unsure — see chat"),
    _NEEDS_INPUT: ("❓", "needs input"),
    _READY: ("📋", "ready"),
    _ACCEPTED: ("✅", "accepted"),
    _ABANDONED: ("🚫", "abandoned"),
}


def _rail_priming(row: IcRow, common: str = "") -> str:
    """Turn-1 message: hand the per-IC skill its IC and ask ONLY for the rail. The shared
    'common instructions' (e.g. 'the input bus is +SYS_12V') ride along so every rail-read
    gets the same standing hints — the P2 twin of the P3 build common."""
    base = (f"Read the output rail of {row.refdes} — {row.part} ({row.kind}). "
            f"This is TURN 1: reply with ONLY the rail as `{row.refdes} → <rail>`. "
            f"Do NOT build the test yet.")
    common = (common or "").strip()
    return f"{base}\n\n{common}" if common else base


def _build_priming(row: IcRow, common: str) -> str:
    """Turn-2 message: hand back the confirmed rail and ask for the test. The common
    instructions (e.g. P4/P2 PSU connectors, 10 A limit) ride along so the build gets
    the standard answers up front instead of stopping to ask."""
    base = (f"This is TURN 2. The confirmed output rail of {row.refdes} — {row.part} "
            f"is {row.rail}. Build the test on it now (Stages 1–4).")
    common = (common or "").strip()
    return f"{base}\n\n{common}" if common else base


def _alert() -> None:
    """A short audible cue when a build needs the operator (needs-input / ready)."""
    try:
        QApplication.beep()
    except Exception:  # noqa: BLE001
        pass


def _add_list_controls(list_widget: QListWidget) -> QWidget:
    """A controls row for a checkable IC list: Select all / Deselect all (over the
    currently VISIBLE rows) plus a live filter box (hides non-matching rows). Returns a
    container widget to drop into the page layout DIRECTLY ABOVE ``list_widget``."""
    bar = QWidget()
    h = QHBoxLayout(bar)
    h.setContentsMargins(0, 0, 0, 0)
    sel = QPushButton("Select all")
    desel = QPushButton("Deselect all")
    filt = QLineEdit()
    filt.setPlaceholderText("filter…")

    def _set_all(state: Qt.CheckState) -> None:
        for i in range(list_widget.count()):
            it = list_widget.item(i)
            if not it.isHidden():
                it.setCheckState(state)

    def _filter(text: str) -> None:
        t = text.lower()
        for i in range(list_widget.count()):
            it = list_widget.item(i)
            it.setHidden(t not in it.text().lower())

    sel.clicked.connect(lambda: _set_all(Qt.CheckState.Checked))
    desel.clicked.connect(lambda: _set_all(Qt.CheckState.Unchecked))
    filt.textChanged.connect(_filter)
    h.addWidget(sel)
    h.addWidget(desel)
    h.addWidget(filt, 1)
    return bar


# =========================================================================== #
# Per-IC session state (wizard-owned; the chat persists rail-read → build)     #
# =========================================================================== #

class _IcState:
    """One IC's wizard-owned session: its ``SkillChatWidget`` (created on P2 for the
    rail-read, re-parented into P3 for the build) plus the metadata both pages read.
    The chat's persistent OpenCode session carries turn-1 context into turn-2."""

    def __init__(self, row: IcRow, chat: SkillChatWidget):
        self.row = row              # IcRow; .rail is mutable (filled by turn 1 / edit)
        self.key = row.refdes
        self.chat = chat
        self.phase = _PENDING
        self.awaiting: Optional[str] = None   # "rail" | "build" — routes the next reply
        self.test_block = ""
        self.build_pending = False  # build requested while turn-1 rail-read still ran
        self.rail_read_started = False  # turn-1 kickoff has fired (per-IC btn: Read rail→Re-read)
        self.probe_hint = ""            # display-only TP hint shown when the netname is generic
        self.panel: Optional["_IcPanel"] = None  # set when P3 wraps it


# =========================================================================== #
# Per-IC build panel (P3; wraps an existing wizard-owned chat — re-parents it)  #
# =========================================================================== #

class _IcPanel(QWidget):
    """One IC's BUILD surface on P3: its (already-existing) chat + a read-only test
    view + Validate / Abandon. The chat is the SAME ``SkillChatWidget`` that rail-read
    on P2 — adopting it here re-parents it (its session/context come along)."""

    def __init__(self, state: _IcState, parent=None):
        super().__init__(parent)
        self.state = state
        self.row = state.row
        self.key = state.key
        self.chat = state.chat

        # VSCode-style: the TEST fills the centre (the main area) with Validate/Abandon
        # below it; the chat docks NARROW on the RIGHT (Copilot-style).
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        self.test_view = QTextEdit()
        self.test_view.setReadOnly(True)
        self.test_view.setPlaceholderText("The generated test appears here once built.")
        if state.test_block:
            self.test_view.setPlainText(state.test_block)
        cl.addWidget(self.test_view, 1)
        row_btns = QHBoxLayout()
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        row_btns.addWidget(self.status, 1)
        self.validate_btn = QPushButton("✅ Validate")
        self.validate_btn.setEnabled(bool(state.test_block))
        self.abandon_btn = QPushButton("🚫 Abandon")
        row_btns.addWidget(self.validate_btn)
        row_btns.addWidget(self.abandon_btn)
        cl.addLayout(row_btns)

        self._split = QSplitter(Qt.Orientation.Horizontal)
        self._split.addWidget(centre)
        self._split.addWidget(self.chat)            # right: the chat (narrower)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 0)
        self._split.setSizes([560, 320])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._split)

    def adopt_chat(self) -> None:
        """Re-parent the chat back into THIS panel's split, on the right (it may currently
        sit in P2's host). addWidget re-parents + re-appends to the right of the centre."""
        self._split.addWidget(self.chat)
        self._split.setSizes([560, 320])


# =========================================================================== #
# P1 — Classify power ICs                                                      #
# =========================================================================== #

class _ClassifyPage(QWizardPage):
    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("1 · Classify power ICs")
        self.setSubTitle("Run the classifier, then tick the ICs you want tests for. "
                         "Include the netlist in the chat's context picker.")
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Power ICs (tick the ones to build):"))
        self._ic_list = QListWidget()
        self._ic_list.itemChanged.connect(lambda *_: self.completeChanged.emit())
        ll.addWidget(_add_list_controls(self._ic_list))
        ll.addWidget(self._ic_list, 1)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.chat = SkillChatWidget(
            [wiz.classifier] if wiz.classifier else [],
            wiz.backend_factory,
            sources=wiz.sources,
            documents_dir=wiz.documents_dir,
            backend_tab_id="dcdc_classifier",
            show_skill_selector=False,
            show_run_button=False,
        )
        rl.addWidget(self.chat, 1)
        btn_row = QHBoxLayout()
        self._find_btn = QPushButton("🔍 Classify ICs")
        self._find_btn.setToolTip("List the board's power ICs (re-runs fresh each time).")
        self._find_btn.clicked.connect(self._on_search)
        btn_row.addWidget(self._find_btn)
        btn_row.addStretch(1)
        rl.addLayout(btn_row)
        split.addWidget(right)
        split.setSizes([300, 540])
        lay = QVBoxLayout(self)
        lay.addWidget(split)

        self.chat.reply_finished.connect(self._on_reply)
        # Next stays GREYED while the classifier runs — re-evaluate isComplete on busy change.
        self.chat.busy_changed.connect(lambda *_: self.completeChanged.emit())
        if not (wiz.classifier and wiz.authoring):
            self._find_btn.setEnabled(False)
            self.chat.set_status("Wizard skills not found (need dcdc_classifier + "
                                 "dcdc_authoring under authoring_wizards/).")

    def initializePage(self) -> None:  # noqa: N802
        if self._ic_list.count() == 0:
            cached = finder_cache.load(self._wiz.project_root)
            if cached:
                self._populate(cached)
                self.chat.set_status(
                    f"Loaded {len(cached)} cached IC(s) — 'Restart' to refresh.")

    def _on_search(self) -> None:
        if self.chat.is_busy:
            return
        finder_cache.clear(self._wiz.project_root)   # one button: always a fresh run
        self._ic_list.clear()
        self.chat.set_skill(self._wiz.classifier)    # fresh session
        self.chat.set_status("Classifying power ICs…")
        self.chat.run_kickoff()
        self.completeChanged.emit()                  # grey Next while it runs

    def _populate(self, rows: list) -> None:
        self._ic_list.clear()
        for n, r in enumerate(rows, 1):
            r.rail = ""   # classifier rows carry no rail; read on P2
            it = QListWidgetItem(f"{n}. {r.refdes} — {r.part} ({r.kind})")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            it.setData(Qt.ItemDataRole.UserRole, r)
            self._ic_list.addItem(it)
        self.completeChanged.emit()

    def _on_reply(self, text: str) -> None:
        rows = parse_classifier_list(text)
        self._populate(rows)
        if rows:
            finder_cache.save(self._wiz.project_root, rows)
        self.chat.set_status(
            f"Found {len(rows)} power IC(s) — untick any you don't want, then Next."
            if rows else "No power ICs parsed — refine in the chat and classify again.")

    def checked_rows(self) -> list:
        out = []
        for i in range(self._ic_list.count()):
            it = self._ic_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def isComplete(self) -> bool:  # noqa: N802
        # Next stays greyed until classification FINISHES (chat not busy) and ≥1 IC is ticked.
        return bool(self.checked_rows()) and not self.chat.is_busy

    def validatePage(self) -> bool:  # noqa: N802
        self._wiz.checked = self.checked_rows()
        self._wiz.context = self.chat.resolved_context()
        return True

    def shutdown(self) -> None:
        self.chat.shutdown()


# =========================================================================== #
# P2 — Rail identification (the human gate) + pick                             #
# =========================================================================== #

class _RailHost(QWidget):
    """One IC's rail-ID surface on P2 (master-detail, mirroring P3's build panel): its
    CHAT (re-ask / discuss the rail in conversation), an EDITABLE rail field (override a
    bad read), and a Re-read button. The chat is the wizard-owned ``SkillChatWidget`` —
    adopted here on P2, re-parented into P3's panel for the build."""

    def __init__(self, state: _IcState, on_read, net_names, parent=None):
        super().__init__(parent)
        self.state = state
        self.key = state.key
        self.chat = state.chat
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.chat, 1)            # adopts (re-parents) the chat into this host
        row = QHBoxLayout()
        row.addWidget(QLabel("Rail:"))
        # A filterable dropdown CONSTRAINED to the board's real nets: read automatically,
        # type any substring to filter, pick the right net (no free-text typos).
        self.rail_combo = QComboBox()
        self.rail_combo.setEditable(True)
        self.rail_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.rail_combo.addItem("")
        self.rail_combo.addItems(net_names or [])
        self.rail_combo.setCurrentText("")
        comp = self.rail_combo.completer()
        if comp is not None:
            comp.setFilterMode(Qt.MatchFlag.MatchContains)
            comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.rail_combo.setToolTip("The rail net — read automatically; type to FILTER the "
                                   "board's nets and pick the right one.")
        row.addWidget(self.rail_combo, 1)
        # Adaptive per-IC action: "🔌 Read rail" on the FIRST press (kicks off this IC's
        # rail-read), then "↻ Re-read" once read. refresh() flips the label by state.
        self.reread_btn = QPushButton("🔌 Read rail")
        self.reread_btn.setToolTip("Read THIS IC's rail; press again to re-read (flag a wrong "
                                   "+Vin/+Vout).")
        self.reread_btn.clicked.connect(lambda: on_read(self.key))
        row.addWidget(self.reread_btn)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        row.addWidget(self.status)
        lay.addLayout(row)

    def adopt_chat(self) -> None:
        """Re-parent the chat back into THIS host (it may currently sit in P3's panel or
        the holder). Idempotent — addWidget is a no-op when already parented here."""
        self.layout().insertWidget(0, self.chat)

    def rail_value(self) -> str:
        return self.rail_combo.currentText().strip()

    def set_rail(self, rail: str) -> None:
        """Fill the field from a fresh read (auto or re-read). Skips while the operator is
        typing; the wizard is the ONLY writer, so a manual correction survives refreshes."""
        le = self.rail_combo.lineEdit()
        if rail and not (le is not None and le.hasFocus()):
            self.rail_combo.setCurrentText(rail)


class _RailPage(QWizardPage):
    """Master-detail (mirrors P3): a CHECKABLE list of ICs on the left, the selected IC's
    rail-read CHAT + editable rail on the right. A '🔌 Read rails' trigger starts the
    reads (NO auto-start)."""

    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("2 · Identify the rails")
        self.setSubTitle("Press 🔌 Read checked rails — or 🔌 Read rail on one IC — then REVIEW "
                         "each, fix a wrong rail (edit the field or chat with that IC), and "
                         "tick which to build.")
        self._hosts: dict[str, _RailHost] = {}
        top = QHBoxLayout()
        self._read_btn = QPushButton("🔌 Read checked rails")
        self._read_btn.setToolTip("Start reading the CHECKED ICs' rails (capped) — "
                                  "untick an IC to skip its read.")
        self._read_btn.clicked.connect(self._on_read_all)
        top.addWidget(self._read_btn)
        top.addWidget(QLabel("Parallel:"))
        self._cap_spin = QSpinBox()
        self._cap_spin.setRange(1, _PARALLEL_MAX)
        self._cap_spin.setValue(_load_saved_cap())
        self._cap_spin.setToolTip("Max concurrent LLM sessions — remembered across runs.")
        self._cap_spin.valueChanged.connect(self._wiz._on_cap)
        top.addWidget(self._cap_spin)
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{theme.muted_color()};")
        top.addWidget(self._summary, 1)
        cm_row = QHBoxLayout()
        cm_row.addWidget(QLabel("Common instructions for all rail-reads:"))
        self._common = QLineEdit()
        self._common.setPlaceholderText(
            "shared hint prepended to every rail-read — e.g. the input bus is +SYS_12V")
        cm_row.addWidget(self._common, 1)
        split = QSplitter(Qt.Orientation.Horizontal)
        self._ic_list = QListWidget()
        self._ic_list.currentRowChanged.connect(self._on_select)
        self._ic_list.itemChanged.connect(lambda *_: self.completeChanged.emit())
        list_col = QWidget()
        lcl = QVBoxLayout(list_col)
        lcl.setContentsMargins(0, 0, 0, 0)
        lcl.addWidget(_add_list_controls(self._ic_list))
        lcl.addWidget(self._ic_list, 1)
        split.addWidget(list_col)
        self._stack = QStackedWidget()
        split.addWidget(self._stack)
        split.setSizes([280, 600])
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(cm_row)
        lay.addWidget(split, 1)

    def initializePage(self) -> None:  # noqa: N802
        """Create a session + host per checked IC. NO auto-read — the operator presses
        Read rails. Incremental: Back to P1 to add/remove keeps the rest + their reads."""
        checked = list(self._wiz.checked)
        keys = [r.refdes for r in checked]
        for key in list(self._hosts):                 # drop de-selected
            if key not in keys:
                self._wiz.drop_session(key)
                self._hosts.pop(key).deleteLater()
        for row in checked:                            # add newly-selected (no read yet)
            if row.refdes not in self._hosts:
                state = self._wiz.create_session(row)
                self._hosts[row.refdes] = _RailHost(
                    state, self._wiz.read_or_reread, self._wiz.net_names())
        self._reorder(keys)
        self.refresh()
        if self._ic_list.count() and self._ic_list.currentRow() < 0:
            self._ic_list.setCurrentRow(0)

    def showEvent(self, ev) -> None:  # noqa: N802
        """Re-claim the chats back from P3 EVERY time P2 is shown. QWizard does NOT re-run
        initializePage on Back, so without this the chats (re-parented into P3's panels on the
        forward trip) stay there and P2's host layouts show empty."""
        super().showEvent(ev)
        for host in self._hosts.values():
            host.adopt_chat()
        self.refresh()

    def _reorder(self, keys: list) -> None:
        """Re-seat the list (checkable LABELS) + the stack (panels), RE-ADOPTING each chat
        into its host (it may have moved to P3). Clearing the LABEL list never deletes the
        host panels (they live in the stack) — robust to Back/Next, unlike setItemWidget."""
        while self._stack.count():
            self._stack.removeWidget(self._stack.widget(0))
        self._ic_list.blockSignals(True)
        self._ic_list.clear()
        self._hosts = {k: self._hosts[k] for k in keys if k in self._hosts}
        for key in keys:
            host = self._hosts[key]
            host.adopt_chat()                          # bring the chat back from P3 if needed
            self._stack.addWidget(host)
            it = QListWidgetItem(key)
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self._ic_list.addItem(it)
        self._ic_list.blockSignals(False)

    def _on_read_all(self) -> None:
        # Read only the CHECKED ICs (untick to skip an IC's read); per-IC "Read rail"
        # still reads one regardless.
        for i in range(self._ic_list.count()):
            it = self._ic_list.item(i)
            if it.checkState() != Qt.CheckState.Checked:
                continue
            key = it.data(Qt.ItemDataRole.UserRole)
            if key in self._hosts:
                self._wiz.start_rail_read(key)
        self.refresh()

    def _on_select(self, idx: int) -> None:
        if 0 <= idx < self._stack.count():
            self._stack.setCurrentIndex(idx)

    def on_rail_update(self, key: str) -> None:
        host = self._hosts.get(key)
        state = self._wiz.sessions.get(key)
        if host is not None and state is not None:
            host.set_rail(state.row.rail)              # fill the field from the fresh read
        self.refresh()

    def _badge_of(self, state: _IcState) -> str:
        slot = self._wiz._scheduler.state_of(state.key)
        if slot is SlotState.RUNNING:
            return "running"
        if slot is SlotState.QUEUED:
            return "queued"
        if state.phase in (_RAILED, _RAIL_FAILED):
            return state.phase
        return _PENDING

    def refresh(self) -> None:
        counts: dict[str, int] = {}
        for i, (key, host) in enumerate(self._hosts.items()):
            state = self._wiz.sessions.get(key)
            if state is None:
                continue
            badge = self._badge_of(state)
            counts[badge] = counts.get(badge, 0) + 1
            icon, label = _BADGE.get(badge, ("•", badge))
            if i < self._ic_list.count():
                rail = state.row.rail or "…"
                hint = f"  ({state.probe_hint})" if state.probe_hint else ""
                self._ic_list.item(i).setText(
                    f"{i + 1}. {icon} {key}  {state.row.part} → {rail}{hint}")
            host.status.setText(label)
            host.reread_btn.setText("↻ Re-read" if state.rail_read_started else "🔌 Read rail")
        order = ["running", "queued", _RAILED, _RAIL_FAILED]
        self._summary.setText("   ".join(
            f"{_BADGE[k][0]} {counts[k]} {_BADGE[k][1]}" for k in order if counts.get(k)))

    def isComplete(self) -> bool:  # noqa: N802
        """Next once EVERY checked IC has a rail (read or typed)."""
        any_checked = False
        for i in range(self._ic_list.count()):
            it = self._ic_list.item(i)
            if it.checkState() != Qt.CheckState.Checked:
                continue
            any_checked = True
            host = self._hosts.get(it.data(Qt.ItemDataRole.UserRole))
            if host is None or not host.rail_value():
                return False
        return any_checked

    def validatePage(self) -> bool:  # noqa: N802
        """Apply each picked IC's (possibly-edited) rail and hand the picked set on."""
        picked = []
        for i in range(self._ic_list.count()):
            it = self._ic_list.item(i)
            if it.checkState() != Qt.CheckState.Checked:
                continue
            key = it.data(Qt.ItemDataRole.UserRole)
            state = self._wiz.sessions.get(key)
            host = self._hosts.get(key)
            if state is None or host is None:
                continue
            state.row.rail = host.rail_value()
            state.phase = _RAILED
            picked.append(state.row)
        self._wiz.picked = picked
        return True

    def shutdown(self) -> None:
        pass   # sessions are wizard-owned; the wizard tears them down


# =========================================================================== #
# P3 — Build the tests (re-parents the P2 sessions in)                         #
# =========================================================================== #

class _BuildPage(QWizardPage):
    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("3 · Build the tests")
        self.setSubTitle("Set the common instructions, then 🔨 Build all. Validate / abandon "
                         "each (switchable until Finish).")
        self._panels: dict[str, _IcPanel] = {}
        top = QHBoxLayout()
        self._build_btn = QPushButton("🔨 Build all")
        self._build_btn.setToolTip("Launch the not-yet-built tests with the common instructions.")
        self._build_btn.clicked.connect(self._on_build_all)
        top.addWidget(self._build_btn)
        top.addWidget(QLabel("Parallel builds:"))
        self._cap_spin = QSpinBox()
        self._cap_spin.setRange(1, _PARALLEL_MAX)
        self._cap_spin.setValue(wiz._scheduler.capacity)
        self._cap_spin.valueChanged.connect(self._wiz._on_cap)
        top.addWidget(self._cap_spin)
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{theme.muted_color()};")
        top.addWidget(self._summary, 1)
        cm_row = QHBoxLayout()
        cm_row.addWidget(QLabel("Common instructions for all builds:"))
        self._common = QLineEdit()
        self._common.setPlaceholderText(
            "shared answer prepended to every build — e.g. use P4/P2 as the PSU "
            "connectors, 10 A limit")
        cm_row.addWidget(self._common, 1)
        split = QSplitter(Qt.Orientation.Horizontal)
        self._ic_list = QListWidget()
        self._ic_list.currentRowChanged.connect(self._on_select)
        list_col = QWidget()
        lcl = QVBoxLayout(list_col)
        lcl.setContentsMargins(0, 0, 0, 0)
        lcl.addWidget(_add_list_controls(self._ic_list))
        lcl.addWidget(self._ic_list, 1)
        split.addWidget(list_col)
        self._stack = QStackedWidget()
        split.addWidget(self._stack)
        split.setSizes([240, 600])
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(cm_row)
        lay.addWidget(split, 1)

    def initializePage(self) -> None:  # noqa: N802
        picked = list(self._wiz.picked)
        keys = [r.refdes for r in picked]
        for key in list(self._panels):
            if key not in keys:
                self._drop_panel(key)
        for row in picked:
            if row.refdes not in self._panels:
                self._add_panel(row.refdes)
        self._reorder(keys)
        self._sync_cap()
        self.refresh()
        if self._ic_list.count() and self._ic_list.currentRow() < 0:
            self._ic_list.setCurrentRow(0)

    def showEvent(self, ev) -> None:  # noqa: N802
        """Re-claim the chats back from P2 every time P3 is shown (QWizard skips initializePage
        on Back — the same gap as P2)."""
        super().showEvent(ev)
        for panel in self._panels.values():
            panel.adopt_chat()
        self.refresh()

    def _sync_cap(self) -> None:
        self._cap_spin.blockSignals(True)
        self._cap_spin.setValue(self._wiz._scheduler.capacity)
        self._cap_spin.blockSignals(False)

    def _add_panel(self, key: str) -> None:
        state = self._wiz.sessions.get(key)
        if state is None:
            return
        panel = _IcPanel(state)            # re-parents the chat into this panel
        state.panel = panel
        panel.validate_btn.clicked.connect(lambda _=False, k=key: self._on_validate(k))
        panel.abandon_btn.clicked.connect(lambda _=False, k=key: self._on_abandon(k))
        self._panels[key] = panel

    def _drop_panel(self, key: str) -> None:
        panel = self._panels.pop(key, None)
        if panel is None:
            return
        state = self._wiz.sessions.get(key)
        if state is not None:              # keep the SESSION alive; just un-panel it
            state.chat.setParent(self._wiz._session_holder)
            state.panel = None
        panel.deleteLater()

    def _reorder(self, keys: list) -> None:
        while self._stack.count():
            self._stack.removeWidget(self._stack.widget(0))
        self._ic_list.clear()
        self._panels = {k: self._panels[k] for k in keys if k in self._panels}
        for key in keys:
            if key in self._panels:
                self._panels[key].adopt_chat()     # re-adopt the chat back from P2's host
                self._stack.addWidget(self._panels[key])
                self._ic_list.addItem(QListWidgetItem(key))

    def _on_select(self, idx: int) -> None:
        if 0 <= idx < self._stack.count():
            self._stack.setCurrentIndex(idx)

    def _on_build_all(self) -> None:
        self._wiz.common_message = self._common.text().strip()
        for key in self._panels:
            state = self._wiz.sessions.get(key)
            if state and state.row.rail and not state.test_block \
                    and state.phase not in _TERMINAL:
                self._wiz.request_build(key)
        self.refresh()

    def _on_validate(self, key: str) -> None:
        state = self._wiz.sessions.get(key)
        if state is None or not state.test_block:
            return
        state.phase = _ACCEPTED
        self._wiz.accepted[key] = (state.row, state.test_block)
        if state.panel:
            state.panel.status.setText("Accepted — Abandon to drop it, or refine + re-Validate.")
        self.refresh()
        self.completeChanged.emit()

    def _on_abandon(self, key: str) -> None:
        state = self._wiz.sessions.get(key)
        if state is None:
            return
        state.phase = _ABANDONED
        state.build_pending = False
        self._wiz._scheduler.cancel(key)
        if state.chat.is_busy:
            state.chat.stop()
        self._wiz.accepted.pop(key, None)
        if state.panel:
            state.panel.status.setText("Abandoned — Validate to re-accept (if a test exists).")
        self.refresh()
        self.completeChanged.emit()

    def _badge_of(self, state: _IcState) -> str:
        if state.phase in _TERMINAL:
            return state.phase
        slot = self._wiz._scheduler.state_of(state.key)
        if slot is SlotState.RUNNING:
            return "running"
        if slot is SlotState.QUEUED:
            return "queued"
        return state.phase

    def refresh(self) -> None:
        counts: dict[str, int] = {}
        for i, (key, panel) in enumerate(self._panels.items()):
            state = self._wiz.sessions.get(key)
            if state is None:
                continue
            badge = self._badge_of(state)
            counts[badge] = counts.get(badge, 0) + 1
            icon, label = _BADGE.get(badge, ("•", badge))
            r = state.row
            if i < self._ic_list.count():
                self._ic_list.item(i).setText(f"{icon} {key}  {r.part} → {r.rail}  [{label}]")
            panel.validate_btn.setEnabled(bool(state.test_block))
        order = ["running", "queued", _RAILED, _NEEDS_INPUT, _READY, _ACCEPTED, _ABANDONED]
        self._summary.setText("   ".join(
            f"{_BADGE[k][0]} {counts[k]} {_BADGE[k][1]}" for k in order if counts.get(k)))

    def isComplete(self) -> bool:  # noqa: N802
        return bool(self._panels) and all(
            self._wiz.sessions[k].phase in _TERMINAL
            for k in self._panels if k in self._wiz.sessions)

    def shutdown(self) -> None:
        pass   # sessions are wizard-owned


# =========================================================================== #
# P4 — Create / update + Finish                                                #
# =========================================================================== #

class _CreatePage(QWizardPage):
    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("4 · Add to the project")
        self.setSubTitle("Create each accepted test as a new test, or update an "
                         "existing one. Finish writes them all.")
        self._rows: list[dict] = []
        self._box = QGroupBox("Accepted tests")
        self._box_lay = QVBoxLayout(self._box)
        lay = QVBoxLayout(self)
        lay.addWidget(self._box)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self._status)

    def initializePage(self) -> None:  # noqa: N802
        while self._box_lay.count():
            item = self._box_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._rows = []
        self._status.setText("")
        self._existing = list_existing_tests(self._wiz.target())
        accepted = list(self._wiz.accepted.values())
        if not accepted:
            self._box_lay.addWidget(QLabel("No accepted tests. Go back and "
                                           "validate at least one."))
            return
        for row, block in accepted:
            self._add_row(row, block)

    def _add_row(self, row: IcRow, block: str) -> None:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel(f"{row.refdes} → {row.rail}"), 1)
        combo = QComboBox()
        combo.addItem(_NEW_TEST)
        combo.addItems(self._existing)
        h.addWidget(combo)
        name = QLineEdit(f"PSU - {row.rail}")
        h.addWidget(name, 1)
        warn = QLabel("")
        warn.setStyleSheet("color:#c0392b; font-size:9pt;")
        h.addWidget(warn)
        rec = {"widget": w, "row": row, "block": block,
               "combo": combo, "name": name, "warn": warn}
        combo.currentIndexChanged.connect(lambda *_: self._on_row_changed(rec))
        name.textChanged.connect(lambda *_: self._on_row_changed(rec))
        self._rows.append(rec)
        self._box_lay.addWidget(w)
        self._on_row_changed(rec)

    def _on_row_changed(self, rec: dict) -> None:
        is_new = rec["combo"].currentIndex() == 0
        rec["name"].setVisible(is_new)
        warn = ""
        if is_new and rec["name"].text().strip() in self._existing:
            warn = "name exists → will disambiguate"
        rec["warn"].setText(warn)

    def validatePage(self) -> bool:  # noqa: N802
        results, ok = [], True
        prog = QProgressDialog("Creating tests…", None, 0, len(self._rows), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.show()
        for i, rec in enumerate(self._rows):
            row, block = rec["row"], rec["block"]
            prog.setLabelText(
                f"Creating test {i+1}/{len(self._rows)}: "
                + (rec["name"].text().strip() or ("PSU - " + rec["row"].rail)))
            prog.setValue(i)
            QApplication.processEvents()
            if rec["combo"].currentIndex() == 0:
                res = materialize_test(self._wiz.target(), rec["name"].text().strip()
                                       or f"PSU - {row.rail}", block)
            else:
                res = update_existing_test(self._wiz.target(),
                                           rec["combo"].currentText(), block)
            ok = ok and res.created
            colour = theme.chat_assistant_border() if res.created else "#c0392b"
            results.append(f'<div style="color:{colour};">{html.escape(res.message)}</div>')
        prog.setValue(len(self._rows))
        self._status.setText("".join(results))
        return ok

    def isComplete(self) -> bool:  # noqa: N802
        return True


# =========================================================================== #
# The wizard (COORDINATOR: owns the scheduler + per-IC sessions + routing)     #
# =========================================================================== #

class DcdcWizardDialog(QWizard):
    """Modeless paged DCDC test wizard (Classify → Rail-ID → Build → Create).

    The per-IC ``SkillChatWidget`` is created on P2 (rail-read) and reused on P3
    (build); this wizard owns it, the shared scheduler, and the signal routing — the
    pages are views over that shared state."""

    def __init__(self, main_window, parent=None) -> None:
        super().__init__(parent)
        self._mw = main_window
        self.backend_factory = getattr(main_window, "backend_factory", None)
        self.project_root = self._resolve_project_root()

        wizards = {w.skill_id: w for w in registry.load_wizards(self.project_root)}
        self.classifier = wizards.get("dcdc_classifier")
        self.authoring = wizards.get("dcdc_authoring")
        try:
            self.sources, self.documents_dir = skill_menu._build_sources(
                self._mw, self.project_root)
        except Exception:  # noqa: BLE001
            log.exception("dcdc-wizard could not build context sources")
            self.sources, self.documents_dir = [], None

        # Shared state + per-IC sessions.
        self._scheduler = ConcurrencyScheduler(_load_saved_cap())
        self.sessions: dict[str, _IcState] = {}
        self._net_names: Optional[list] = None   # lazy board net list (P2 rail dropdown)
        self._session_holder = QWidget()         # hidden parent for chats before P3
        self._session_holder.hide()
        self.checked: list = []                  # P1 → P2 (classified + checked)
        self.context: str = ""                   # P1 picker context, pushed to all
        self.picked: list = []                   # P2 → P3 (rail-confirmed + picked)
        self.common_message: str = ""            # appended to EVERY build's priming
        self.accepted: dict = {}                 # refdes -> (IcRow, test_block)

        self.setWindowTitle("DCDC test wizard")
        self.setModal(False)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.IndependentPages, False)
        self.resize(940, 780)

        self._classify = _ClassifyPage(self)
        self._rail = _RailPage(self)
        self._build = _BuildPage(self)
        self._create = _CreatePage(self)
        for p in (self._classify, self._rail, self._build, self._create):
            self.addPage(p)

    # -- per-IC session lifecycle --------------------------------------------

    def create_session(self, row: IcRow) -> _IcState:
        """Mint the per-IC chat (parked in the hidden holder) wired to the shared
        scheduler + the wizard's signal routing. Idempotent per refdes."""
        if row.refdes in self.sessions:
            return self.sessions[row.refdes]
        chat = SkillChatWidget(
            [self.authoring] if self.authoring else [],
            self.backend_factory,
            sources=None,
            backend_tab_id=f"dcdc_v2_{row.refdes}",
            show_skill_selector=False,
            show_run_button=False,
            parent=self._session_holder,
            dispatch_gate=(lambda fire, *, interactive, k=row.refdes:
                           self._gate(k, fire, interactive)),
        )
        chat.set_skill(self.authoring)
        chat.set_pushed_context(self.context)
        state = _IcState(row, chat)
        self.sessions[row.refdes] = state
        chat.busy_changed.connect(lambda b, k=row.refdes: self._on_busy(k, b))
        chat.reply_finished.connect(lambda t, k=row.refdes: self._on_reply(k, t))
        chat.reply_failed.connect(lambda r, k=row.refdes: self._on_failed(k, r))
        chat.conversation_reset.connect(lambda k=row.refdes: self._on_chat_reset(k))
        return state

    def read_or_reread(self, key: str) -> None:
        """The per-IC button: the FIRST press kicks off this IC's rail-read (deterministic
        run_kickoff); after that it re-asks via a follow-up turn. This is also the path that
        re-initiates a chat the operator trashed (reset clears rail_read_started)."""
        state = self.sessions.get(key)
        if state is None:
            return
        (self.reask_rail if state.rail_read_started else self.start_rail_read)(key)

    def start_rail_read(self, key: str) -> None:
        state = self.sessions.get(key)
        if state is None or state.chat.is_busy \
                or self._scheduler.state_of(key) is not SlotState.IDLE:
            return    # already reading/queued — is_busy is False while merely QUEUED
        state.awaiting = "rail"
        state.phase = _PENDING
        state.rail_read_started = True
        common = self._rail._common.text().strip() if self._rail is not None else ""
        state.chat.run_kickoff(priming=_rail_priming(state.row, common))

    def reask_rail(self, key: str) -> None:
        """Re-run the rail-read (operator flagged it). Uses any text the operator
        typed in the rail field as a hint."""
        state = self.sessions.get(key)
        if state is None or state.chat.is_busy \
                or self._scheduler.state_of(key) is not SlotState.IDLE:
            return    # a read is already running or queued — don't double-submit
        hint = ""
        rr = self._rail._hosts.get(key)
        if rr is not None:
            hint = rr.rail_value()
        state.awaiting = "rail"
        state.phase = _PENDING
        msg = (f"Re-read the output rail of {state.row.refdes}. Double-check +Vin vs "
               f"+Vout (the operator flagged the rail as possibly wrong).")
        if hint:
            msg += f" The operator suggests it may be `{hint}` — verify."
        state.chat.send_user_turn(msg)
        self._rail.refresh()

    def _on_chat_reset(self, key: str) -> None:
        """A per-IC chat was TRASHED (fresh session) — reset this IC's wizard state to match,
        so its next action is a clean '🔌 Read rail' again (no stale rail / phase / build)."""
        state = self.sessions.get(key)
        if state is None:
            return
        self._scheduler.cancel(key)            # drop any queued fire for this IC
        state.rail_read_started = False
        state.awaiting = None
        state.phase = _PENDING
        state.test_block = ""
        state.build_pending = False
        state.row.rail = ""
        rr = self._rail._hosts.get(key) if self._rail is not None else None
        if rr is not None:
            rr.rail_combo.setCurrentText("")
        self._refresh_pages()

    def request_build(self, key: str) -> None:
        """Fire turn-2 build now, or defer it (``build_pending``) if the turn-1
        rail-read is still in flight — the deferred fire happens on its completion."""
        state = self.sessions.get(key)
        if state is None or state.phase in _TERMINAL or state.test_block:
            return
        if state.chat.is_busy or self._scheduler.state_of(key) is not SlotState.IDLE:
            state.build_pending = True
        else:
            self._fire_build(key)

    def _fire_build(self, key: str) -> None:
        state = self.sessions.get(key)
        if state is None:
            return
        state.awaiting = "build"
        state.chat.send_user_turn(_build_priming(state.row, self.common_message))

    def _maybe_fire_pending_build(self, key: str) -> None:
        state = self.sessions.get(key)
        if (state and state.build_pending and state.phase == _RAILED
                and state.awaiting is None and not state.chat.is_busy
                and self._scheduler.state_of(key) is SlotState.IDLE):
            state.build_pending = False
            self._fire_build(key)

    def drop_session(self, key: str) -> None:
        """Tear ONE session down fully (de-selected on P1, or close)."""
        state = self.sessions.pop(key, None)
        if state is None:
            return
        # If it reached P3, detach its panel FIRST (the chat is the panel's child — pull
        # it out so deleting the panel can't double-delete the chat).
        if state.panel is not None:
            state.chat.setParent(self._session_holder)
            self._build._panels.pop(key, None)
            state.panel.deleteLater()
            state.panel = None
        for sig in (state.chat.busy_changed, state.chat.reply_finished,
                    state.chat.reply_failed, state.chat.conversation_reset):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._scheduler.cancel(key)        # drop a QUEUED fire
        if state.chat.is_busy:
            state.chat.stop()              # stop a RUNNING worker
        self._scheduler.complete(key)      # free its slot — busy_changed is now
                                           # disconnected, so the normal complete() never runs
        try:
            state.chat.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("dcdc-wizard: session shutdown failed")
        state.chat.deleteLater()

    # -- scheduler gate + signal routing -------------------------------------

    def _gate(self, key: str, fire, interactive: bool) -> None:
        self._scheduler.submit(key, fire, priority=interactive)
        self._refresh_pages()

    def _on_busy(self, key: str, busy: bool) -> None:
        if not busy:
            self._scheduler.complete(key)
            self._maybe_fire_pending_build(key)
        self._refresh_pages()

    def _is_rail_turn(self, state: _IcState) -> bool:
        """Which reply to expect. The explicit fires set ``awaiting``; a reply to a message
        the operator TYPED (awaiting cleared) is routed by the active page — typing in the
        P2 chat is a rail re-read, typing in the P3 chat is a build refine."""
        if state.awaiting == "rail":
            return True
        if state.awaiting == "build":
            return False
        return self.currentPage() is self._rail

    def _on_reply(self, key: str, text: str) -> None:
        state = self.sessions.get(key)
        if state is None:
            return
        rail_turn = self._is_rail_turn(state)
        state.awaiting = None
        if rail_turn:
            # ignore a LATE auto-read that landed after the operator advanced off P2 (typed
            # a rail + Next) — but an active P2 re-ask/re-type IS honoured even when RAILED
            if state.phase not in _RAIL_PHASES and self.currentPage() is not self._rail:
                return
            rail = parse_rail_reply(text)
            state.row.rail = rail or state.row.rail
            state.probe_hint = parse_rail_probe(text)   # display-only hint (generic netname)
            state.phase = _RAILED if rail else _RAIL_FAILED
            self._maybe_fire_pending_build(key)
            self._rail.on_rail_update(key)
        else:  # build (turn 2)
            block = find_dcdc_test_block(text)
            if block:
                state.test_block = block   # a refine updates the test even post-decision
                if state.panel is not None:
                    state.panel.test_view.setPlainText(block)
                if state.phase not in _TERMINAL:
                    state.phase = _READY
            elif state.phase not in _TERMINAL:
                state.phase = _NEEDS_INPUT
            if state.phase not in _TERMINAL:
                _alert()               # don't beep on an already-decided session
            self._build.refresh()

    def _on_failed(self, key: str, reason: str) -> None:
        state = self.sessions.get(key)
        if state is not None:
            if state.awaiting == "rail" and state.phase in _RAIL_PHASES:
                state.phase = _RAIL_FAILED      # the rail-read failed → operator must fix it
                state.build_pending = False     # no rail → nothing to auto-build
            state.awaiting = None               # never strand the router on a dead turn
            if state.panel is not None:
                state.panel.status.setText(reason)
        self._refresh_pages()

    def _on_cap(self, n: int) -> None:
        try:
            self._scheduler.set_capacity(n)
        except ValueError:           # can't lower while fires are in flight
            for sp in (self._rail._cap_spin, self._build._cap_spin):
                sp.blockSignals(True)
                sp.setValue(self._scheduler.capacity)
                sp.blockSignals(False)
            return
        # keep both pages' spinboxes in sync
        for sp in (self._rail._cap_spin, self._build._cap_spin):
            if sp.value() != n:
                sp.blockSignals(True)
                sp.setValue(n)
                sp.blockSignals(False)
        _save_cap(self._scheduler.capacity)        # remember it across runs
        self._refresh_pages()

    def _refresh_pages(self) -> None:
        for pg in (self._rail, self._build):
            try:
                pg.refresh()
            except Exception:  # noqa: BLE001
                log.exception("dcdc-wizard: page refresh failed")

    # -- misc ----------------------------------------------------------------

    def _resolve_project_root(self):
        pm = getattr(self._mw, "project_manager", None)
        return getattr(pm, "project_root", None)

    def target(self):
        return getattr(self._mw, "project_manager", None) or self.project_root

    def net_names(self) -> list:
        """All board net names (lazy-loaded once) for the P2 rail dropdown — constrains the
        operator's manual rail choice to real nets. Empty on any failure (the combobox then
        behaves as a plain free-text field)."""
        if self._net_names is None:
            self._net_names = []
            try:
                from ..core import odb_inspect
                board = odb_inspect.load_board(self.project_root)
                self._net_names = sorted(
                    {(n.get("net") or "").strip() for n in (board.get("nets") or [])}
                    - {""})
            except Exception:  # noqa: BLE001
                log.exception("dcdc-wizard: net-list load failed")
        return self._net_names

    def _teardown_all(self) -> None:
        """Idempotent teardown of EVERY embedded chat (classifier + per-IC sessions) so no
        private OpenCode session leaks. Runs on BOTH closeEvent (the X) AND done()
        (Finish/Cancel) — a modeless QWizard's done() does NOT dispatch a closeEvent."""
        if getattr(self, "_torn", False):
            return
        self._torn = True
        try:
            self._classify.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("dcdc-wizard: classify shutdown failed")
        for key in list(self.sessions):
            self.drop_session(key)

    def done(self, result):  # noqa: N802 — Finish / Cancel (no closeEvent fires)
        self._teardown_all()
        super().done(result)

    def closeEvent(self, event):  # noqa: N802
        self._teardown_all()
        super().closeEvent(event)
