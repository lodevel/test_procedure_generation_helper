"""Per-project persistence for the validator-in-the-loop settings.

The chat panel surfaces a single operator toggle (auto-correct on
validator failure) whose state must survive editor restarts on a
per-project basis. Storing this in ``<project>/config/config.json``
under a ``validator_loop`` section keeps it next to the other
per-project preferences without bloating the chat-panel widget with
JSON I/O.

This module is the single source of truth for the section's name and
the read/write helpers. Both:

  - ``chat_panel.ChatPanel`` — calls :func:`load_settings` from
    ``switch_context`` and :func:`save_setting` from the toggle's
    ``toggled`` signal.
  - Project apply/seed flow (via ``procedo.bench.config_manager``)
    treats the section as part of "everything else" and preserves it
    across config-apply cycles per the post-apply-preserves contract.

A read error reads as no settings (the defaults); a save that cannot read or write the
project's ``config.json`` raises, and its caller tells the user — it never replaces a
config it could not read.

**Concurrency contract.** Single-writer per project. Writes use an
atomic temp-file + rename so a crash mid-write never leaves
``config.json`` truncated. The read-modify-write sequence is **not**
locked across processes — if two editor windows have the same project
open and toggle simultaneously, the last writer wins (which is fine for
a 1-operator setting; both windows will reflect the persisted value on
their next ``switch_context``).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from procedo.core import config_manager

log = logging.getLogger(__name__)

#: Each project's section as last read (:func:`load_settings`), by its normalised root.
_kept: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()  # read by a project open's worker too


SECTION_NAME = "validator_loop"
"""Top-level key under which validator-loop settings live in
``<project>/config/config.json``. Must match the section name preserved
by ``config_manager.seed_project_from_config`` (any key not in the four
overwritten sections is passed through untouched)."""


def is_enabled(project_root: Path | None) -> bool:
    """True iff the deterministic validator is enabled for this project.

    Reads ``validator_loop.enabled`` from the project config; defaults to
    True when the key is absent (back-compat with projects that predate
    the toggle). Callers use this to short-circuit ``is_loop_available``
    so operators who explicitly opted out of the validator stop seeing
    "validator unavailable" warnings (Phase 4.6).
    """
    if project_root is None:
        return True
    section = load_settings(project_root)
    val = section.get("enabled", True)
    return bool(val)


def max_attempts(project_root: Path | None) -> int:
    """The project's validator retry budget (``validator_loop.max_attempts``), else the
    built-in default."""
    from ..core.task_config import DEFAULT_MAX_VALIDATOR_ATTEMPTS

    if project_root is not None:
        value = load_settings(project_root).get("max_attempts")
        try:
            if value is not None:
                return max(1, int(value))
        except (TypeError, ValueError):
            log.debug("validator_loop.max_attempts is not an int: %r", value)
    return DEFAULT_MAX_VALIDATOR_ATTEMPTS


def load_settings(project_root: Path) -> dict[str, Any]:
    """Read the ``validator_loop`` section from the project's config.

    Returns an empty dict when the project has no config yet, the file
    is unreadable, or the section is absent — callers fall back to
    in-process defaults in any of those cases.

    Read once, then kept until :func:`forget` (the window's config watcher, a save here, a
    project open): every view asks it, and on a share each read is round trips.
    """
    key = os.path.normcase(os.path.abspath(project_root))
    with _lock:
        kept = _kept.get(key)
    if kept is None:
        section = config_manager.read_config_section(project_root / "config", SECTION_NAME)
        kept = section if isinstance(section, dict) else {}
        with _lock:
            _kept[key] = kept
    return dict(kept)


def forget(project_root: Path | None = None) -> None:
    """The project's ``config.json`` may have changed (None: any project's): read it again."""
    with _lock:
        if project_root is None:
            _kept.clear()
        else:
            _kept.pop(os.path.normcase(os.path.abspath(project_root)), None)


def save_setting(project_root: Path, key: str, value: Any) -> None:
    """Merge a single ``key=value`` into the ``validator_loop`` section, every other section
    kept (an atomic write). Raises, writing nothing, when the project's ``config.json``
    cannot be read or written: the caller tells the user."""
    config_dir = project_root / "config"
    data = config_manager.load_unified_config(config_dir)
    section = data.get(SECTION_NAME)
    data[SECTION_NAME] = {**section, key: value} if isinstance(section, dict) else {key: value}
    try:
        config_manager.write_unified_config(config_dir, data)
    finally:
        forget(project_root)
