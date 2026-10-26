"""The project's engine processes: one per project and role, per app process.

Every wheel op the app asks of a project (parse, render, validate, codegen,
export rows…) is a frame to that project's TOOLS instance,
``engine_client/_execute_op_subprocess.py --role tools`` in the project venv, instead of a
fresh Python per call (docs/plans/engine_process.md). An instance starts on first
use and is replaced when it died or the project's bundle moved
(``display_snapshot.kept_bundle_key``: read once, and again only when something said
the bundle may have changed: on a share each look is a dozen round trips). Its venv
is resolved when it starts. An op that WRITES files or results asks ``fresh``: the bundle
and the venv are looked at again first, so it never runs on a superseded engine (a sync
pull replacing wheels sends Studio no event). Bench and
Studio are separate OS processes, so they never share one. An instance exits when its
stdin closes, however this process ends, and ``stop_all`` ends them ahead of the temp
root's removal.

A TOOLS instance holds nothing. When a warm one (already running before the
frame) dies under a frame, the op is retried once in a fresh one; a fresh one
that dies would die again. A frame past its deadline kills the instance and is
not retried: a slow op would only cost the wait twice. Either loss reads as
``ParserUnavailable``. One lock per project and role serialises callers, as the
daemon is single-threaded. Each instance claims its venv for its pid
(``venv_pool.engine_token`` names this app), so a delete in another window
refuses while it runs, and a delete in this one stops it first (:func:`stop`).

The RUN instance holds the instruments (:func:`run`). Its holder — the guided
dialog or the bench session — alone ends it (``shutdown``, or ``kill`` then the
recovery safe-off); the registry never restarts, replaces or stops a live one,
and its claim is a bench holder's, which every Reinstall refuses.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional

from procedo.core import display_snapshot, temp_root

from . import engine_session
from .bench_client import BenchSessionClient

_LOST = ("SessionDead", "SessionTimeout")


class _Slot:
    """One project's place for one role: its lock and its instance with the interpreter and
    the bundle key it started under."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.python: Optional[str] = None
        self.bundle: Optional[tuple] = None
        self.session: Optional[engine_session.RemoteSession] = None
        self.generation = 0  # bumped by every stop(), which works without the lock


_slots: dict[tuple[str, str], _Slot] = {}
_slots_lock = threading.Lock()
_closed = False  # set by stop_all: the app is going, nothing starts again
_CLOSING = {"ok": False, "kind": "ParserUnavailable", "error": "app is closing"}
_STOPPED = {"ok": False, "kind": "ParserUnavailable", "error": "the project's engine was stopped"}


def tools_op(
    project_root: Path, spec: dict[str, Any], timeout: float, fresh: bool = False
) -> dict[str, Any]:
    """``spec`` (an ``_engine_ops`` op) answered by the project's TOOLS instance: the
    op's result dict, or ``{"ok": False, "kind", "error"}``. Raises
    ParserUnavailable when the project has no venv. *fresh*: the op writes files or
    results — the bundle and the venv are looked at again before it (a display read
    runs under what was kept)."""
    return _op("tools", Path(project_root), spec, timeout, fresh)


def _op(
    role: str, project_root: Path, spec: dict[str, Any], timeout: float, fresh: bool
) -> dict[str, Any]:
    if fresh:
        bundle = display_snapshot.read_bundle_key(project_root)
        python = str(engine_session._resolve_project_python(project_root).python)
    else:
        bundle, python = display_snapshot.kept_bundle_key(project_root), None
    slot = _slot(role, project_root)
    with slot.lock:
        if _closed:
            return dict(_CLOSING)
        generation = slot.generation
        if (
            slot.session is None
            or slot.session.dead
            or slot.bundle != bundle
            or python not in (None, slot.python)
        ):
            if not _replaced(slot, generation, bundle, role, project_root):
                return dict(_STOPPED)
        session = slot.session
        warm = session.alive
        result = session.engine_op(timeout=timeout, **spec)
        if _closed:
            return dict(_CLOSING)
        if result.get("kind") == "SessionDead" and warm and slot.generation == generation:
            if not _replaced(slot, generation, bundle, role, project_root):
                return dict(_STOPPED)
            result = slot.session.engine_op(timeout=timeout, **spec)
    if result.get("kind") in _LOST:
        return {**result, "kind": "ParserUnavailable"}
    return result


def run(project_root: Path) -> Optional[BenchSessionClient]:
    """Take the project's RUN slot for a holder: a fresh instance, started by its
    first frame; None while the slot's last instance is live (another holder's)."""
    session, taken = _take_run(Path(project_root))
    return session if taken else None


def run_for_recovery(project_root: Path) -> tuple[BenchSessionClient, bool]:
    """The RUN instance a recovery safe-off drives, and whether it took it (a fresh
    one it must end) rather than found a holder's live one (left to that holder)."""
    return _take_run(Path(project_root))


def _take_run(project_root: Path) -> tuple[BenchSessionClient, bool]:
    slot = _slot("run", project_root)
    with slot.lock:
        if slot.session is not None and not slot.session.ended:
            return slot.session, False
        slot.session = BenchSessionClient(project_root)
        return slot.session, True


def safe_off_remote(
    targets: list[dict[str, Any]],
    bench_map: dict[str, dict[str, Any]],
    project_root: Path,
    procedure_json: Optional[dict] = None,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """RECOVERY safe-off once a holder lost its RUN instance (it killed it first): a
    FRESH instance in the project's RUN slot drives ``targets`` (``[{"device","etype",
    "channels"}]``) OFF, ELOAD before PSU, then shuts down (close + unlock), so recovery
    and RUN are never two live processes; a live holder's instance still in the slot
    does the off and stays its holder's. Never raises: ``{"ok": True, "log",
    "unsafe"?}`` or ``{"ok": False, "kind", "error"}``."""
    sess, taken = run_for_recovery(project_root)
    if not taken:
        return sess.safe_off(targets, bench_map, timeout, procedure_json=procedure_json)
    try:
        res = sess.safe_off(targets, bench_map, timeout, procedure_json=procedure_json)
        sess.shutdown()  # close + unlock the just-opened devices
        return res
    finally:
        sess.kill()


def _slot(role: str, project_root: Path) -> _Slot:
    name = (role, os.path.normcase(os.path.abspath(project_root)))
    with _slots_lock:
        return _slots.setdefault(name, _Slot())


def _replace(slot: _Slot, bundle: tuple, role: str, project_root: Path) -> None:
    """A fresh instance in ``slot`` for the project's venv (ParserUnavailable when it has
    none) and ``bundle``; the one it replaces exits."""
    venv = engine_session._resolve_project_python(project_root)
    if slot.session is not None:
        _retire(slot.session)
    slot.python, slot.bundle = str(venv.python), bundle
    slot.session = engine_session.RemoteSession(project_root, role=role)


def _replaced(slot: _Slot, generation: int, bundle: tuple, role: str, project_root: Path) -> bool:
    """:func:`_replace`, unless a :func:`stop` landed meanwhile (it takes no lock):
    then the instance just made is retired again and the op answers stopped."""
    _replace(slot, bundle, role, project_root)
    if slot.generation == generation:
        return True
    session, slot.session, slot.python = slot.session, None, None
    if session is not None:
        _retire(session)
    return False


def _retire(session: engine_session.RemoteSession) -> None:
    """End an instance; one that already died is not started again just to be told
    to stop (``shutdown`` would lazily restart it)."""
    if session.alive:
        session.shutdown()
    else:
        session.kill()


def stop(
    *, venv_folder: Optional[Path] = None, project_root: Optional[Path] = None, wait: bool = False
) -> None:
    """End this app's instances so something can go: the ones whose interpreter lives
    in ``venv_folder`` (it is about to be deleted), or the ones of ``project_root``
    (it closed, or its bundle changed). The next op starts afresh wherever its
    venv then is. Instances of other projects, and every RUN, are left running.
    Never waits on a slot's lock (the GUI thread calls it): an instance is closed
    as :func:`stop_all` does, and an op in flight under it ends ``SessionDead``,
    not retried. A project's instances close on a thread, so a wedged one never
    holds the caller up; a venv's close before this returns, as a delete follows,
    and so does a project's with ``wait`` (a bundle install may evict its venv next)."""
    ended: list = []
    folder = (
        None if venv_folder is None else os.path.normcase(os.path.abspath(venv_folder)) + os.sep
    )
    project = None if project_root is None else os.path.normcase(os.path.abspath(project_root))
    for (role, name), slot in list(_slots.items()):
        python = slot.python
        if role == "run":
            continue
        if name == project or (
            folder and python and os.path.normcase(os.path.abspath(python)).startswith(folder)
        ):
            slot.generation += 1
            session, slot.session, slot.python = slot.session, None, None
            if session is not None:
                ended.append(session)
    if venv_folder is not None or wait:
        _close(ended)
    elif ended:
        threading.Thread(target=_close, args=(ended,), daemon=True).start()


def _close(sessions: list) -> None:
    for session in sessions:
        session.close()


def stop_all() -> None:
    """End every TOOLS instance for good: none starts again, so a worker caught
    mid-frame gets "app is closing" instead of a respawn racing the root's removal.
    Closed without the slot's lock, which that worker holds. ``temp_root`` runs it
    before removing its root: Windows cannot delete a running process's working
    folder. A RUN is its holder's to end, before this (closeEvent ends the bench
    session) or after (atexit); a recovery may still take one; stdin EOF is the backstop."""
    global _closed
    _closed = True
    for (role, _name), slot in list(_slots.items()):
        if role == "run":
            continue
        session, slot.session, slot.python = slot.session, None, None
        if session is not None:
            session.close()


# On every exit path (aboutToQuit and atexit alike), ahead of the removal itself.
temp_root.before_remove(stop_all)
