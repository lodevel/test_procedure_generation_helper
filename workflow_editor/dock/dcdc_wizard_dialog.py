"""DCDC test wizard — Phase 1 single-IC vertical slice.

A CONCRETE Qt shell over the Qt-free wizard brain (``authoring/wizard/*`` +
``authoring/test_materializer``). The shell only renders state + forwards input;
all logic (list parsing, done-signal, validation, materialization) lives in the
pure leaf modules and is unit-tested without Qt.

Flow:
  Stage A  run ``dcdc_finder``               -> parse_finder_list -> pick ONE IC
  Stage B  run ``dcdc_authoring`` (primed)   -> find_dcdc_test_block (or answer a
                                                question over a few turns)
  Validate basic existence checks vs the netlist (ic_refdes / ic_part / TP)
  Stage C  name (``PSU - <rail>``) + materialize a REAL project test

The chat sub-panel IS the real skill chat: an embedded
:class:`~workflow_editor.dock.skill_chat_widget.SkillChatWidget` (the SAME widget
the Skills-menu *Skill chat* hosts), so the wizard inherits its transcript,
input, toggles, and Stop/Trash/Restart/timer/token controls. The wizard keeps
only its STAGE brain (find -> pick -> build -> validate -> create) and drives the
widget through ``set_skill`` / ``run_kickoff``, reacting to ``reply_finished``.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QLineEdit,
)

from .. import theme
from ..authoring import registry, skill_menu
from ..authoring.wizard.list_parse import parse_finder_list, IcRow
from ..authoring.wizard.done_signal import find_dcdc_test_block
from ..authoring.wizard.validate import validate_params, OdbBoardData, Check
from ..authoring.test_materializer import materialize_test
from .skill_chat_widget import SkillChatWidget

log = logging.getLogger(__name__)

# Stage of the wizard — routes the single ``reply_finished`` seam.
_STAGE_IDLE = "idle"
_STAGE_FIND = "find"
_STAGE_BUILD = "build"


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


class DcdcWizardDialog(QDialog):
    """Modeless single-IC DCDC test wizard (Phase 1)."""

    def __init__(self, main_window, parent=None) -> None:
        super().__init__(parent)
        self._mw = main_window
        self._backend_factory = getattr(main_window, "backend_factory", None)
        self._project_root = self._resolve_project_root()

        wizards = {w.skill_id: w for w in registry.load_wizards(self._project_root)}
        self._finder = wizards.get("dcdc_finder")
        self._authoring = wizards.get("dcdc_authoring")

        self._picked: Optional[IcRow] = None
        self._rows: list[IcRow] = []
        self._test_block: str = ""
        self._stage: str = _STAGE_IDLE
        # Context sources for the embedded chat's PICKER — the SAME rules /
        # documents / artifacts (incl. the netlist) the Skills-menu chat offers, so
        # the OPERATOR includes the board data themselves (pull via tools and/or
        # include via the picker — never an auto-push).
        try:
            self._sources, self._documents_dir = skill_menu._build_sources(
                self._mw, self._project_root)
        except Exception:
            log.exception("dcdc-wizard could not build context sources")
            self._sources, self._documents_dir = [], None
        # True once a validated test is ready to materialize — gates Stage C's
        # Create/name through the busy toggle without clobbering them on idle.
        self._can_create: bool = False

        self.setWindowTitle("DCDC test wizard")
        self.setModal(False)
        self.resize(840, 720)
        self._setup_ui()
        if not (self._finder and self._authoring):
            self._fail("Wizard skills not found (need dcdc_finder + dcdc_authoring "
                       "under authoring_wizards/).")
            self._find_btn.setEnabled(False)

    def _resolve_project_root(self):
        pm = getattr(self._mw, "project_manager", None)
        root = getattr(pm, "project_root", None)
        return root

    # -- UI -------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Find a power IC, build its scope test, validate it against the "
            "netlist, and add it to the project as <b>PSU - &lt;rail&gt;</b>.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{theme.muted_color()};")
        layout.addWidget(intro)

        # Stage A — find ICs --------------------------------------------------
        a = QGroupBox("1 · Find power ICs")
        al = QVBoxLayout(a)
        self._find_btn = QPushButton("🔍 Find power ICs")
        self._find_btn.clicked.connect(self._on_find)
        al.addWidget(self._find_btn)
        self._ic_list = QListWidget()
        self._ic_list.setMaximumHeight(150)
        self._ic_list.itemSelectionChanged.connect(self._on_pick_changed)
        al.addWidget(self._ic_list)
        layout.addWidget(a)

        # Stage B — author the test (the embedded REAL skill chat) ------------
        b = QGroupBox("2 · Build the test for the selected IC")
        bl = QVBoxLayout(b)
        self._build_btn = QPushButton("⚙️ Build test")
        self._build_btn.setEnabled(False)
        self._build_btn.clicked.connect(self._on_build)
        bl.addWidget(self._build_btn)
        # The chat sub-panel: the SAME widget the Skills-menu chat hosts, pinned
        # programmatically (no skill combo) and kicked off by the stage buttons
        # (no Run button). It owns the transcript, input/Send, the web/save/
        # project-tools toggles, the CONTEXT PICKER (rules / documents / artifacts,
        # incl. the netlist) and the Stop/Trash/Restart/timer/token controls — so
        # the operator includes board data EXACTLY like the skill chat (pull via
        # the 🔧 tools and/or include via the picker; never an auto-push).
        self._chat = SkillChatWidget(
            [self._finder, self._authoring],
            self._backend_factory,
            sources=self._sources,
            documents_dir=self._documents_dir,
            backend_tab_id="dcdc_wizard",
            show_skill_selector=False,
            show_run_button=False,
            parent=self,
        )
        self._chat.set_input_placeholder(
            "Answer the skill's question here, then press Enter…")
        self._chat.reply_finished.connect(self._on_reply)
        self._chat.reply_failed.connect(self._on_reply_failed)
        self._chat.busy_changed.connect(self._on_busy)
        bl.addWidget(self._chat, stretch=1)
        layout.addWidget(b, stretch=1)

        # Validation ----------------------------------------------------------
        v = QGroupBox("3 · Validation (existence checks vs the netlist)")
        vl = QVBoxLayout(v)
        self._checks_label = QLabel("Build a test to validate it.")
        self._checks_label.setWordWrap(True)
        self._checks_label.setTextFormat(Qt.TextFormat.RichText)
        vl.addWidget(self._checks_label)
        layout.addWidget(v)

        # Stage C — write -----------------------------------------------------
        c = QGroupBox("4 · Add to project")
        cl = QHBoxLayout(c)
        cl.addWidget(QLabel("Test name:"))
        self._name = QLineEdit()
        self._name.setEnabled(False)
        cl.addWidget(self._name, 1)
        self._create_btn = QPushButton("✅ Create test")
        self._create_btn.setEnabled(False)
        self._create_btn.clicked.connect(self._on_create)
        cl.addWidget(self._create_btn)
        layout.addWidget(c)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        layout.addWidget(self._status)

    # -- Stage A: find --------------------------------------------------------

    def _on_find(self) -> None:
        if not self._finder or self._chat.is_busy:
            return
        self._stage = _STAGE_FIND
        self._ic_list.clear()
        self._rows = []
        self._picked = None
        self._build_btn.setEnabled(False)
        self._reset_downstream()
        self._chat.set_skill(self._finder)
        self._status.setText("Finding power ICs…")
        self._chat.run_kickoff()

    # -- Stage B: author ------------------------------------------------------

    def _on_build(self) -> None:
        if not self._picked or self._chat.is_busy:
            return
        self._stage = _STAGE_BUILD
        self._test_block = ""
        self._reset_downstream()
        self._chat.set_skill(self._authoring)
        self._status.setText("Building the test…")
        self._chat.run_kickoff(priming=_priming(self._picked))

    def _on_pick_changed(self) -> None:
        idx = self._ic_list.currentRow()
        self._picked = self._rows[idx] if 0 <= idx < len(self._rows) else None
        self._build_btn.setEnabled(self._picked is not None and not self._chat.is_busy)

    # -- the single reply seam ------------------------------------------------

    def _on_reply(self, text: str) -> None:
        """One authoritative-reply seam routed by stage: parse the finder list,
        or detect the generated test block (else the skill asked a question and
        the widget's input is live for the user to answer)."""
        if self._stage == _STAGE_FIND:
            self._rows = parse_finder_list(text)
            if not self._rows:
                self._status.setText("No power ICs parsed from the reply.")
                return
            for row in self._rows:
                QListWidgetItem(
                    f"{row.refdes} — {row.part} ({row.kind}) → {row.rail}",
                    self._ic_list)
            self._status.setText(f"Found {len(self._rows)} power IC(s) — pick one.")
        elif self._stage == _STAGE_BUILD:
            block = find_dcdc_test_block(text)
            if block:
                self._test_block = block
                self._validate_and_offer()
            else:
                # No test yet → the skill asked a question; the widget already
                # re-enabled its input for the user to answer via Send.
                self._status.setText(
                    "The skill asked a question — answer it in the chat, then "
                    "press Enter.")

    def _on_reply_failed(self, reason: str) -> None:
        self._status.setText(reason)

    def _on_busy(self, busy: bool) -> None:
        """Gate the wizard's own stage buttons while the chat is in flight (the
        widget self-disables its Run/Send/Stop). Create/name re-enable on idle
        only when a validated test is ready (``_can_create``)."""
        self._find_btn.setEnabled(not busy and bool(self._finder and self._authoring))
        self._build_btn.setEnabled(not busy and self._picked is not None)
        self._create_btn.setEnabled(not busy and self._can_create)
        self._name.setEnabled(not busy and self._can_create)

    # -- Validation + Stage C -------------------------------------------------

    def _validate_and_offer(self) -> None:
        row = self._picked
        params = {
            "ic_refdes": row.refdes,
            "ic_part": row.part,
            "rail_test_point": _extract_test_point(self._test_block),
        }
        try:
            board = OdbBoardData.from_project(self._project_root)
            checks = validate_params(params, board)
        except Exception:
            log.exception("dcdc-wizard validation failed")
            checks = [Check("validation", False, "Could not load the board to validate.")]
        self._render_checks(checks)
        # Prefill the test name + enable Stage C (the user is the gate — a failed
        # existence check warns, it does not block writing).
        self._name.setText(f"PSU - {row.rail}")
        self._can_create = True
        self._name.setEnabled(True)
        self._create_btn.setEnabled(True)

    def _render_checks(self, checks: list[Check]) -> None:
        rows = []
        for c in checks:
            icon = "✅" if c.passed else "⚠️"
            colour = theme.chat_assistant_border() if c.passed else "#c0392b"
            rows.append(f'<div style="color:{colour};">{icon} <b>{html.escape(c.name)}</b>'
                        f' — {html.escape(c.detail)}</div>')
        self._checks_label.setText("".join(rows) or "(no checks)")

    def _on_create(self) -> None:
        name = self._name.text().strip()
        if not name or not self._test_block:
            return
        target = getattr(self._mw, "project_manager", None) or self._project_root
        try:
            result = materialize_test(target, name, self._test_block)
        except Exception:
            log.exception("dcdc-wizard materialize failed")
            self._status.setText("Create failed — see logs.")
            return
        if result.created:
            kind = "with procedure.json" if result.json_written else "(text only — generate JSON in the editor)"
            self._can_create = False
            self._create_btn.setEnabled(False)
            self._status.setText(f"Created '{name}' {kind}: {result.path}")
        else:
            self._status.setText(f"Not created: {result.message}")

    # -- state ----------------------------------------------------------------

    def _reset_downstream(self) -> None:
        self._can_create = False
        self._name.setEnabled(False)
        self._create_btn.setEnabled(False)
        self._checks_label.setText("Build a test to validate it.")

    def _fail(self, message: str) -> None:
        self._status.setText(message)
        log.warning("dcdc-wizard: %s", message)

    # -- cleanup --------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 — Qt override
        """Tear down the embedded chat's worker + dedicated backend (an embedded
        QWidget's own closeEvent never fires)."""
        self._chat.shutdown()
        super().closeEvent(event)
