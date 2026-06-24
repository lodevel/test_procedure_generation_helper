"""Skill-chat dialog — a modeless window that runs ONE skill conversation.

A skill chat is a plain multi-turn conversation: the skill's ``SKILL.md`` system
prompt plus the user's chosen context, then the whole transcript so far. Replies
are prose (no JSON contract). When the operator likes a draft they click *Insert
into procedure* and it is raw-appended to the procedure editor via a callback.

This module is the thin Qt controller; the brain lives elsewhere and is reused,
not reimplemented:

* :class:`~workflow_editor.authoring.SkillChatSession` — turn logic + prompt
  assembly (``start_user_turn`` / ``record_assistant`` / ``interpret``).
* :func:`~workflow_editor.authoring.assemble` — turns the picker's selection into
  the pushed context text.
* :class:`~workflow_editor.widgets.skill_context_picker.SkillContextPicker` — the
  checkable context browser.
* :class:`~workflow_editor.llm.worker.LLMWorker` — runs the send off the UI
  thread; we drive it with an ``LLMRequest(raw_prompt=...)`` so the backend
  bypasses its JSON output contract.

Backend ownership (controller contract): the dialog creates and owns a DEDICATED
backend via ``backend_factory.create_backend(tab_id="skill_chat")`` so the skill
chat has its OWN OpenCode session, independent of the dock chat. Because each
turn re-sends the full transcript, the session is reset before every send (when
the backend exposes ``reset_session``) so the server doesn't also prepend its own
history and double-count it. Backends without a reset API are sent to as-is — for
the stateless external API that is already correct.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPlainTextEdit, QPushButton, QCheckBox, QComboBox, QLabel, QWidget,
)

from .. import theme
from ..authoring import Skill, SkillChatSession, assemble
from ..widgets.skill_context_picker import SkillContextPicker
from ..llm.backend_base import LLMRequest, LLMTask
from ..llm.worker import LLMWorker

log = logging.getLogger(__name__)


class SkillChatDialog(QDialog):
    """Modeless window driving one run of one authoring skill.

    The window is non-modal (``show()``, not ``exec()``) so the operator can
    keep editing the procedure while the chat is open. It owns a private backend
    + :class:`SkillChatSession`; closing it stops the backend.
    """

    def __init__(
        self,
        skills: list,
        sources: list,
        backend_factory,
        documents_dir,
        insert_callback: Callable[[str], None],
        parent=None,
    ) -> None:
        """
        Args:
            skills: The discovered skills to choose from (selected in-dialog via
                a combobox — scales to many; the first is active initially).
            sources: Context sources (one picker tab each).
            backend_factory: Factory exposing ``create_backend(tab_id=...)``;
                used to mint a dedicated ``"skill_chat"`` backend/session.
            documents_dir: Folder backing the picker's Documents tab (or None).
            insert_callback: Called with the latest assistant draft when the
                operator clicks *Insert into procedure* — the caller wires this
                to the text tab's editor append + artifact sync. The dialog does
                NOT import the text tab.
            parent: Parent widget (the window stays modeless regardless).
        """
        super().__init__(parent)
        self._skills: list[Skill] = list(skills)
        self._skill = self._skills[0]  # active skill (switchable in-dialog)
        self._backend_factory = backend_factory
        self._insert_callback = insert_callback
        self._session = SkillChatSession(self._skill)

        # Lazily-created dedicated backend + the in-flight worker.
        self._backend = None
        self._worker: Optional[LLMWorker] = None
        # The most recent assistant draft (what *Insert* appends). Empty until
        # the first reply lands.
        self._latest_draft: str = ""
        # Accumulates streamed response text for the live "Assistant: ..." line.
        self._stream_buffer: str = ""

        self.setWindowTitle("Skill chat")
        self.setModal(False)
        self.resize(900, 680)

        self._picker = SkillContextPicker(sources, documents_dir=documents_dir)
        self._setup_ui()

    # -- UI construction ------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Skill selector — choose which skill to run here (scales to many skills;
        # a flat menu of every skill doesn't).
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Skill:"))
        self._skill_combo = QComboBox()
        for s in self._skills:
            self._skill_combo.addItem(s.title or s.skill_id, s.skill_id)
        self._skill_combo.currentIndexChanged.connect(self._on_skill_changed)
        sel_row.addWidget(self._skill_combo, 1)
        layout.addLayout(sel_row)

        self._header = QLabel(self._skill.when_to_use or "")
        self._header.setWordWrap(True)
        self._header.setStyleSheet(f"color:{theme.muted_color()};")
        layout.addWidget(self._header)

        # Transcript (left) | context picker (right).
        split = QSplitter(Qt.Orientation.Horizontal)

        transcript_group = QGroupBox("Conversation")
        tg_layout = QVBoxLayout(transcript_group)
        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText(
            "The skill conversation appears here. Ask for a draft, refine it "
            "over a few turns, then Insert it into the procedure."
        )
        tg_layout.addWidget(self._transcript)
        split.addWidget(transcript_group)

        context_group = QGroupBox("Context to push")
        cg_layout = QVBoxLayout(context_group)
        cg_layout.addWidget(self._picker)
        split.addWidget(context_group)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([560, 340])
        layout.addWidget(split, stretch=1)

        # Web-search toggle — placeholder until the OpenCode web tools land;
        # kept visible so the planned capability is discoverable.
        self._web_checkbox = QCheckBox("🌐 Web search  (coming soon)")
        self._web_checkbox.setToolTip(
            "Let the skill search the web. Not wired yet — needs the OpenCode "
            "web-tool config (a later phase)."
        )
        self._web_checkbox.setEnabled(False)
        layout.addWidget(self._web_checkbox)

        # Input box (Enter sends, Shift+Enter newline — mirrors ChatPanel).
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Ask the skill for a draft or a change...")
        self._input.setFixedHeight(72)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        # Action row.
        btn_row = QHBoxLayout()

        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        btn_row.addWidget(self._send_btn)

        self._stop_btn = self._icon_button(
            "⏹️", "Stop the current request", self._on_stop)
        self._stop_btn.setEnabled(False)
        btn_row.addWidget(self._stop_btn)

        self._trash_btn = self._icon_button(
            "🗑️", "Clear the conversation and start over", self._on_trash)
        btn_row.addWidget(self._trash_btn)

        btn_row.addStretch()

        self._insert_btn = QPushButton("Insert into procedure")
        self._insert_btn.setToolTip(
            "Append to the procedure: your highlighted selection, or the whole "
            "latest draft if nothing is selected."
        )
        self._insert_btn.setEnabled(False)
        self._insert_btn.clicked.connect(self._on_insert)
        btn_row.addWidget(self._insert_btn)

        layout.addLayout(btn_row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.muted_color()}; font-size:9pt;")
        layout.addWidget(self._status)

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

    # -- backend lifecycle ----------------------------------------------------

    def _ensure_backend(self):
        """Lazily mint + start the dedicated ``"skill_chat"`` backend.

        Mirrors :class:`TabContext`'s lazy-create-then-start pattern. The
        backend is cached for the dialog's lifetime so the OpenCode session is
        stable across turns (we reset it per send, not recreate it)."""
        if self._backend is None:
            self._backend = self._backend_factory.create_backend(tab_id="skill_chat")
            if hasattr(self._backend, "start"):
                self._backend.start()
        return self._backend

    def _reset_backend_session(self) -> None:
        """Start a FRESH session before a send so the server doesn't double-count
        history (each turn already carries the full transcript).

        Prefers the cheap ``new_session`` (just re-POSTs /session); deliberately
        does NOT use ``reset_session`` (it stops + restarts the whole server).
        No-op for stateless backends (external API) — already correct there."""
        backend = self._backend
        new_session = getattr(backend, "new_session", None) if backend else None
        if callable(new_session):
            try:
                new_session()
            except Exception:
                log.exception("skill-chat new_session failed; sending anyway")

    # -- send path ------------------------------------------------------------

    def _on_send(self) -> None:
        if self._worker is not None:
            return  # a request is already in flight
        message = self._input.toPlainText().strip()
        if not message:
            return

        # Push the latest context selection (only the first turn actually
        # carries it; harmless to refresh before the conversation starts).
        bundle = assemble(self._picker.selections())
        self._session.set_context(bundle.text)

        prompt = self._session.start_user_turn(message)
        self._input.clear()
        self._append_line("You", message)

        backend = self._ensure_backend()
        self._reset_backend_session()

        request = LLMRequest(task=LLMTask.AD_HOC_CHAT, raw_prompt=prompt)
        self._worker = LLMWorker(backend, request, parent=self)
        self._worker.text_chunk.connect(self._on_text_chunk)
        self._worker.thinking_chunk.connect(self._on_thinking_chunk)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._stream_buffer = ""
        self._set_busy(True)
        self._status.setText("Thinking…")
        self._worker.start()

    def _on_text_chunk(self, text: str) -> None:
        """Show response text progressively so the operator isn't staring at
        silence. The final, authoritative text is set in :meth:`_on_finished`."""
        if not self._stream_buffer:
            self._append_line("Assistant", text, newline_before=True)
        else:
            self._transcript.moveCursor(QTextCursor.MoveOperation.End)
            self._transcript.insertPlainText(text)
            self._scroll_to_end()
        self._stream_buffer += text

    def _on_thinking_chunk(self, _text: str) -> None:
        # Reasoning chunks are not displayed in the transcript; just keep the
        # status line alive so the window doesn't look frozen.
        self._status.setText("Thinking…")

    def _on_finished(self, response) -> None:
        self._teardown_worker()
        self._set_busy(False)
        self._status.setText("")

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
            self._append_line("System", reason, newline_before=bool(self._stream_buffer))
            self._session.drop_last_user_turn()
            return

        text = SkillChatSession.interpret(response)
        self._session.record_assistant(text)
        self._latest_draft = text
        self._insert_btn.setEnabled(bool(text.strip()))

        # Replace the streamed preview (if any) with the authoritative text by
        # re-rendering the whole transcript from the session — cheap and avoids
        # drift between the live stream and the final parse.
        self._render_transcript()

    def _on_error(self, message: str) -> None:
        self._teardown_worker()
        self._set_busy(False)
        self._status.setText("")
        # The send failed/cancelled — drop the unanswered user turn.
        self._session.drop_last_user_turn()
        self._append_line(
            "System", message, newline_before=bool(self._stream_buffer)
        )

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._status.setText("Stopping…")

    def _on_skill_changed(self, index: int) -> None:
        """Switch the active skill: fresh session + header, cleared transcript.
        (The combo is disabled while a request is in flight, so this never fires
        mid-send.)"""
        if not (0 <= index < len(self._skills)):
            return
        self._skill = self._skills[index]
        self._session = SkillChatSession(self._skill)
        self._latest_draft = ""
        self._stream_buffer = ""
        self._insert_btn.setEnabled(False)
        self._transcript.clear()
        self._header.setText(self._skill.when_to_use or "")

    def _on_trash(self) -> None:
        """Clear the conversation and start a fresh session (same skill)."""
        if self._worker is not None:
            self._worker.cancel()
            self._teardown_worker()
            self._set_busy(False)
        self._session = SkillChatSession(self._skill)
        self._latest_draft = ""
        self._stream_buffer = ""
        self._insert_btn.setEnabled(False)
        self._transcript.clear()
        self._status.setText("")

    # -- insert ---------------------------------------------------------------

    def _on_insert(self) -> None:
        """Append to the procedure: the transcript SELECTION if the operator
        highlighted one (e.g. just the steps, dropping the chatter), otherwise
        the whole latest draft."""
        cursor = self._transcript.textCursor()
        if cursor.hasSelection():
            # QPlainTextEdit returns paragraph breaks as U+2029.
            draft = cursor.selectedText().replace("\u2029", "\n").strip()
            what = "selection"
        else:
            draft = (self._latest_draft or "").strip()
            what = "latest draft"
        if not draft:
            return
        try:
            self._insert_callback(draft)
        except Exception:
            log.exception("skill-chat insert_callback failed")
            self._append_line("System", "Insert failed — see logs.")
            return
        self._status.setText(f"Inserted {what} into the procedure.")

    # -- transcript helpers ---------------------------------------------------

    def _render_transcript(self) -> None:
        """Re-render the whole transcript from the session (source of truth)."""
        self._transcript.clear()
        for turn in self._session.turns:
            speaker = "You" if turn.role == "user" else "Assistant"
            self._transcript.appendPlainText(f"{speaker}: {turn.content}\n")
        self._scroll_to_end()

    def _append_line(
        self, speaker: str, text: str, newline_before: bool = False
    ) -> None:
        if newline_before:
            self._transcript.appendPlainText("")
        self._transcript.appendPlainText(f"{speaker}: {text}")
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        bar = self._transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- state ----------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._send_btn.setEnabled(not busy)
        self._input.setEnabled(not busy)
        self._stop_btn.setEnabled(busy)
        self._trash_btn.setEnabled(not busy)
        self._skill_combo.setEnabled(not busy)  # no skill-switch mid-request

    def _teardown_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    # -- cleanup --------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 — Qt override
        """Stop the in-flight request and the dedicated backend on close so the
        private OpenCode session doesn't leak."""
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
        super().closeEvent(event)
