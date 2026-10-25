"""The Qt side of a backend's start: ``backend_factory.start_backend`` (Qt-free) run off the GUI
thread and answered on it, for the Studio tabs and the skill chats."""

from __future__ import annotations

import logging

from procedo.core.off_gui import run_off_gui

from ..llm.backend_base import NoneBackend
from ..llm.backend_factory import BACKEND_TYPE_NONE, start_backend

log = logging.getLogger(__name__)

#: What a chat shows while its send waits for the AI server to start (:func:`request_backend`).
SERVER_STARTING_TEXT = "Starting the AI server…"


def request_backend(factory, tab_id: str, on_answer) -> None:
    """``on_answer((factory, *start_backend(factory, tab_id)))`` (a QObject's bound method)
    without the GUI thread ever waiting on a server start: at once when no server is involved
    (no factory, or the backend is off), else from a worker (``run_off_gui``; the factory
    shares one server start among the threads asking meanwhile). A start that raised answers
    a fallback ``NoneBackend`` carrying what it raised."""

    def _start() -> tuple:
        try:
            return (factory, *start_backend(factory, tab_id))
        except Exception as exc:
            log.exception(f"Starting the backend for '{tab_id}' failed")
            return factory, NoneBackend(reason=str(exc) or type(exc).__name__), False

    if factory is None or factory.backend_type == BACKEND_TYPE_NONE:
        on_answer(_start())
    else:
        run_off_gui(_start, on_answer, name=f"start-backend-{tab_id}")
