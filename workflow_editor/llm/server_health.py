"""Classified health/errors for the OpenCode server lifecycle, plus free-port
selection.

Split out from :mod:`.server_manager` so the value objects, the human messages,
and port selection are pure and unit-testable — the manager does the subprocess
work and feeds results through :class:`ServerStatus` / :func:`classify_install`.
The whole point is that when the server won't start the user sees WHY (WSL
missing, opencode missing, port busy, timeout) instead of a silent dud.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from enum import Enum


class ServerError(Enum):
    """Why the OpenCode server is not usable. ``NONE`` means healthy."""

    NONE = "none"
    WSL_MISSING = "wsl_missing"
    OPENCODE_MISSING = "opencode_missing"
    PORT_IN_USE = "port_in_use"
    START_TIMEOUT = "start_timeout"
    START_FAILED = "start_failed"


_MESSAGES = {
    ServerError.WSL_MISSING: (
        "WSL is not available. The OpenCode backend runs in WSL — install WSL "
        "(or check the configured wsl path) and try again."
    ),
    ServerError.OPENCODE_MISSING: (
        "OpenCode was not found in the WSL PATH. Install it (e.g. "
        "`npm i -g opencode-ai`) or fix your PATH, then retry."
    ),
    ServerError.PORT_IN_USE: (
        "The OpenCode server could not bind its port — it is already in use."
    ),
    ServerError.START_TIMEOUT: (
        "The OpenCode server did not become ready in time."
    ),
    ServerError.START_FAILED: (
        "The OpenCode server process failed to start."
    ),
}


@dataclass(frozen=True)
class ServerStatus:
    """Outcome of a start/availability check: healthy, or a classified failure
    with an optional ``detail`` (captured stderr / exception text)."""

    ok: bool
    error: ServerError = ServerError.NONE
    detail: str = ""

    @property
    def message(self) -> str:
        """A user-facing sentence (failure reason + any captured detail)."""
        if self.ok:
            return "OpenCode server ready."
        base = _MESSAGES.get(self.error, "The OpenCode server is unavailable.")
        return f"{base}\n\n{self.detail.strip()}" if self.detail.strip() else base

    @classmethod
    def healthy(cls) -> "ServerStatus":
        return cls(ok=True)

    @classmethod
    def failure(cls, error: ServerError, detail: str = "") -> "ServerStatus":
        return cls(ok=False, error=error, detail=detail)


def classify_install(wsl_ok: bool, opencode_ok: bool) -> ServerError:
    """Map the two installation checks to a reason (``NONE`` if both pass)."""
    if not wsl_ok:
        return ServerError.WSL_MISSING
    if not opencode_ok:
        return ServerError.OPENCODE_MISSING
    return ServerError.NONE


def is_port_conflict(stderr: str) -> bool:
    """Heuristic: does this server stderr indicate the port was already taken?
    Used to upgrade a generic start failure to :data:`ServerError.PORT_IN_USE`."""
    s = stderr.lower()
    return any(m in s for m in ("address already in use", "eaddrinuse", "address in use"))


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # SO_REUSEADDR so a port lingering in TIME_WAIT isn't reported busy.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(preferred: int = 0, host: str = "127.0.0.1") -> int:
    """Return a free TCP port.

    Decision Q1: with the default ``preferred=0`` (the lifecycle's only caller)
    the OS always assigns the port — a stray on the old saved port is never
    silently reused, and the saved Port setting is cosmetic. A non-zero
    ``preferred`` is honoured when free (kept for callers/tests that want it),
    else the OS assigns one.

    There is an unavoidable TOCTOU gap between probing and the server binding;
    the caller treats a later bind failure as :data:`ServerError.PORT_IN_USE`
    and retries with a fresh OS-assigned port (Windows-probe / WSL2-bind
    namespaces can disagree)."""
    if preferred and _port_is_free(preferred, host):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]
