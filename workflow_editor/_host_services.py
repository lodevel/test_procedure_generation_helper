"""Guarded access to the HOST app's ``project_services`` package.

Embedded runs always have ``project_services`` (the host installs this editor
editable into its own venv). A STANDALONE editor checkout may not — every
host-backed feature must degrade gracefully instead of crashing on a menu
click. Three seams, all funnelled through one availability probe + one
user-facing message:

* :func:`requires_host` — decorator for parameterless ``MainWindow`` slots
  whose whole feature lives host-side (shared dialogs, full report). Missing
  host => one informational QMessageBox, the slot becomes a no-op.
* :func:`load_optional` — silent import for nice-to-haves (shared recents);
  returns the module or ``None``, never raises, never pops UI.
* :func:`note_recent_project` — the shared-recents write, a no-op standalone.
"""
from __future__ import annotations

import functools
import importlib
import importlib.util
import logging

log = logging.getLogger(__name__)

HOST_REQUIRED_MSG = (
    "This feature requires the host app (project_services). "
    "Standalone editor runs without it."
)


def host_available() -> bool:
    """True when the host's ``project_services`` package is importable."""
    try:
        return importlib.util.find_spec("project_services") is not None
    except (ImportError, ValueError):
        return False


def requires_host(method):
    """Gate a parameterless MainWindow slot on the host app.

    When ``project_services`` is missing the slot shows one informational
    QMessageBox (parented to ``self``) and returns ``None``. Signal payloads
    (e.g. ``triggered``'s ``checked``) are swallowed, exactly like the
    undecorated ``(self)``-only slots did.
    """

    @functools.wraps(method)
    def _gated(self, *_args, **_kwargs):
        if host_available():
            return method(self)
        log.info("host-only feature %s unavailable standalone", method.__name__)
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(self, "Host app required", HOST_REQUIRED_MSG)
        return None

    return _gated


def load_optional(module_name: str):
    """Import a host module, or return ``None`` (silently) when unavailable."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        log.debug("optional host module %s unavailable", module_name)
        return None


def note_recent_project(path_str: str) -> None:
    """Record *path_str* in the SHARED recents (app_settings); no-op standalone."""
    aps = load_optional("project_services.app_settings")
    if aps is not None:
        aps.add_recent_project(path_str)
