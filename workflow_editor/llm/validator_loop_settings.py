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
  - Project apply/seed flow (via ``test_procedure_gui.config_manager``)
    treats the section as part of "everything else" and preserves it
    across config-apply cycles per the post-apply-preserves contract.

Errors are logged but never raised — a transient I/O glitch must not
block the operator from continuing to work; the toggle state simply
falls back to the in-memory checkbox value.

**Concurrency contract.** Single-writer per project. Writes use an
atomic temp-file + rename so a crash mid-write never leaves
``config.json`` truncated. The read-modify-write sequence is **not**
locked across processes — if two editor windows have the same project
open and toggle simultaneously, the last writer wins (which is fine for
a 1-operator setting; both windows will reflect the persisted value on
their next ``switch_context``).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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


def load_settings(project_root: Path) -> dict[str, Any]:
    """Read the ``validator_loop`` section from the project's config.

    Returns an empty dict when the project has no config yet, the file
    is unreadable, or the section is absent — callers fall back to
    in-process defaults in any of those cases.
    """
    config_path = project_root / "config" / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read validator_loop settings from %s", config_path)
        return {}
    section = data.get(SECTION_NAME, {})
    return section if isinstance(section, dict) else {}


def save_setting(project_root: Path, key: str, value: Any) -> None:
    """Merge a single ``key=value`` into the ``validator_loop`` section.

    Preserves all other sections of the config — the chat panel must
    never clobber ``test_order``, ``parsers``, etc. by writing only its
    own slice. Same preserve-and-overlay contract as
    ``config_manager.write_config_section``.

    Uses an atomic temp-file + rename so a crash mid-write leaves the
    previous ``config.json`` intact (vs. ``write_text`` which would
    truncate the file before writing the new contents).
    """
    config_dir = project_root / "config"
    config_path = config_dir / "config.json"
    config_dir.mkdir(parents=True, exist_ok=True)

    data = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning(
                "config.json at %s is unreadable; rewriting with %s only",
                config_path, SECTION_NAME,
            )
            data = {}

    section = data.get(SECTION_NAME, {})
    if not isinstance(section, dict):
        section = {}
    section[key] = value
    data[SECTION_NAME] = section

    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        _atomic_write_text(config_path, serialized)
    except OSError:
        log.exception("Could not write validator_loop settings to %s", config_path)


def _atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically (temp-file + rename).

    On Windows, ``os.replace`` overwrites the destination atomically;
    on POSIX, rename(2) is atomic within the same filesystem. The temp
    file is created in the destination's parent directory so the rename
    is guaranteed same-filesystem.
    """
    parent = target.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except Exception:
        # On any failure, don't leave the temp file lying around.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
