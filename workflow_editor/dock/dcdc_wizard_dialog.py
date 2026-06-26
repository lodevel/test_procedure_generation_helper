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

Reuses the skill-run machinery exactly like :class:`SkillChatDialog`:
``SkillChatSession`` (prompt assembly), ``backend_factory.create_backend`` (one
dedicated backend; a fresh OpenCode session per skill, reused across the
authoring Q&A turns), ``LLMWorker`` (off-UI-thread send) and ``WorkTimer`` (the
live elapsed indicator). The full multi-pane parallel wizard is a later phase;
this proves one correct, written test end-to-end.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QPlainTextEdit, QLineEdit, QWidget,
    QCheckBox,
)

from .. import theme
from ..authoring import SkillChatSession
from ..authoring import registry
from ..authoring.wizard.list_parse import parse_finder_list, IcRow
from ..authoring.wizard.done_signal import find_dcdc_test_block
from ..authoring.wizard.validate import validate_params, OdbBoardData, Check
from ..authoring.test_materializer import materialize_test
from ..llm.backend_base import LLMRequest, LLMTask
from ..llm.worker import LLMWorker
from ..widgets.work_timer import WorkTimer

log = logging.getLogger(__name__)


def _extract_test_point(block: str) -> str:
    """Best-effort scrape of the rail's scope-probe pad from the generated test.

    The generator names a test point in the steps; we look for a ``TP*`` token.
    Empty when none is confidently found, so the TP check simply reports
    'not provided' rather than false-failing on a parse miss."""
    m = re.search(r"\bTP[A-Za-z0-9_]+\b", block or "")
    return m.group(0) if m else ""


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

        self._backend = None
        self._worker: Optional[LLMWorker] = None
        self._auth_session: Optional[SkillChatSession] = None  # multi-turn authoring
        self._picked: Optional[IcRow] = None
        self._rows: list[IcRow] = []
        self._test_block: str = ""
        self._stream: str = ""
        self._committed: list[tuple[str, str]] = []  # (speaker, text) transcript blocks
        # True only while the authoring skill is waiting on the user's answer —
        # the single source of truth for the answer box's enabled state (never
        # read the widget's own isEnabled(), which a busy-toggle would clobber).
        self._awaiting_answer: bool = False

        self.setWindowTitle("DCDC test wizard")
        self.setModal(False)
        self.resize(840, 720)
        self._timer = WorkTimer(on_tick=lambda s: self._status.setText(f"Working… {s}"))
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

        # Per-run tools (same as the skill chat). The skill needs the netlist
        # (project tools) to FIND ICs, and web/save-datasheets to fetch a
        # datasheet the project documents don't already have.
        tog = QHBoxLayout()
        self._web_cb = QCheckBox("🌐 Web")
        self._web_cb.setToolTip("Let the skill search the web + read/fetch datasheet PDFs.")
        self._save_cb = QCheckBox("💾 Save datasheets")
        self._save_cb.setToolTip("Cache fetched datasheets into the project documents (needs 🌐).")
        self._tools_cb = QCheckBox("🔧 Netlist / project tools")
        self._tools_cb.setChecked(True)
        self._tools_cb.setToolTip("Let the skill PULL the netlist / components / test points "
                                  "(required to find ICs and map the rail).")
        for cb in (self._web_cb, self._save_cb, self._tools_cb):
            tog.addWidget(cb)
        tog.addStretch()
        layout.addLayout(tog)

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

        # Stage B — author the test ------------------------------------------
        b = QGroupBox("2 · Build the test for the selected IC")
        bl = QVBoxLayout(b)
        self._build_btn = QPushButton("⚙️ Build test")
        self._build_btn.setEnabled(False)
        self._build_btn.clicked.connect(self._on_build)
        bl.addWidget(self._build_btn)
        self._transcript = QTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText(
            "The authoring conversation appears here. If the skill asks a "
            "question, answer it below; when it emits the test it is shown here.")
        bl.addWidget(self._transcript)
        ans_row = QHBoxLayout()
        self._answer = QLineEdit()
        self._answer.setPlaceholderText("Answer the skill's question…")
        self._answer.setEnabled(False)
        self._answer.returnPressed.connect(self._on_answer)
        self._answer_btn = QPushButton("Send")
        self._answer_btn.setEnabled(False)
        self._answer_btn.clicked.connect(self._on_answer)
        ans_row.addWidget(self._answer)
        ans_row.addWidget(self._answer_btn)
        bl.addLayout(ans_row)
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

    # -- backend / run --------------------------------------------------------

    def _ensure_backend(self):
        if self._backend is None:
            self._backend = self._backend_factory.create_backend(tab_id="dcdc_wizard")
            if hasattr(self._backend, "start"):
                self._backend.start()
        return self._backend

    def _run(self, session: SkillChatSession, prompt: str, fresh: bool, on_done) -> None:
        """Send one turn off the UI thread. ``fresh`` mints a new OpenCode session
        (a new skill / a new build); otherwise the session is reused (Q&A turns)."""
        if self._worker is not None:
            return
        backend = self._ensure_backend()
        try:
            if fresh and hasattr(backend, "new_session"):
                backend.new_session()
            elif hasattr(backend, "ensure_session"):
                backend.ensure_session()
        except Exception:
            log.exception("dcdc-wizard session setup failed; sending anyway")

        skill = session.skill
        request = LLMRequest(
            task=LLMTask.AD_HOC_CHAT,
            raw_prompt=prompt,
            system_prompt=session.system_prompt,
            web_enabled=self._web_cb.isChecked(),
            save_docs_enabled=self._save_cb.isChecked(),
            project_tools_enabled=self._tools_cb.isChecked(),
            skill_servers_enabled=list((skill.metadata or {}).get("mcp_tools") or []),
        )
        self._stream = ""
        self._worker = LLMWorker(backend, request, parent=self)
        self._worker.text_chunk.connect(self._on_chunk)
        self._worker.thinking_chunk.connect(self._on_thinking_chunk)
        self._worker.finished.connect(on_done)
        self._worker.error.connect(self._on_error)
        self._set_busy(True)
        self._timer.start()
        self._worker.start()

    def _on_chunk(self, text: str) -> None:
        self._stream += text
        self._repaint(live=self._stream)

    def _on_thinking_chunk(self, _text: str) -> None:
        # Reasoning chunks aren't shown in the transcript; the WorkTimer keeps
        # the status line alive so the window doesn't look frozen.
        pass

    def _teardown_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _finish_common(self) -> None:
        self._teardown_worker()
        self._set_busy(False)
        self._status.setText(f"Done in {self._timer.stop()}")

    def _on_error(self, message: str) -> None:
        self._timer.stop()
        self._teardown_worker()
        self._set_busy(False)
        self._status.setText("")
        self._append("System", message)

    # -- Stage A: find --------------------------------------------------------

    def _on_find(self) -> None:
        if not self._finder or self._worker is not None:
            return
        self._ic_list.clear()
        self._append("System", "Finding power ICs…")
        session = SkillChatSession(self._finder)
        session.set_context("")
        self._run(session, session.kickoff(), fresh=True, on_done=self._on_find_done)

    def _on_find_done(self, response) -> None:
        self._finish_common()
        if not getattr(response, "success", False):
            self._append("System", "Finder failed: "
                         + (getattr(response, "error_message", "") or "unknown error"))
            return
        text = SkillChatSession.interpret(response)
        self._rows = parse_finder_list(text)
        if not self._rows:
            self._append("Finder", text or "(no list returned)")
            self._append("System", "No power ICs parsed from the reply.")
            return
        for row in self._rows:
            QListWidgetItem(f"{row.refdes} — {row.part} ({row.kind}) → {row.rail}",
                            self._ic_list)
        self._append("System", f"Found {len(self._rows)} power IC(s) — pick one.")

    def _on_pick_changed(self) -> None:
        idx = self._ic_list.currentRow()
        self._picked = self._rows[idx] if 0 <= idx < len(self._rows) else None
        self._build_btn.setEnabled(self._picked is not None and self._worker is None)

    # -- Stage B: author ------------------------------------------------------

    def _on_build(self) -> None:
        if not self._picked or self._worker is not None:
            return
        self._test_block = ""
        self._reset_downstream()
        self._auth_session = SkillChatSession(self._authoring)
        self._auth_session.set_context("")
        prompt = self._auth_session.start_user_turn(_priming(self._picked))
        self._append("You", _priming(self._picked))
        self._run(self._auth_session, prompt, fresh=True, on_done=self._on_auth_done)

    def _on_answer(self) -> None:
        msg = self._answer.text().strip()
        if not msg or self._worker is not None or self._auth_session is None:
            return
        self._answer.clear()
        prompt = self._auth_session.start_user_turn(msg)
        self._append("You", msg)
        self._run(self._auth_session, prompt, fresh=False, on_done=self._on_auth_done)

    def _on_auth_done(self, response) -> None:
        self._finish_common()
        if not getattr(response, "success", False):
            self._auth_session.drop_last_user_turn()
            self._append("System", "Build failed: "
                         + (getattr(response, "error_message", "") or "unknown error"))
            return
        text = SkillChatSession.interpret(response)
        self._auth_session.record_assistant(text)
        self._append("Authoring", text)

        block = find_dcdc_test_block(text)
        if block:
            self._test_block = block
            self._awaiting_answer = False
            self._answer.setEnabled(False)
            self._answer_btn.setEnabled(False)
            self._validate_and_offer()
        else:
            # No test yet → the skill asked a question; let the user answer.
            self._awaiting_answer = True
            self._answer.setEnabled(True)
            self._answer_btn.setEnabled(True)
            self._answer.setFocus()

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
            self._append("System", "Create failed — see logs.")
            return
        if result.created:
            kind = "with procedure.json" if result.json_written else "(text only — generate JSON in the editor)"
            self._append("System", f"Created '{name}' {kind}: {result.path}")
            self._create_btn.setEnabled(False)
            self._status.setText(result.message)
        else:
            self._append("System", f"Not created: {result.message}")

    # -- state / transcript ---------------------------------------------------

    def _reset_downstream(self) -> None:
        self._awaiting_answer = False
        self._answer.setEnabled(False)
        self._answer_btn.setEnabled(False)
        self._name.setEnabled(False)
        self._create_btn.setEnabled(False)
        self._checks_label.setText("Build a test to validate it.")

    def _set_busy(self, busy: bool) -> None:
        self._find_btn.setEnabled(not busy)
        self._build_btn.setEnabled(not busy and self._picked is not None)
        self._answer.setEnabled(not busy and self._awaiting_answer)
        self._answer_btn.setEnabled(not busy and self._awaiting_answer)

    def _append(self, speaker: str, text: str) -> None:
        self._committed.append((speaker, text))
        self._repaint()

    def _repaint(self, live: str = "") -> None:
        blocks = list(self._committed)
        if live:
            blocks.append(("LLM (working…)", live))
        self._transcript.setHtml("".join(
            f'<div style="margin:4px 0;"><b>{html.escape(sp)}:</b> '
            f'{html.escape(tx).replace(chr(10), "<br>")}</div>' for sp, tx in blocks))
        bar = self._transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _fail(self, message: str) -> None:
        self._status.setText(message)
        log.warning("dcdc-wizard: %s", message)

    # -- cleanup --------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 — Qt override
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(2000)
            self._teardown_worker()
        if self._backend is not None and hasattr(self._backend, "stop"):
            try:
                self._backend.stop()
            except Exception:
                log.exception("dcdc-wizard backend stop failed")
            self._backend = None
        super().closeEvent(event)
