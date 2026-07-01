"""Skill-chat widget — the EMBEDDABLE chat body that runs ONE skill conversation.

This is the reusable brain-and-chrome of a skill chat, extracted from
:class:`~workflow_editor.dock.skill_chat_dialog.SkillChatDialog` so it can be
hosted by either the Skills-menu dialog (thin window chrome + the *Insert*
button) OR the DCDC wizard (driven programmatically through its stage logic).

A skill chat is a plain multi-turn conversation on a PERSISTENT OpenCode session:
the skill's ``SKILL.md`` system prompt plus the chosen context on the first
message, then only the NEW message each turn (OpenCode keeps the history,
including MCP tool results). Replies are prose (no JSON contract).

The brain it drives is reused, not reimplemented:

* :class:`~workflow_editor.authoring.SkillChatSession` — turn logic + prompt
  assembly (``start_user_turn`` / ``record_assistant`` / ``interpret``).
* :func:`~workflow_editor.authoring.assemble` — turns the picker's selection into
  the pushed context text.
* :class:`~workflow_editor.widgets.skill_context_picker.SkillContextPicker` — the
  checkable context browser (built ONLY when ``sources`` is given; otherwise a
  host pushes the context string via :meth:`set_pushed_context`).
* :class:`~workflow_editor.llm.worker.LLMWorker` — runs the send off the UI
  thread; driven with ``LLMRequest(raw_prompt=...)`` so the backend bypasses its
  JSON output contract.

Backend ownership: the widget creates and owns a DEDICATED backend via
``backend_factory.create_backend(tab_id=backend_tab_id)`` so each embed has its
OWN OpenCode session, independent of the dock chat and of the other embed
(``"skill_chat"`` for the dialog, ``"dcdc_wizard"`` for the wizard — never share
a tab_id or they would share a conversation). The session is PERSISTENT:
``ensure_session`` before each send reuses it; ``new_session`` only resets.

IMPORTANT: a ``QWidget``'s ``closeEvent`` does NOT fire when it is embedded in a
parent layout, so the backend teardown lives in the public :meth:`shutdown` —
EVERY host MUST call it from its own ``closeEvent`` or the OpenCode session
leaks.
"""

from __future__ import annotations

import html
import logging
import threading
from typing import Optional

from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPlainTextEdit, QTextEdit, QPushButton, QCheckBox, QComboBox, QLabel,
)

from .. import theme
from ..authoring import SkillChatSession, assemble
from ..widgets.skill_context_picker import SkillContextPicker
from ..llm.backend_base import LLMRequest, LLMTask
from ..llm.context_usage import format_context_usage, used_tokens
from ..llm.worker import LLMWorker
from ..widgets.work_timer import WorkTimer

log = logging.getLogger(__name__)


# Speaker → (theme role, display name, avatar glyph). The speaker strings are
# the ones already passed to ``_append_line`` / built in ``_render_transcript``
# ("You" / "Assistant" / "System"); the theme role keys ("user" / "assistant" /
# "system") drive the palette-aware tints via ``theme.message_bg`` /
# ``message_border`` so the colours track the light/dark Fluent skin and never
# hardcode a colour that breaks dark mode.
_ROLE_META: dict[str, tuple[str, str, str]] = {
    "You": ("user", "You", "🧑"),
    "Assistant": ("assistant", "Assistant", "🤖"),
    "System": ("system", "System", "⚙️"),
}


def _role_accent(role: str) -> str:
    """Accent (left-border + label) colour for a theme role — reuses the chat
    palette's role borders so it stays readable in both light and dark."""
    return {
        "user": theme.chat_user_border(),
        "assistant": theme.chat_assistant_border(),
        "system": theme.chat_system_border(),
    }.get(role, theme.border_color())


class SkillChatWidget(QWidget):
    """The embeddable chat body for one run of one authoring skill.

    Owns the transcript, input, the web/save-docs/project-tools toggles, the
    (optional) context picker, the Run/Send/Stop/Trash/Restart controls, the
    elapsed-time indicator, the token/context-usage readout, the LLM worker
    dispatch, the dedicated backend lifecycle, and the :class:`SkillChatSession`.

    It does NOT own the *Insert* button nor any window chrome — those belong to
    the host. The host gates its own out-of-widget buttons via the signals and
    drives programmatic kickoffs via the public methods.
    """

    # The authoritative assistant text, emitted AFTER ``latest_reply`` is set and
    # the assistant turn is recorded (order matters — hosts read latest_reply).
    reply_finished = Signal(str)
    # A failure/error reason (response.success False, or a transport error).
    reply_failed = Signal(str)
    # A request went in-flight ↔ returned to idle (host gates its own buttons).
    busy_changed = Signal(bool)
    # Trash OR a skill switch cleared the chat (host disables Insert / clears).
    conversation_reset = Signal()

    def __init__(
        self,
        skills: list,
        backend_factory,
        *,
        sources: Optional[list] = None,
        documents_dir=None,
        backend_tab_id: str = "skill_chat",
        show_skill_selector: bool = True,
        show_run_button: bool = True,
        dispatch_gate=None,
        parent=None,
    ) -> None:
        """
        Args:
            skills: The skills this widget can run. The combo (when shown)
                populates from these; ``skills[0]`` is active initially. A host
                that pins a skill programmatically still passes the full list.
            backend_factory: Factory exposing ``create_backend(tab_id=...)`` and
                (for the restart button) ``server_manager``.
            sources: Context picker sources (one tab each). ``None``/empty → NO
                context panel; the host pushes the context string instead via
                :meth:`set_pushed_context`.
            documents_dir: Folder backing the picker's Documents tab (or None).
            backend_tab_id: Dedicated-backend / OpenCode-session id. Each embed
                MUST use a distinct id (``"skill_chat"`` / ``"dcdc_wizard"``).
            show_skill_selector: Show the skill combo + when-to-use header. False
                when the host switches the skill programmatically.
            show_run_button: Show the *Run skill* button. False when the host
                drives the kickoff from its own stage buttons.
            dispatch_gate: Optional ``gate(fire, *, interactive)`` callable. When
                set, EVERY fire (kickoff + user send) is routed through it instead
                of firing immediately — the DCDC wizard's concurrency scheduler,
                which calls ``fire`` now (slot free) or stores it to fire later
                (queued). ``interactive`` is True for a user answer (may jump the
                queue), False for an initial build. None → fire at once (the
                Skills-menu chat path, unchanged).
            parent: Parent widget.
        """
        super().__init__(parent)
        self._skills: list = list(skills)
        self._skill = self._skills[0] if self._skills else None
        self._backend_factory = backend_factory
        self._backend_tab_id = backend_tab_id
        self._show_skill_selector = show_skill_selector
        self._show_run_button = show_run_button
        self._dispatch_gate = dispatch_gate
        self._session = SkillChatSession(self._skill)

        # Lazily-created dedicated backend + the in-flight worker.
        self._backend = None
        self._worker: Optional[LLMWorker] = None
        # The most recent assistant draft (what a host Inserts / parses). Empty
        # until the first reply lands.
        self._latest_draft: str = ""
        # Accumulates streamed response text for the live "Assistant: ..." block.
        self._stream_buffer: str = ""
        # Display source of truth for the rich transcript: an ordered list of
        # (speaker, text) blocks. Includes transient System/You lines that are
        # NOT (yet) session turns; a re-render from the session rebuilds it.
        self._blocks: list[tuple[str, str]] = []
        # Raw context string pushed by the host when there is NO picker.
        self._pushed_context: str = ""

        # Widgets that may not be built (gated by the show_* flags).
        self._skill_combo: Optional[QComboBox] = None
        self._header: Optional[QLabel] = None
        self._run_btn: Optional[QPushButton] = None

        # Per-chat toggle DEFAULTS come from Settings (opencode.web_default /
        # opencode.project_tools_default; both False if unset or unreadable).
        # Each is still per-chat overridable via its checkbox.
        opencode_settings: dict = {}
        self._context_limit = 16384  # token window for the context-% readout
        try:
            from ..dialogs.settings_dialog import load_settings
            _s = load_settings()
            opencode_settings = _s.get("opencode", {}) or {}
            _common = _s.get("common_llm", {}) or {}
            self._context_limit = int(
                _common.get("context_window", _common.get("max_tokens", 16384)) or 16384)
        except Exception:
            log.exception("skill-chat could not read Settings defaults")
        self._web_default = bool(opencode_settings.get("web_default", False))
        self._save_docs_default = bool(opencode_settings.get("save_docs_default", False))
        self._project_tools_default = bool(
            opencode_settings.get("project_tools_default", False))

        # The picker is built ONLY when sources are supplied; otherwise the host
        # pushes the context string (and no context panel is shown).
        self._picker: Optional[SkillContextPicker] = (
            SkillContextPicker(sources, documents_dir=documents_dir) if sources else None)
        self._setup_ui()

    # -- UI construction ------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Skill selector — choose which skill to run here (scales to many skills;
        # a flat menu of every skill doesn't). Hidden when the host pins a skill.
        if self._show_skill_selector:
            sel_row = QHBoxLayout()
            sel_row.addWidget(QLabel("Skill:"))
            self._skill_combo = QComboBox()
            for s in self._skills:
                label = s.title or s.skill_id
                if s.version:
                    label = f"{label}  (v{s.version})"
                self._skill_combo.addItem(label, s.skill_id)
            self._skill_combo.currentIndexChanged.connect(self._on_skill_changed)
            sel_row.addWidget(self._skill_combo, 1)
            layout.addLayout(sel_row)

            self._header = QLabel(
                (self._skill.when_to_use or "") if self._skill else "")
            self._header.setWordWrap(True)
            self._header.setStyleSheet(f"color:{theme.muted_color()};")
            layout.addWidget(self._header)

        # Transcript (left) | context picker (right, only when a picker exists).
        split = QSplitter(Qt.Orientation.Horizontal)

        transcript_group = QGroupBox("Conversation")
        tg_layout = QVBoxLayout(transcript_group)
        # Rich-text transcript: each turn is rendered as a role-tinted block
        # (accent border + tinted background + bold coloured role label) so the
        # eye separates You / Assistant / System at a glance. QTextEdit (not
        # QPlainTextEdit) because we paint per-role HTML.
        self._transcript = QTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText(
            "The skill conversation appears here. Ask for a draft, refine it "
            "over a few turns, then Insert it into the procedure."
        )
        tg_layout.addWidget(self._transcript)
        split.addWidget(transcript_group)

        if self._picker is not None:
            context_group = QGroupBox("Context to push")
            cg_layout = QVBoxLayout(context_group)
            cg_layout.addWidget(self._picker)
            split.addWidget(context_group)
            split.setStretchFactor(0, 1)
            split.setStretchFactor(1, 0)
            split.setSizes([560, 340])
        else:
            split.setStretchFactor(0, 1)
        layout.addWidget(split, stretch=1)

        # Web toggle — exposes OpenCode's webfetch + websearch tools for this
        # chat only (per-request override). OFF by default: with web on, even a
        # no-code skill could be steered into leaking the attached context via a
        # crafted URL, so the user opts in explicitly.
        self._web_checkbox = QCheckBox("🌐 Web access (search + fetch)")
        self._web_checkbox.setToolTip(
            "Let the skill search the web and fetch pages (e.g. to confirm a "
            "power IC or find a datasheet).\n"
            "Off by default — only enable it for skills you trust, since web "
            "access can be used to leak the context you attached."
        )
        # Default checked state comes from Settings (per-chat overridable).
        self._web_checkbox.setChecked(self._web_default)

        # Project-data tools toggle — exposes the project_tools MCP server for
        # this chat only, letting the LLM PULL board data (netlist, components,
        # test points) on demand instead of having it all pushed up front. OFF
        # by default (Settings can flip the default); per-chat overridable.
        self._project_tools_checkbox = QCheckBox("🔧 Project data tools (pull)")
        self._project_tools_checkbox.setToolTip(
            "Let the skill pull board data on demand — query the netlist, list "
            "or inspect components, and read test points — instead of pushing "
            "it all into the context up front.\n"
            "Off by default; enable it so the model can fetch exactly the "
            "board data it needs."
        )
        self._project_tools_checkbox.setChecked(self._project_tools_default)

        self._save_docs_checkbox = QCheckBox("💾 Save datasheets")
        self._save_docs_checkbox.setToolTip(
            "Let the model SAVE datasheets it downloads into the project's "
            "documents folder (sandboxed) for reuse by future tests. Needs "
            "🌐 web on. Off by default."
        )
        self._save_docs_checkbox.setChecked(self._save_docs_default)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(self._web_checkbox)
        toggle_row.addWidget(self._project_tools_checkbox)
        toggle_row.addWidget(self._save_docs_checkbox)
        toggle_row.addStretch()
        layout.addLayout(toggle_row)

        # Input box (Enter sends, Shift+Enter newline — mirrors ChatPanel).
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText(
            "Optional: prime the skill before Run (e.g. 'build the test for U86'), "
            "then refine over the next turns…")
        self._input.setFixedHeight(72)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        # Action row.
        btn_row = QHBoxLayout()

        if self._show_run_button:
            self._run_btn = QPushButton("Run skill")
            self._run_btn.setToolTip(
                "Start the skill using the checked context. Type a message first to "
                "PRIME it (e.g. which rail/IC to build) — or leave it empty to run "
                "the skill from the top.")
            self._run_btn.clicked.connect(self._on_run)
            btn_row.addWidget(self._run_btn)

        self._send_btn = QPushButton("Send")
        self._send_btn.setToolTip("Send a message to refine the draft.")
        self._send_btn.clicked.connect(self._on_send)
        btn_row.addWidget(self._send_btn)

        self._stop_btn = self._icon_button(
            "⏹️", "Stop the current request", self._on_stop)
        self._stop_btn.setEnabled(False)
        btn_row.addWidget(self._stop_btn)

        self._trash_btn = self._icon_button(
            "🗑️", "Clear the conversation and start over", self.clear)
        btn_row.addWidget(self._trash_btn)

        # Stays enabled even while busy — the point is to recover a HUNG send.
        self._restart_btn = self._icon_button(
            "🔄", "Restart the backend server (recover a hung / unresponsive "
            "backend without relaunching the editor)", self._on_restart_backend)
        btn_row.addWidget(self._restart_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        layout.addWidget(self._status)
        self._work_timer = WorkTimer(
            on_tick=lambda s: self._status.setText(f"Thinking… {s}"), parent=self)

        # Context-usage readout. We show the LATEST turn's total tokens as the
        # current context size: OpenCode holds the persistent session, so the last
        # turn's reported usage IS the live context (system + history + tool
        # results) — and this stays correct across any compaction, unlike a running
        # sum (which would over-count).
        self._context_label = QLabel("")
        self._context_label.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        layout.addWidget(self._context_label)

    def eventFilter(self, obj, event):  # noqa: N802 — Qt override
        """Enter in the input field sends; Shift+Enter inserts a newline."""
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            is_return = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            shift = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            if is_return and not shift:
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _icon_button(self, text: str, tooltip: str, on_click) -> QPushButton:
        """A compact icon button whose glyph isn't clipped — a local stylesheet
        overrides the app's wider button padding (the old setMaximumWidth(35) cut
        the emoji off)."""
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setObjectName("iconButton")
        btn.setStyleSheet("QPushButton { padding: 4px 8px; min-width: 0; }")
        btn.clicked.connect(on_click)
        return btn

    # -- public API -----------------------------------------------------------

    @property
    def latest_reply(self) -> str:
        """The last authoritative assistant text (Insert source / parse source)."""
        return self._latest_draft

    @property
    def is_busy(self) -> bool:
        """True while a request is in flight."""
        return self._worker is not None

    def set_skill(self, skill) -> None:
        """Pin/switch the active skill (programmatic twin of ``_on_skill_changed``):
        fresh session + new OpenCode session + cleared transcript + reset token
        label, and ``conversation_reset`` emitted. Guards the combo signal so it
        can't re-enter the combo path."""
        if self._skill_combo is not None:
            idx = self._index_of(skill)
            if idx >= 0:
                self._skill_combo.blockSignals(True)
                self._skill_combo.setCurrentIndex(idx)
                self._skill_combo.blockSignals(False)
        self._switch_to(skill)

    def set_pushed_context(self, text: str) -> None:
        """Set the raw context string pushed on the next send when there is NO
        picker (e.g. the wizard's netlist). Fed to ``session.set_context`` in the
        send path instead of ``assemble(picker.selections())``."""
        self._pushed_context = text or ""

    def resolved_context(self) -> str:
        """The context string this widget would send right now — the picker's
        assembled selection (when it has a picker) or the host's pushed string.
        Hosts read this to PROPAGATE one widget's chosen context to another: the
        DCDC wizard hands P1's picker selection to every per-IC build chat."""
        return self._resolve_context()

    def run_kickoff(self, priming: str = "") -> None:
        """Start the skill with the resolved context (Run path). A non-empty
        ``priming`` is sent as the opening turn (e.g. "build the test for U86" so
        the skill jumps straight in); empty is the bare kickoff (context + skill
        prompt only). No-op if a request is in flight or the session is started."""
        if self._worker is not None or self._session.started:
            return
        self._session.set_context(self._resolve_context())
        title = (self._skill.title or self._skill.skill_id) if self._skill else "skill"
        self._append_line("System", f"Running '{title}'…")
        priming = (priming or "").strip()
        if priming:
            prompt = self._session.start_user_turn(priming)
            self._append_line("You", priming)
        else:
            prompt = self._session.kickoff()
        self._dispatch(prompt, interactive=False)  # an initial build (not a user answer)

    def send_user_turn(self, text: str) -> None:
        """Send a refine turn (programmatic twin of ``_on_send`` minus reading the
        box). No-op if a request is in flight or ``text`` is blank."""
        if self._worker is not None:
            return
        text = (text or "").strip()
        if not text:
            return
        # Refresh the pushed context (it rides the first send, and again only when
        # the resolved selection changes — see SkillChatSession._lead_context).
        self._session.set_context(self._resolve_context())
        prompt = self._session.start_user_turn(text)
        self._append_line("You", text)
        self._dispatch(prompt, interactive=True)  # a user answer/refine: may jump the queue

    def clear(self) -> None:
        """Trash: cancel any in-flight send, start a FRESH session (same skill),
        drop the history, clear the transcript. Emits ``conversation_reset``."""
        if self._worker is not None:
            self._worker.cancel()
            self._teardown_worker()
        self._session = SkillChatSession(self._skill)
        self._new_backend_session()  # discard OpenCode's history too
        self._latest_draft = ""
        self._stream_buffer = ""
        self._blocks = []
        self._transcript.clear()
        self._status.setText("")
        self._context_label.setText("")  # session destroyed → drop its stale token readout
        self._set_busy(False)  # fresh session → Run re-enabled, controls live
        self.conversation_reset.emit()

    def shutdown(self) -> None:
        """Cancel + wait the in-flight worker and stop the dedicated backend so the
        private OpenCode session doesn't leak. MANDATORY: a QWidget's closeEvent
        does NOT fire when embedded, so EVERY host MUST call this from its own
        closeEvent."""
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(2000)
            self._teardown_worker()
        if self._backend is not None and hasattr(self._backend, "stop"):
            try:
                self._backend.stop()
            except Exception:
                log.exception("skill-chat backend stop failed")
            self._backend = None

    def stop(self) -> None:
        """Public Stop: cancel the in-flight turn (host-facing). The wizard's
        *Abandon* calls this to halt a running build. No-op when idle or merely
        queued (no worker yet) — the host cancels the queued fire on its side."""
        self._on_stop()

    def set_input_placeholder(self, text: str) -> None:
        """Tailor the input box's prompt hint (host-facing convenience)."""
        self._input.setPlaceholderText(text)

    def set_status(self, text: str) -> None:
        """Write the widget's status line (host-facing convenience)."""
        self._status.setText(text)

    def append_note(self, speaker: str, text: str) -> None:
        """Push a transient line into the transcript (host-facing convenience).
        Note: a successful turn re-renders from the session and drops transient
        System lines — see :meth:`_render_transcript`."""
        self._append_line(speaker, text)

    # -- context resolution seam ---------------------------------------------

    def _resolve_context(self) -> str:
        """The ONE place the picker-vs-pushed-string fork lives: the picker's
        assembled text when a picker exists, else the host's pushed string."""
        if self._picker is not None:
            return assemble(self._picker.selections()).text
        return self._pushed_context

    def _index_of(self, skill) -> int:
        """Index of ``skill`` in ``self._skills`` matched by ``skill_id`` (robust to
        identity/equality differences), or -1."""
        target = getattr(skill, "skill_id", None)
        for i, s in enumerate(self._skills):
            if s is skill or getattr(s, "skill_id", None) == target:
                return i
        return -1

    # -- backend lifecycle ----------------------------------------------------

    def _ensure_backend(self):
        """Lazily mint + start the dedicated backend.

        Mirrors :class:`TabContext`'s lazy-create-then-start pattern. The
        backend is cached for the widget's lifetime so the OpenCode session is
        stable across turns (reused per send via ``ensure_session``, not
        recreated)."""
        if self._backend is None:
            self._backend = self._backend_factory.create_backend(
                tab_id=self._backend_tab_id)
            if hasattr(self._backend, "start"):
                self._backend.start()
        return self._backend

    def _ensure_session(self) -> None:
        """Make sure the cached backend has a LIVE session, creating one only if it
        has none — the persistent-session counterpart of ``new_session``. The
        session is REUSED across sends so OpenCode keeps the whole conversation
        (incl. MCP tool results) instead of throwing it away each turn; after a
        server restart the same id reattaches (OpenCode persists sessions on disk)."""
        backend = self._backend
        ensure = getattr(backend, "ensure_session", None) if backend else None
        if callable(ensure):
            try:
                ensure()
            except Exception:
                log.exception("skill-chat ensure_session failed; sending anyway")

    def _new_backend_session(self) -> None:
        """Start a FRESH server session to TRUE-reset the conversation (trash /
        skill-switch), discarding OpenCode's history. Prefers the cheap
        ``new_session`` (re-POSTs /session); never ``reset_session`` (which stops +
        restarts the whole server). No-op when no backend exists yet — the next send
        mints a fresh one."""
        backend = self._backend
        new_session = getattr(backend, "new_session", None) if backend else None
        if callable(new_session):
            try:
                new_session()
            except Exception:
                log.exception("skill-chat new_session failed; continuing")

    # -- send path ------------------------------------------------------------

    def _on_run(self) -> None:
        """Run button: read the optional PRIMING message from the input box, then
        kick off. An empty box is the bare kickoff."""
        if self._worker is not None or self._session.started:
            return
        priming = self._input.toPlainText().strip()
        if priming:
            self._input.clear()
        self.run_kickoff(priming)

    def _on_send(self) -> None:
        """Send button / Enter: read the input box and send a refine turn."""
        if self._worker is not None:
            return  # a request is already in flight
        message = self._input.toPlainText().strip()
        if not message:
            return
        self._input.clear()
        self.send_user_turn(message)

    def _dispatch(self, prompt: str, *, interactive: bool = True) -> None:
        """Fire a built turn (shared by Run + Send). With a ``dispatch_gate`` set
        (the wizard's scheduler) the actual fire is routed through it — run now or
        queued — and the widget LOCKS immediately (``_set_busy`` disables input +
        Send) so no second turn is submitted while one is pending; with no gate it
        fires at once (the Skills-menu chat path)."""
        self._set_busy(True)  # pending OR running: lock the UI, block a re-submit
        try:
            if self._dispatch_gate is not None:
                self._dispatch_gate(lambda: self._really_dispatch(prompt),
                                    interactive=interactive)
            else:
                self._really_dispatch(prompt)
        except Exception:
            # A synchronous dispatch/setup failure (the scheduler already released
            # any slot it took) must not leave the widget locked with no worker.
            log.exception("skill-chat dispatch failed")
            self._set_busy(False)
            self._status.setText("Failed to start — see logs.")

    def _really_dispatch(self, prompt: str) -> None:
        """Build the worker and start streaming — the actual fire, run immediately
        or released later by the gate. Assumes the UI is already locked busy."""
        backend = self._ensure_backend()
        self._ensure_session()

        _declared = list(
            (getattr(self._skill, "metadata", None) or {}).get("mcp_tools") or [])

        # run_skill (skill-invokes-skill) is opt-in: a skill must declare it in
        # `mcp_tools`. When it does, mint this (depth-0) session's HMAC chain-token
        # from the per-process secret and prepend a [HOST] preamble handing it to
        # the model; the run_skill MCP server verifies that token. No secret (server
        # down / non-OpenCode backend) -> stay off (the server would refuse anyway).
        _sys = self._session.system_prompt
        _run_skill = False
        if "run_skill" in _declared:
            _secret = getattr(getattr(backend, "config", None), "run_skill_secret", None)
            if _secret:
                from ..authoring import rs_core
                _tok = rs_core.sign(
                    {"depth": 0, "visited": [self._skill.skill_id]}, _secret.encode())
                _sys = (f"[HOST] Your run_skill chain_token is: {_tok}\n"
                        f"[HOST] If (and only if) you call run_skill, pass this exact "
                        f"chain_token as the chain_token argument.\n\n{_sys}")
                _run_skill = True
            else:
                log.warning(
                    "skill %r declares run_skill but no signing secret is available "
                    "(OpenCode backend not pre-warmed?); recursion is unavailable.",
                    getattr(self._skill, "skill_id", "?"))

        request = LLMRequest(
            task=LLMTask.AD_HOC_CHAT,
            raw_prompt=prompt,
            # SKILL.md as the governing system prompt (not buried in the body);
            # carries the [HOST] run_skill token preamble when recursion is enabled.
            system_prompt=_sys,
            web_enabled=self._web_checkbox.isChecked(),
            save_docs_enabled=self._save_docs_checkbox.isChecked(),
            project_tools_enabled=self._project_tools_checkbox.isChecked(),
            # A skill exposes ONLY its own tools: it declares the server(s) it
            # uses in frontmatter (e.g. dcdc_bringup -> `mcp_tools: [dcdc_tools]`);
            # the backend turns those on and every other skill tool explicitly off.
            skill_servers_enabled=_declared,
            run_skill_enabled=_run_skill,
        )
        self._worker = LLMWorker(backend, request, parent=self)
        self._worker.text_chunk.connect(self._on_text_chunk)
        self._worker.thinking_chunk.connect(self._on_thinking_chunk)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._stream_buffer = ""
        self._work_timer.start()
        self._worker.start()

    def _on_text_chunk(self, text: str) -> None:
        """Show response text progressively so the operator isn't staring at
        silence. The growing reply is repainted as a live "Assistant" block; the
        final, authoritative text is set in :meth:`_on_finished`."""
        self._stream_buffer += text
        self._repaint(live=("Assistant", self._stream_buffer))

    def _on_thinking_chunk(self, _text: str) -> None:
        # Reasoning chunks are not displayed in the transcript; the live
        # work_timer keeps the status line updated while thinking.
        pass

    def _on_finished(self, response) -> None:
        self._teardown_worker()
        self._set_busy(False)
        self._status.setText(f"Replied in {self._work_timer.stop()}")

        if not getattr(response, "success", False):
            # A genuine failure: the backend returns success=True for a prose
            # reply (the raw_prompt -> plain_text parse path), so success=False
            # here means a real transport/backend problem (e.g. NoneBackend
            # carrying the classified server reason). Surface it and drop the
            # unanswered user turn so the next send doesn't double-prompt.
            reason = (
                getattr(response, "error_message", "")
                or getattr(response, "assistant_message", "")
                or "The request failed."
            )
            # Keep any partial streamed reply visible above the failure notice.
            if self._stream_buffer:
                self._blocks.append(("Assistant", self._stream_buffer))
            self._append_line("System", reason)
            self._session.drop_last_user_turn()
            self.reply_failed.emit(reason)
            return

        text = SkillChatSession.interpret(response)
        self._session.record_assistant(text)
        self._latest_draft = text

        # Replace the streamed preview (if any) with the authoritative text by
        # re-rendering the whole transcript from the session — cheap and avoids
        # drift between the live stream and the final parse.
        self._render_transcript()
        self._update_context_label(response)
        # Emit AFTER _latest_draft is set + the assistant turn recorded — hosts
        # read widget.latest_reply from this signal.
        self.reply_finished.emit(text)

    def _update_context_label(self, response) -> None:
        """Show the LATEST turn's TOTAL tokens (input + output) as the current
        context size. Output counts too: the reply joins the transcript and is
        re-sent as input next turn, so total = system + context + the whole
        transcript INCLUDING this reply. For a stateless skill chat this IS the
        live context, and it tracks compaction (a running sum would over-count
        after a compact). Computed via the shared ``context_usage`` helpers so
        the dock chat and this widget can't drift."""
        used = used_tokens(response)
        if not used:
            return
        # Prefer the active model's REAL context window (from the running
        # OpenCode server) over the static common_llm.context_window setting,
        # which is wrong for modern models (e.g. gpt-5.x: 272k+). The server is
        # up by the time a response lands, so resolve it lazily here and cache.
        backend = self._backend
        if backend is not None:
            try:
                window = backend.get_context_window()
            except Exception:
                window = None
            if isinstance(window, int) and window > 0:
                self._context_limit = window
        limit = self._context_limit or 16384
        text, colour = format_context_usage(used, limit)
        # An empty colour from the helper means "use the widget's muted default".
        colour = colour or theme.muted_color()
        self._context_label.setText(text)
        self._context_label.setStyleSheet(f"color:{colour}; font-size:9pt;")

    def _on_error(self, message: str) -> None:
        self._teardown_worker()
        self._set_busy(False)
        self._work_timer.stop()
        self._status.setText("")
        # The send failed/cancelled — drop the unanswered user turn.
        self._session.drop_last_user_turn()
        # Keep any partial streamed reply visible above the error notice.
        if self._stream_buffer:
            self._blocks.append(("Assistant", self._stream_buffer))
        self._append_line("System", message)
        self.reply_failed.emit(message)

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._status.setText("Stopping…")

    def _on_skill_changed(self, index: int) -> None:
        """Combo path: switch the active skill. (The combo is disabled while a
        request is in flight, so this never fires mid-send.)"""
        if not (0 <= index < len(self._skills)):
            return
        self._switch_to(self._skills[index])

    def _switch_to(self, skill) -> None:
        """Shared skill-switch body (combo path + programmatic ``set_skill``):
        fresh session + header, cleared transcript + token readout, fresh
        OpenCode conversation. Emits ``conversation_reset``."""
        self._skill = skill
        self._session = SkillChatSession(self._skill)
        self._new_backend_session()  # new skill → fresh OpenCode conversation
        self._latest_draft = ""
        self._stream_buffer = ""
        self._blocks = []
        self._transcript.clear()
        self._context_label.setText("")  # new session → drop the previous skill's token readout
        if self._header is not None:
            self._header.setText((self._skill.when_to_use or "") if self._skill else "")
        self._set_busy(False)  # fresh session → Run re-enabled
        self.conversation_reset.emit()

    def _on_restart_backend(self) -> None:
        """Stop + start the shared OpenCode server to recover from a hung or
        unresponsive backend — without relaunching the whole editor (the only
        recovery before). The trash icon resets the chat SESSION only, which
        can't fix a wedged server. Runs off the UI thread (the WSL spawn takes a
        few seconds); the next send re-creates the backend with a fresh session
        against the new server."""
        sm = getattr(self._backend_factory, "server_manager", None)
        if sm is None:
            self._status.setText("This backend has no restartable server.")
            return
        # Cancel any in-flight (possibly hung) request and drop the cached
        # backend so the next send reconnects to the fresh server.
        if self._worker is not None:
            self._worker.cancel()
        self._teardown_worker()
        self._set_busy(False)
        self._work_timer.stop()
        self._backend = None
        self._status.setText("Restarting backend…")

        def _restart() -> None:
            try:
                sm.stop()
                sm.start()
            except Exception:
                log.exception("backend restart failed")

        threading.Thread(target=_restart, daemon=True).start()
        self._status.setText("Backend restarting — send again in a moment.")

    # -- rich-text transcript rendering --------------------------------------

    @staticmethod
    def _block_html(speaker: str, text: str) -> str:
        """Render one message as a role-tinted HTML block: an accent left-border,
        a role-tinted background, a bold coloured role label with an avatar glyph,
        then the HTML-escaped body. All colours come from the theme so the block
        stays readable in both the light and dark Fluent skins."""
        role, name, glyph = _ROLE_META.get(speaker, ("system", speaker, "•"))
        accent = _role_accent(role)
        bg = theme.message_bg(role)
        border = theme.message_border(role)
        body = html.escape(text).replace("\n", "<br>")
        return (
            f'<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="margin:4px 0;"><tr><td '
            f'style="background:{bg}; border:1px solid {border}; '
            f'border-left:4px solid {accent}; padding:6px 10px;">'
            f'<div style="color:{accent}; font-weight:bold; '
            f'margin-bottom:3px;">{glyph}&nbsp;{html.escape(name)}</div>'
            f'<div style="color:{theme.chat_content_color()};">{body}</div>'
            f'</td></tr></table>'
        )

    def _repaint(self, live: Optional[tuple[str, str]] = None) -> None:
        """Repaint the whole transcript from ``self._blocks`` plus an optional
        in-progress ``live`` (speaker, text) block for the streaming reply."""
        blocks = list(self._blocks)
        if live is not None:
            blocks.append(live)
        self._transcript.setHtml(
            "".join(self._block_html(sp, tx) for sp, tx in blocks))
        self._scroll_to_end()

    def _render_transcript(self) -> None:
        """Rebuild the block list from the session (source of truth) and repaint.
        This drops transient System lines once the authoritative turns land —
        matching the prior plain-text behaviour."""
        self._blocks = [
            ("You" if turn.role == "user" else "Assistant", turn.content)
            for turn in self._session.turns
        ]
        self._repaint()

    def _append_line(
        self, speaker: str, text: str, newline_before: bool = False
    ) -> None:
        # ``newline_before`` is a no-op now (blocks have their own spacing); the
        # parameter is kept so existing call sites don't change.
        self._blocks.append((speaker, text))
        self._repaint()

    def _scroll_to_end(self) -> None:
        bar = self._transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- state ----------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._send_btn.setEnabled(not busy)
        self._input.setEnabled(not busy)
        self._stop_btn.setEnabled(busy)
        self._trash_btn.setEnabled(not busy)
        if self._skill_combo is not None:
            self._skill_combo.setEnabled(not busy)  # no skill-switch mid-request
        # Run only kicks off a fresh conversation; once started, use Send.
        if self._run_btn is not None:
            self._run_btn.setEnabled(not busy and not self._session.started)
        self.busy_changed.emit(busy)

    def _teardown_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
