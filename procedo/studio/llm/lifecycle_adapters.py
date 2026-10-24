"""Desktop server-lifecycle adapters for the shared ``opencode_client`` backend.

``opencode_client.OpenCodeBackend`` only ever reads a URL — it never spawns a
server. HOW an ``opencode serve`` comes to exist behind that URL is a
:class:`~opencode_client.lifecycle.ServerLifecycle`. The desktop has two shapes,
both preserved here byte-for-byte from the pre-migration ``OpenCodeBackend``:

* **Shared manager (normal chat).** :class:`OpenCodeServerManager` owns ONE
  ``opencode serve`` shared across all tabs (free-port pick, install
  classification, health / orphan sweep). :class:`ManagerLifecycle` binds the
  seam to it; each per-tab backend just reads its URL and mints its own session.
  ``stop()`` is a no-op — the tabs share the server and the manager owns
  teardown (the same reasoning as :class:`RemoteServerLifecycle`).
* **Self-spawn (no manager / the Settings "Test Connection" probe).**
  :func:`make_wsl_lifecycle` reproduces the desktop's historical
  ``wsl bash -ic "OPENCODE_ENABLE_EXA=1 opencode serve --port P --hostname H"``
  spawn in the launch dir, polling ``/health``, pointed at this Studio process's
  launch config with ``OPENCODE_CONFIG``.

These live in the desktop app (not the package) on purpose: the migration rule
is to express a host-only need as an adapter in that host, never by forking the
shared package module.
"""

from __future__ import annotations

import shlex
from typing import Optional

from opencode_client.lifecycle import (
    ServerLifecycle,
    ServerUnavailable,
    SubprocessServerLifecycle,
)

from .opencode_backend import OpenCodeConfig, safe_wsl_cwd
from .launch_files import config_wsl_path


def make_wsl_lifecycle(config: OpenCodeConfig) -> SubprocessServerLifecycle:
    """A lifecycle that self-spawns ``opencode serve`` inside WSL, exactly as the
    pre-migration desktop backend did.

    Produces the argv ``[wsl, "bash", "-ic", "OPENCODE_ENABLE_EXA=1
    [OPENCODE_CONFIG=<this process's launch config>] opencode serve --port <port>
    --hostname <host>"]`` with ``cwd`` = the launch dir, polling ``/health`` for
    readiness — the endpoint the desktop has always used. The config is named
    explicitly: each Studio process's has its own name (``config_wsl_path``), which
    OpenCode never discovers in the cwd.
    """
    env = {"OPENCODE_ENABLE_EXA": "1"}
    if config.working_directory:
        env["OPENCODE_CONFIG"] = shlex.quote(config_wsl_path(config.working_directory))
    return SubprocessServerLifecycle(
        project_cwd=config.working_directory or safe_wsl_cwd(),
        host=config.server_hostname,
        port=config.server_port,
        command_prefix=[config.wsl_path, "bash", "-ic"],
        shell_wrap=True,
        env_inline=env,
        health_path="/health",
    )


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
