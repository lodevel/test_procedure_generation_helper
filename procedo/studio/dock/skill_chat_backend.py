"""The dedicated backend of one skill chat (:class:`~procedo.studio.dock.skill_chat_widget.
SkillChatWidget`): started off the GUI thread on the first send, its own OpenCode session, kept
for the chat's lifetime so the session is stable across turns, a fresh session on a reset, the
shared server's relaunch, the stop."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject

from .backend_request import SERVER_STARTING_TEXT, request_backend

log = logging.getLogger(__name__)

#: The Conversation card's cue while a turn is in flight.
WORKING = "Working\u2026"


def _stop(backend) -> None:
    """Stop a started backend nobody uses."""
    if hasattr(backend, "stop"):
        try:
            backend.stop()
        except Exception:
            log.exception("skill-chat backend stop failed")


class SkillChatBackend(QObject):
    """One skill chat's backend (``backend``: None until one started and was kept); *cue* is
    the chat's loading cue, which says "Starting the AI server…" while a turn waits for it."""

    def __init__(self, factory, tab_id: str, cue, parent=None) -> None:
        super().__init__(parent)
        self.factory = factory
        self.tab_id = tab_id
        self.backend = None
        self._cue = cue
        self._starting = cue.hold(SERVER_STARTING_TEXT)
        self._queued = None  # fire(backend) of the turn waiting for the backend's start
        self._requested = False  # a start is out
        self._closed = False

    def await_backend(self, fire) -> None:
        """``fire(backend)`` now, or once the backend has started off the GUI thread
        (``request_backend``), the cue saying so meanwhile — mirrors the Studio tab's
        ``_await_backend``; the chat sends no second turn meanwhile (``is_busy``)."""
        if self.backend is not None:
            fire(self.backend)
            return
        self._queued = fire
        self._starting.set(True)
        if not self._requested:
            self._requested = True
            request_backend(self.factory, self.tab_id, self._on_started)

    @property
    def waiting(self) -> bool:
        """A turn waits for the backend's start."""
        return self._queued is not None

    def drop_queued(self) -> bool:
        """Drop the turn waiting for the backend (its start goes on): True when one was."""
        queued, self._queued = self._queued, None
        if self._starting.on:
            self._starting.set(False)
            self._cue.set_label(WORKING)
        return queued is not None

    def _on_started(self, answer) -> None:
        """The start answered (GUI thread): kept as ``start_backend`` says, the queued turn
        fired on it (on a fallback ``NoneBackend`` its reply is the server's failure). One
        nobody uses — the chat shut down meanwhile, or not kept and no turn waiting — is
        stopped."""
        _factory, backend, keep = answer
        self._requested = False
        fire = None if self._closed else self._queued
        self.drop_queued()
        if keep and not self._closed and self.backend is None:
            self.backend = backend
        elif fire is None:
            _stop(backend)
        if fire is not None:
            fire(backend)

    def ensure_session(self) -> None:
        """Make sure the cached backend has a LIVE session, creating one only if it
        has none — the persistent-session counterpart of ``new_session``. The
        session is REUSED across sends so OpenCode keeps the whole conversation
        (incl. MCP tool results) instead of throwing it away each turn; after a
        server restart the same id reattaches (OpenCode persists sessions on disk)."""
        backend = self.backend
        ensure = getattr(backend, "ensure_session", None) if backend else None
        if callable(ensure):
            try:
                ensure()
            except Exception:
                log.exception("skill-chat ensure_session failed; sending anyway")

    def new_session(self) -> None:
        """Start a FRESH server session to TRUE-reset the conversation (trash /
        skill-switch), discarding OpenCode's history. Prefers the cheap
        ``new_session`` (re-POSTs /session); never ``reset_session`` (which stops +
        restarts the whole server). No-op when no backend exists yet — the next send
        mints a fresh one."""
        backend = self.backend
        new_session = getattr(backend, "new_session", None) if backend else None
        if callable(new_session):
            try:
                new_session()
            except Exception:
                log.exception("skill-chat new_session failed; continuing")

    def relaunch_server(self) -> bool:
        """Relaunch the shared OpenCode server off the UI thread (the WSL spawn takes a few
        seconds) and drop the cached backend so the next send reconnects to the fresh server
        with a fresh session. ``relaunch`` keeps the manager usable — a ``stop()`` would
        retire it and leave every tab without a server. False: the factory has no
        restartable server."""
        sm = getattr(self.factory, "server_manager", None)
        if sm is None:
            return False
        self.backend = None

        def _restart() -> None:
            try:
                sm.relaunch()
            except Exception:
                log.exception("backend restart failed")

        threading.Thread(target=_restart, daemon=True).start()
        return True

    def shutdown(self) -> None:
        """Stop the backend so the private OpenCode session doesn't leak; a turn waiting for
        its start never fires, and what that start answers is stopped."""
        self._closed = True
        self.drop_queued()
        if self.backend is not None:
            _stop(self.backend)
            self.backend = None
