"""Desktop server-lifecycle adapter for the shared ``opencode_client`` backend.

``opencode_client.OpenCodeBackend`` only ever reads a URL — it never spawns a
server. HOW an ``opencode serve`` comes to exist behind that URL is a
:class:`~opencode_client.lifecycle.ServerLifecycle`. On the desktop the ONE spawner is
:class:`OpenCodeServerManager`: it alone knows the Linux pid of the server it started in
WSL and stops exactly that (terminating ``wsl.exe`` alone can leave the server running).
:class:`ManagerLifecycle` binds the seam to a manager; each backend just reads its URL and
mints its own session. ``stop()`` is a no-op — the manager's owner stops it (the same
reasoning as :class:`RemoteServerLifecycle`).

This lives in the desktop app (not the package) on purpose: the migration rule is to
express a host-only need as an adapter in that host, never by forking the shared package
module.
"""

from __future__ import annotations

from typing import Optional

from opencode_client.lifecycle import ServerLifecycle, ServerUnavailable


class ManagerLifecycle(ServerLifecycle):
    """Bind the client's server seam to the desktop's shared
    :class:`OpenCodeServerManager`.

    The manager owns the one shared server's lifetime; every per-tab backend only
    reads its URL. On failure the manager's classified ``last_status`` (WSL
    missing, ``opencode`` not installed, port in use, …) is surfaced as the
    :class:`ServerUnavailable` message so the UI keeps its precise reasons.
    """

    def __init__(self, manager) -> None:
        self._m = manager

    def ensure_running(self, timeout: float = 30.0) -> str:
        # ``ensure_running`` (re)starts the shared server if it is down; a False
        # return carries a classified reason in ``last_status``.
        if not self._m.ensure_running():
            status = getattr(self._m, "last_status", None)
            reason = getattr(status, "message", None) or "OpenCode server unavailable"
            raise ServerUnavailable(reason)
        return self._m.server_url

    def is_alive(self) -> bool:
        return bool(self._m.is_alive)

    def stop(self) -> None:
        # Tabs SHARE the server; the manager owns teardown. A per-backend stop
        # must NOT kill the shared process (mirrors RemoteServerLifecycle.stop).
        return None

    @property
    def url(self) -> Optional[str]:
        return self._m.server_url
