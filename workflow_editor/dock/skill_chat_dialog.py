"""Skill-chat dialog — a modeless window that runs ONE skill conversation.

This is now a THIN host: the entire chat body (transcript, input, the
web/save_docs/project_tools toggles, the context picker, the Run/Send/Stop/
Trash/Restart controls, the elapsed-time indicator, the token/context-usage
readout, the LLM worker dispatch + backend lifecycle + ``SkillChatSession``)
lives in the reusable :class:`~workflow_editor.dock.skill_chat_widget.SkillChatWidget`,
so the Skills-menu chat and a hosting wizard share the SAME chat — not two
hand-rolled copies.

The dialog adds only what is window-specific:

* window chrome (title / size / modeless),
* the *Insert into procedure* button, wired to the host's ``insert_callback``
  and gated by the widget's ``reply_finished`` / ``conversation_reset`` signals,
* close-time teardown — a QWidget's ``closeEvent`` does NOT fire when embedded,
  so the dialog forwards its own close to ``widget.shutdown()``.

The public constructor signature is UNCHANGED so ``skill_menu._launch_chat``
keeps working verbatim.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton

from .skill_chat_widget import SkillChatWidget

log = logging.getLogger(__name__)


class SkillChatDialog(QDialog):
    """Modeless window hosting one :class:`SkillChatWidget`.

    The window is non-modal (``show()``, not ``exec()``) so the operator can
    keep editing the procedure while the chat is open. The widget owns a private
    backend + :class:`SkillChatSession`; closing the dialog tears it down via
    :meth:`SkillChatWidget.shutdown`.
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
        self._insert_callback = insert_callback

        self.setWindowTitle("Skill chat")
        self.setModal(False)
        self.resize(900, 680)

        # The whole chat body — native skill combo + Run button visible, context
        # picker built from the supplied sources.
        self._chat = SkillChatWidget(
            list(skills),
            backend_factory,
            sources=sources,
            documents_dir=documents_dir,
            backend_tab_id="skill_chat",
            show_skill_selector=True,
            show_run_button=True,
            parent=self,
        )

        layout = QVBoxLayout(self)
        layout.addWidget(self._chat, stretch=1)

        # Insert button — host chrome. Reads the widget's latest reply; enabled
        # only when there IS a reply to insert.
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._insert_btn = QPushButton("Insert into procedure")
        self._insert_btn.setToolTip(
            "Append the latest draft to the procedure text editor.")
        self._insert_btn.setEnabled(False)
        self._insert_btn.clicked.connect(self._on_insert)
        btn_row.addWidget(self._insert_btn)
        layout.addLayout(btn_row)

        # Gate Insert on the widget's reply lifecycle: enable when an
        # authoritative reply lands (non-empty), disable when the chat resets.
        self._chat.reply_finished.connect(self._on_reply_finished)
        self._chat.conversation_reset.connect(
            lambda: self._insert_btn.setEnabled(False))

    # -- insert ---------------------------------------------------------------

    def _on_reply_finished(self, text: str) -> None:
        self._insert_btn.setEnabled(bool(text.strip()))

    def _on_insert(self) -> None:
        """Raw-append the latest assistant draft to the procedure editor."""
        draft = (self._chat.latest_reply or "").strip()
        if not draft:
            return
        try:
            self._insert_callback(draft)
        except Exception:
            log.exception("skill-chat insert_callback failed")
            self._chat.append_note("System", "Insert failed — see logs.")
            return
        self._chat.set_status("Inserted latest draft into the procedure.")

    # -- cleanup --------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 — Qt override
        """Tear down the embedded widget's worker + dedicated backend on close so
        the private OpenCode session doesn't leak (an embedded QWidget's own
        closeEvent never fires)."""
        self._chat.shutdown()
        super().closeEvent(event)
