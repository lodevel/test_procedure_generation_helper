"""OpenCode HTTP client — single-sourced in ``opencode_client.client``
(migration doc §3b).

The desktop no longer carries its own copy of the client. The one change vs the
historical in-tree backend is the *server-address seam*: the client only reads a
URL now; WHO starts ``opencode serve`` behind it is a
:class:`~opencode_client.lifecycle.ServerLifecycle`. The desktop's two server
shapes — the shared :class:`OpenCodeServerManager` and the historical WSL
self-spawn — are bound to that seam in
:mod:`workflow_editor.llm.lifecycle_adapters` (``ManagerLifecycle`` /
``make_wsl_lifecycle``), so behaviour is unchanged.
"""
from opencode_client.client import (  # noqa: F401
    OpenCodeBackend,
    OpenCodeConfig,
    resolve_context_window,
    safe_wsl_cwd,
)
