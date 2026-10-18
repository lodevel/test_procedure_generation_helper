"""The project's engine processes: one per project and role, per app process.

Every wheel op the app asks of a project (parse, render, validate, codegen,
export rows…) is a frame to that project's TOOLS instance,
``llm/_execute_op_subprocess.py --role tools`` in the project venv, instead of a
fresh Python per call (docs/plans/engine_process.md). An instance starts on first
use and is replaced when it died or its key moved: the role, the interpreter or
the bundle (``display_snapshot.bundle_key``). Bench and Studio are separate OS
processes, so they never share one. An instance exits when its stdin closes,
however this process ends.

A TOOLS instance holds nothing. When a warm one (already running before the
frame) dies under a frame, the op is retried once in a fresh one; a fresh one
that dies would die again. A frame past its deadline kills the instance and is
not retried: a slow op would only cost the wait twice. Either loss reads as
``ParserUnavailable``. One lock per project and role serialises callers, as the
daemon is single-threaded. Each instance claims its venv for its pid
(``venv_pool.engine_token`` names this app), so a delete in another window
refuses while it runs, and a delete in this one stops it first (:func:`stop`).
"""
from __future__ import annotations

import atexit
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from procedo.core import display_snapshot

from . import pack_parsers

_LOST = ("SessionDead", "SessionTimeout")


class _Slot:
    """One project's place for one role: its lock and its instance with the key it started under."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.key: Optional[tuple] = None
        self.session: Optional[pack_parsers.RemoteSession] = None


_slots: dict[tuple[str, str], _Slot] = {}
_slots_lock = threading.Lock()


def tools_op(project_root: Path, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    """``spec`` (an ``_engine_ops`` op) answered by the project's TOOLS instance: the
    op's result dict, or ``{"ok": False, "kind", "error"}``. Raises
    ParserUnavailable when the project has no venv."""
    return _op("tools", Path(project_root), spec, timeout)


def _op(role: str, project_root: Path, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    venv = pack_parsers._resolve_project_python(project_root)
    key = (role, str(venv.python), display_snapshot.bundle_key(project_root))
    slot = _slot(role, project_root)
    with slot.lock:
        if slot.session is None or slot.session.dead or slot.key != key:
            _replace(slot, key, role, project_root)
        warm = slot.session.alive
        result = slot.session.engine_op(timeout=timeout, **spec)
        if result.get("kind") == "SessionDead" and warm:
            _replace(slot, key, role, project_root)
            result = slot.session.engine_op(timeout=timeout, **spec)
    if result.get("kind") in _LOST:
        return {**result, "kind": "ParserUnavailable"}
    return result


def _slot(role: str, project_root: Path) -> _Slot:
    name = (role, os.path.normcase(os.path.abspath(project_root)))
    with _slots_lock:
        return _slots.setdefault(name, _Slot())


def _replace(slot: _Slot, key: tuple, role: str, project_root: Path) -> None:
    """A fresh instance in ``slot``; the one it replaces exits (a dead one only loses its folder)."""
    if slot.session is not None:
        _retire(slot.session)
    slot.key, slot.session = key, pack_parsers.RemoteSession(None, project_root, role=role)


def _retire(session: pack_parsers.RemoteSession) -> None:
    """End an instance and drop its folder; one that already died is not started
    again just to be told to stop (``shutdown`` would lazily restart it)."""
    if session.alive:
        session.shutdown()
    else:
        session.kill()
        shutil.rmtree(session.screenshot_dir, ignore_errors=True)


def stop(venv_folder: Path) -> None:
    """End this app's instances whose interpreter lives in ``venv_folder``, so the
    folder can be deleted; the next op starts afresh wherever its venv then is."""
    folder = os.path.normcase(os.path.abspath(venv_folder)) + os.sep
    for slot in list(_slots.values()):
        with slot.lock:
            if slot.key and os.path.normcase(os.path.abspath(slot.key[1])).startswith(folder):
                _retire(slot.session)
                slot.key = slot.session = None


def _stop_all() -> None:
    """End every instance at exit, before ``temp_root`` removes their folders:
    Windows cannot delete a running process's working folder."""
    for slot in list(_slots.values()):
        if slot.session is not None:
            slot.session.close()


# Registered after temp_root's (pack_parsers imports it), so it runs first.
atexit.register(_stop_all)
