"""The dedicated backend of one skill chat (:class:`~procedo.studio.dock.skill_chat_widget.
SkillChatWidget`): its own OpenCode session, kept for the chat's lifetime so the session is
stable across turns, a fresh session on a reset, the shared server's relaunch, the stop."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class SkillChatBackend:
    """One skill chat's backend (``backend``: None until one started and was kept)."""

    def __init__(self, factory, tab_id: str) -> None:
        self.factory = factory
        self.tab_id = tab_id
        self.backend = None

    def adopt(self, backend, keep: bool) -> None:
        """A started backend: kept as ``start_backend`` says."""
        if keep and self.backend is None:
            self.backend = backend

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
        """Stop the backend so the private OpenCode session doesn't leak."""
        if self.backend is not None and hasattr(self.backend, "stop"):
            try:
                self.backend.stop()
            except Exception:
                log.exception("skill-chat backend stop failed")
            self.backend = None
