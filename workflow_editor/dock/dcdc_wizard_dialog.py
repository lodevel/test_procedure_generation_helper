"""DCDC test wizard — paged QWizard with parallel per-IC builds.

Three pages, Next → Next → Finish:

  P1 Find   — the finder ``SkillChatWidget`` (with its context picker) on the
              right; a CHECKBOX list of the power ICs it found on the left. Next
              unlocks once ≥1 IC is checked.
  P2 Build  — one build chat PER checked IC (the same ``SkillChatWidget``, picker
              hidden, P1's chosen context inherited). All builds run at once,
              CAPPED by a concurrency scheduler: every fire — an initial build OR
              the user's answer to a question — goes through the cap, so live LLM
              streams never exceed it; the rest queue. Per-IC status badge
              (queued / running / needs-input / ready / accepted / abandoned) +
              a sound on needs-action, Validate / Abandon, and a read-only test
              view. Next unlocks once every IC is accepted or abandoned.
  P3 Create — per ACCEPTED test: "New test" (name, collision warning) or update
              an EXISTING test. Finish materializes them all. No chat.

The chat panels reuse :class:`SkillChatWidget` via its ``dispatch_gate`` seam;
all non-Qt logic (list parse, done-signal, validate, materialize, the scheduler)
lives in tested pure leaves under ``authoring/``.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QComboBox, QSpinBox,
    QTextEdit, QSplitter, QStackedWidget, QWidget, QGroupBox,
)

from .. import theme
from ..authoring import registry, skill_menu
from ..authoring.wizard.list_parse import parse_finder_list, IcRow
from ..authoring.wizard.done_signal import find_dcdc_test_block
from ..authoring.wizard.validate import validate_params, OdbBoardData, Check
from ..authoring.wizard.scheduler import ConcurrencyScheduler, SlotState
from ..authoring.wizard import finder_cache
from ..authoring.test_materializer import (
    materialize_test, update_existing_test, list_existing_tests,
)
from .skill_chat_widget import SkillChatWidget

log = logging.getLogger(__name__)

_PARALLEL_DEFAULT = 5
_PARALLEL_MAX = 16
_NEW_TEST = "➕ New test"

# Settled per-IC phases (the scheduler supplies the transient running/queued).
_PENDING = "pending"
_NEEDS_INPUT = "needs_input"
_READY = "ready"
_ACCEPTED = "accepted"
_ABANDONED = "abandoned"
_TERMINAL = (_ACCEPTED, _ABANDONED)

_BADGE = {
    "running": ("🟢", "running"),
    "queued": ("⏳", "queued"),
    _PENDING: ("•", "pending"),
    _NEEDS_INPUT: ("❓", "needs input"),
    _READY: ("📋", "ready"),
    _ACCEPTED: ("✅", "accepted"),
    _ABANDONED: ("🚫", "abandoned"),
}


def _extract_test_point(block: str) -> str:
    """Scrape the rail's scope-probe designator (scope CH1) from the generated test.

    A board's test points are NOT necessarily ``TP*``: this board designates them by
    the rail name (e.g. ``+AUX0_16V``), and the step reads ``Connect SCOPE1 CH1 to TP
    +AUX0_16V`` — where ``TP`` is the WORD and ``+AUX0_16V`` is the designator. So
    capture the token after the CH1 scope hookup's optional ``TP `` word, NOT a
    ``TP\\w+`` literal. Empty when none found (the check then reports 'not provided'
    rather than false-failing on a parse miss)."""
    block = block or ""
    m = re.search(r"SCOPE\w*\s+CH1\s+to\s+(?:TP\s+)?([^\s(]+)", block, re.IGNORECASE)
    if not m:  # fallback: any scope channel's probe hookup
        m = re.search(r"SCOPE\w*\s+CH\d+\s+to\s+(?:TP\s+)?([^\s(]+)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _priming(row: IcRow) -> str:
    """The first-turn message that hands the authoring skill its one chosen IC."""
    return f"{row.refdes} — {row.part} ({row.kind}) → {row.rail}"


def _alert() -> None:
    """A short audible cue when a build needs the operator (needs-input / ready),
    so they can be elsewhere. ``QApplication.beep`` is the no-dependency path."""
    try:
        QApplication.beep()
    except Exception:  # noqa: BLE001 — a missing bell must never break the wizard
        pass


# =========================================================================== #
# Per-IC build panel (a dumb container; the BuildPage owns the logic)         #
# =========================================================================== #

class _IcPanel(QWidget):
    """One checked IC's build surface: its chat + a read-only test view +
    Validate / Abandon. The chat is a ``SkillChatWidget`` with NO picker (P1's
    context is pushed in) whose firing is gated by the page's scheduler."""

    def __init__(self, row: IcRow, chat: SkillChatWidget, parent=None):
        super().__init__(parent)
        self.row = row
        self.key = row.refdes
        self.chat = chat
        self.phase = _PENDING
        self.test_block = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(chat, stretch=1)

        self.test_view = QTextEdit()
        self.test_view.setReadOnly(True)
        self.test_view.setPlaceholderText("The generated test appears here once built.")
        self.test_view.setMaximumHeight(170)
        lay.addWidget(self.test_view)

        row_btns = QHBoxLayout()
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        row_btns.addWidget(self.status, 1)
        self.validate_btn = QPushButton("✅ Validate")
        self.validate_btn.setEnabled(False)
        self.abandon_btn = QPushButton("🚫 Abandon")
        row_btns.addWidget(self.validate_btn)
        row_btns.addWidget(self.abandon_btn)
        lay.addLayout(row_btns)


# =========================================================================== #
# P1 — Find power ICs                                                          #
# =========================================================================== #

class _FindPage(QWizardPage):
    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("1 · Find power ICs")
        self.setSubTitle("Run the finder, then tick the ICs you want tests for. "
                         "Include the netlist in the chat's context picker.")

        split = QSplitter(Qt.Orientation.Horizontal)

        # Left — the checkbox IC list.
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Power ICs (tick the ones to build):"))
        self._ic_list = QListWidget()
        self._ic_list.itemChanged.connect(lambda *_: self.completeChanged.emit())
        ll.addWidget(self._ic_list, 1)
        split.addWidget(left)

        # Right — the finder chat + a Search button.
        right = QWidget()
        rl = QVBoxLayout(right)
        self.chat = SkillChatWidget(
            [wiz.finder, wiz.authoring] if wiz.finder else [],
            wiz.backend_factory,
            sources=wiz.sources,
            documents_dir=wiz.documents_dir,
            backend_tab_id="dcdc_finder",
            show_skill_selector=False,
            show_run_button=False,
        )
        rl.addWidget(self.chat, 1)
        btn_row = QHBoxLayout()
        self._find_btn = QPushButton("🔍 Search ICs")
        self._find_btn.clicked.connect(self._on_search)
        btn_row.addWidget(self._find_btn)
        self._restart_btn = QPushButton("♻️ Restart analysis")
        self._restart_btn.setToolTip("Wipe the cached IC list and re-run the finder.")
        self._restart_btn.clicked.connect(self._on_restart)
        btn_row.addWidget(self._restart_btn)
        rl.addLayout(btn_row)
        split.addWidget(right)
        split.setSizes([300, 540])

        lay = QVBoxLayout(self)
        lay.addWidget(split)
        cm_row = QHBoxLayout()
        cm_row.addWidget(QLabel("Common instructions for all builds:"))
        self._common = QLineEdit()
        self._common.setPlaceholderText(
            "shared answer prepended to every build — e.g. use P4/P2 as the PSU "
            "connectors, 10 A limit")
        cm_row.addWidget(self._common, 1)
        lay.addLayout(cm_row)

        self.chat.reply_finished.connect(self._on_reply)
        if not (wiz.finder and wiz.authoring):
            self._find_btn.setEnabled(False)
            self.chat.set_status("Wizard skills not found (need dcdc_finder + "
                                 "dcdc_authoring under authoring_wizards/).")

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        # On open, show the CACHED IC list (when the board is unchanged) so the slow
        # finder needn't re-run; the operator re-picks + rebuilds. Only when nothing
        # is populated yet (a fresh session) — never clobber a live search.
        if self._ic_list.count() == 0:
            cached = finder_cache.load(self._wiz.project_root)
            if cached:
                self._populate(cached)
                self.chat.set_status(
                    f"Loaded {len(cached)} cached IC(s) — 'Restart analysis' to refresh.")

    def _on_search(self) -> None:
        if self.chat.is_busy:
            return
        self._ic_list.clear()
        self.chat.set_skill(self._wiz.finder)   # fresh session each search
        self.chat.set_status("Finding power ICs…")
        self.chat.run_kickoff()

    def _on_restart(self) -> None:
        """Wipe the cached list and re-run the finder from scratch."""
        finder_cache.clear(self._wiz.project_root)
        self._on_search()

    def _populate(self, rows: list) -> None:
        self._ic_list.clear()
        for r in rows:
            it = QListWidgetItem(f"{r.refdes} — {r.part} ({r.kind}) → {r.rail}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)   # default ON; untick to skip
            it.setData(Qt.ItemDataRole.UserRole, r)
            self._ic_list.addItem(it)
        self.completeChanged.emit()

    def _on_reply(self, text: str) -> None:
        rows = parse_finder_list(text)
        self._populate(rows)
        if rows:
            finder_cache.save(self._wiz.project_root, rows)   # cache for next open
        self.chat.set_status(
            f"Found {len(rows)} power IC(s) — untick any you don't want, then Next."
            if rows else "No power ICs parsed — refine in the chat and search again.")

    def checked_rows(self) -> list:
        out = []
        for i in range(self._ic_list.count()):
            it = self._ic_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def isComplete(self) -> bool:  # noqa: N802 — Qt override
        return bool(self.checked_rows())

    def validatePage(self) -> bool:  # noqa: N802 — Qt override
        # Hand the checked rows + the chat's chosen context to the wizard for P2.
        self._wiz.checked = self.checked_rows()
        self._wiz.context = self.chat.resolved_context()
        self._wiz.common_message = self._common.text().strip()
        return True

    def shutdown(self) -> None:
        self.chat.shutdown()


# =========================================================================== #
# P2 — Build (parallel, scheduler-gated)                                       #
# =========================================================================== #

class _BuildPage(QWizardPage):
    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("2 · Build the tests")
        self.setSubTitle("Builds run in parallel up to the cap; the rest queue. "
                         "Validate or abandon each — answers respect the cap too.")
        self._panels: dict[str, _IcPanel] = {}
        self._scheduler = ConcurrencyScheduler(_PARALLEL_DEFAULT)

        top = QHBoxLayout()
        top.addWidget(QLabel("Parallel builds:"))
        self._cap_spin = QSpinBox()
        self._cap_spin.setRange(1, _PARALLEL_MAX)
        self._cap_spin.setValue(_PARALLEL_DEFAULT)
        self._cap_spin.setToolTip("Max builds streaming at once. Raise any time; "
                                  "can't lower while builds are in flight.")
        self._cap_spin.valueChanged.connect(self._on_cap)
        top.addWidget(self._cap_spin)
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{theme.muted_color()};")
        top.addWidget(self._summary, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        self._ic_list = QListWidget()
        self._ic_list.currentRowChanged.connect(self._on_select)
        split.addWidget(self._ic_list)
        self._stack = QStackedWidget()
        split.addWidget(self._stack)
        split.setSizes([240, 600])

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)

    # -- lifecycle ------------------------------------------------------------

    def _priming_for(self, row: IcRow) -> str:
        """The IC's opening message + the shared 'common instructions' (e.g. P4/P2
        PSU connectors, 10 A limit) so every build gets the standard answers up
        front instead of each one stopping to ask."""
        base = _priming(row)
        cm = (self._wiz.common_message or "").strip()
        return f"{base}\n\n{cm}" if cm else base

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        """INCREMENTAL: a changed IC selection (e.g. Back to tick a forgotten IC)
        adds only the new builds and drops only the de-selected ones — every
        unaffected build keeps running and every prior accept survives. Same
        scheduler instance throughout, so kept builds keep their slots."""
        new_rows = list(self._wiz.checked)
        new_set = {r.refdes for r in new_rows}
        if set(self._panels) == new_set and self._panels:
            return  # selection unchanged → keep everything exactly as-is
        # Drop de-selected ICs only (frees their slots, forgets their accept).
        for key in list(self._panels):
            if key not in new_set:
                self._teardown_one(key)
                self._wiz.accepted.pop(key, None)
        # Add newly-checked ICs.
        added = [r for r in new_rows if r.refdes not in self._panels]
        for row in added:
            self._add_panel(row)
        self._rebuild_layout([r.refdes for r in new_rows])
        # Kick off ONLY the new builds; kept ones keep their in-flight state.
        for row in added:
            self._panels[row.refdes].chat.run_kickoff(priming=self._priming_for(row))
        self._refresh()
        if self._ic_list.count() and self._ic_list.currentRow() < 0:
            self._ic_list.setCurrentRow(0)

    def _rebuild_layout(self, order: list) -> None:
        """Re-seat the list + stack in checked ``order``, REUSING surviving panel
        widgets (removed ones were already deleteLater'd, so detaching them from
        the stack here is safe)."""
        while self._stack.count():
            self._stack.removeWidget(self._stack.widget(0))
        self._ic_list.clear()
        self._panels = {k: self._panels[k] for k in order}
        for key in order:
            self._stack.addWidget(self._panels[key])
            self._ic_list.addItem(QListWidgetItem(key))

    def _add_panel(self, row: IcRow) -> None:
        key = row.refdes
        chat = SkillChatWidget(
            [self._wiz.authoring],
            self._wiz.backend_factory,
            sources=None,                       # no picker: P1's context is pushed
            backend_tab_id=f"dcdc_build_{key}",
            show_skill_selector=False,
            show_run_button=False,
            dispatch_gate=(lambda fire, *, interactive, k=key:
                           self._gate(k, fire, interactive)),
        )
        chat.set_skill(self._wiz.authoring)
        chat.set_pushed_context(self._wiz.context)
        chat.set_input_placeholder("Answer the skill's question, then press Enter…")
        panel = _IcPanel(row, chat)
        panel.validate_btn.clicked.connect(lambda _=False, k=key: self._on_validate(k))
        panel.abandon_btn.clicked.connect(lambda _=False, k=key: self._on_abandon(k))
        chat.reply_finished.connect(lambda t, k=key: self._on_reply(k, t))
        chat.reply_failed.connect(lambda r, k=key: self._on_failed(k, r))
        chat.busy_changed.connect(lambda b, k=key: self._on_busy(k, b))
        self._panels[key] = panel  # layout is (re)built by _rebuild_layout

    # -- scheduler gate + completion -----------------------------------------

    def _gate(self, key: str, fire, interactive: bool) -> None:
        """Every fire (build or answer) lands here → the scheduler runs it now or
        queues it. Interactive answers jump ahead of not-yet-started builds."""
        self._scheduler.submit(key, fire, priority=interactive)
        self._refresh()

    def _on_busy(self, key: str, busy: bool) -> None:
        # busy True = pending|running (no slot action); False = turn done → free
        # the slot so the next queued turn fires. Errors flip busy→False too.
        if not busy:
            self._scheduler.complete(key)
        self._refresh()

    def _on_reply(self, key: str, text: str) -> None:
        panel = self._panels.get(key)
        if panel is None or panel.phase in _TERMINAL:
            return
        block = find_dcdc_test_block(text)
        if block:
            panel.test_block = block
            panel.test_view.setPlainText(block)
            panel.phase = _READY
            panel.validate_btn.setEnabled(True)
            panel.status.setText("Test ready — Validate or refine in the chat.")
        else:
            panel.phase = _NEEDS_INPUT
            panel.status.setText("The skill asked a question — answer it in the chat.")
        _alert()
        self._refresh()

    def _on_failed(self, key: str, reason: str) -> None:
        panel = self._panels.get(key)
        if panel is not None and panel.phase not in _TERMINAL:
            panel.status.setText(reason)
        self._refresh()

    # -- user actions ---------------------------------------------------------

    def _on_validate(self, key: str) -> None:
        panel = self._panels.get(key)
        if panel is None or not panel.test_block:
            return
        # Stop any queued/running refine so it can't run a turn AFTER acceptance
        # (it would burn a slot and mutate the now-accepted panel's session).
        self._scheduler.cancel(key)
        if panel.chat.is_busy:
            panel.chat.stop()
        panel.phase = _ACCEPTED
        self._wiz.accepted[key] = (panel.row, panel.test_block)
        self._lock_panel(panel)
        self._refresh()
        self.completeChanged.emit()

    def _on_abandon(self, key: str) -> None:
        panel = self._panels.get(key)
        if panel is None:
            return
        panel.phase = _ABANDONED
        self._scheduler.cancel(key)           # drop it if still queued
        if panel.chat.is_busy:
            panel.chat.stop()                 # stop a running turn → frees its slot
        self._wiz.accepted.pop(key, None)
        self._lock_panel(panel)
        self._refresh()
        self.completeChanged.emit()

    def _lock_panel(self, panel: _IcPanel) -> None:
        panel.validate_btn.setEnabled(False)
        panel.abandon_btn.setEnabled(False)
        panel.chat.setEnabled(False)

    # -- capacity -------------------------------------------------------------

    def _on_cap(self, n: int) -> None:
        try:
            self._scheduler.set_capacity(n)   # raises when lowering mid-flight
        except ValueError:
            self._cap_spin.blockSignals(True)  # snap back: can't lower while running
            self._cap_spin.setValue(self._scheduler.capacity)
            self._cap_spin.blockSignals(False)
            return
        self._refresh()

    # -- rendering ------------------------------------------------------------

    def _badge_of(self, panel: _IcPanel) -> str:
        if panel.phase in _TERMINAL:
            return panel.phase
        st = self._scheduler.state_of(panel.key)
        if st is SlotState.RUNNING:
            return "running"
        if st is SlotState.QUEUED:
            return "queued"
        return panel.phase

    def _refresh(self) -> None:
        counts: dict[str, int] = {}
        for i, (key, panel) in enumerate(self._panels.items()):
            badge = self._badge_of(panel)
            counts[badge] = counts.get(badge, 0) + 1
            icon, label = _BADGE.get(badge, ("•", badge))
            self._ic_list.item(i).setText(f"{icon} {key} — {label}")
        order = ["running", "queued", _NEEDS_INPUT, _READY, _ACCEPTED, _ABANDONED]
        parts = [f"{_BADGE[k][0]} {counts[k]} {_BADGE[k][1]}" for k in order if counts.get(k)]
        self._summary.setText("   ".join(parts))

    def _on_select(self, idx: int) -> None:
        if 0 <= idx < self._stack.count():
            self._stack.setCurrentIndex(idx)

    def isComplete(self) -> bool:  # noqa: N802 — Qt override
        return bool(self._panels) and all(
            p.phase in _TERMINAL for p in self._panels.values())

    # -- teardown -------------------------------------------------------------

    def _teardown_one(self, key: str) -> None:
        """Fully drop ONE panel: disconnect its signals FIRST (so a late worker
        signal — if a thread outlives the 2 s shutdown wait — can't run page logic
        against the rebuilt page), cancel/stop its build (freeing its slot), shut
        the chat down, and delete the widget. Used by incremental-remove + close."""
        panel = self._panels.pop(key, None)
        if panel is None:
            return
        for sig in (panel.chat.busy_changed, panel.chat.reply_finished,
                    panel.chat.reply_failed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._scheduler.cancel(key)            # drop it if still queued
        if panel.chat.is_busy:
            panel.chat.stop()                  # stop a running turn → frees its slot
        try:
            panel.chat.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("dcdc-wizard: build panel shutdown failed")
        panel.deleteLater()

    def _teardown_panels(self) -> None:
        """Full teardown (dialog close): drop every panel + reset the scheduler."""
        for key in list(self._panels):
            self._teardown_one(key)
        while self._stack.count():
            self._stack.removeWidget(self._stack.widget(0))
        self._ic_list.clear()
        self._scheduler = ConcurrencyScheduler(self._cap_spin.value())

    def shutdown(self) -> None:
        self._teardown_panels()


# =========================================================================== #
# P3 — Create / update + Finish                                                #
# =========================================================================== #

class _CreatePage(QWizardPage):
    def __init__(self, wiz: "DcdcWizardDialog"):
        super().__init__()
        self._wiz = wiz
        self.setTitle("3 · Add to the project")
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

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        # Clear any previous rendering fully (Back/Next may re-enter the page).
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

    def validatePage(self) -> bool:  # noqa: N802 — Qt override
        results, ok = [], True
        for rec in self._rows:
            row, block = rec["row"], rec["block"]
            if rec["combo"].currentIndex() == 0:
                res = materialize_test(self._wiz.target(), rec["name"].text().strip()
                                       or f"PSU - {row.rail}", block)
            else:
                res = update_existing_test(self._wiz.target(),
                                           rec["combo"].currentText(), block)
            ok = ok and res.created
            colour = theme.chat_assistant_border() if res.created else "#c0392b"
            results.append(f'<div style="color:{colour};">{html.escape(res.message)}</div>')
        self._status.setText("".join(results))
        return ok  # all good → close; any failure → stay open with the messages

    def isComplete(self) -> bool:  # noqa: N802 — Qt override
        return True


# =========================================================================== #
# The wizard                                                                   #
# =========================================================================== #

class DcdcWizardDialog(QWizard):
    """Modeless paged DCDC test wizard (Find → Build → Create)."""

    def __init__(self, main_window, parent=None) -> None:
        super().__init__(parent)
        self._mw = main_window
        self.backend_factory = getattr(main_window, "backend_factory", None)
        self.project_root = self._resolve_project_root()

        wizards = {w.skill_id: w for w in registry.load_wizards(self.project_root)}
        self.finder = wizards.get("dcdc_finder")
        self.authoring = wizards.get("dcdc_authoring")
        try:
            self.sources, self.documents_dir = skill_menu._build_sources(
                self._mw, self.project_root)
        except Exception:  # noqa: BLE001
            log.exception("dcdc-wizard could not build context sources")
            self.sources, self.documents_dir = [], None

        # Shared state across pages.
        self.checked: list = []
        self.context: str = ""
        self.common_message: str = ""   # appended to EVERY build's priming
        self.accepted: dict = {}   # refdes -> (IcRow, test_block)

        self.setWindowTitle("DCDC test wizard")
        self.setModal(False)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.IndependentPages, False)
        self.resize(920, 760)

        self._find = _FindPage(self)
        self._build = _BuildPage(self)
        self._create = _CreatePage(self)
        for p in (self._find, self._build, self._create):
            self.addPage(p)

    def _resolve_project_root(self):
        pm = getattr(self._mw, "project_manager", None)
        return getattr(pm, "project_root", None)

    def target(self):
        """The materialize target: the live ProjectManager (preferred) or the
        project root path."""
        return getattr(self._mw, "project_manager", None) or self.project_root

    def closeEvent(self, event):  # noqa: N802 — Qt override
        """Tear down EVERY embedded chat (finder + each build panel) so no private
        OpenCode session leaks — an embedded QWidget's own closeEvent never fires."""
        for page in (self._find, self._build):
            try:
                page.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("dcdc-wizard: page shutdown failed")
        super().closeEvent(event)
