"""Sync state of a test folder that is not open in the editor.

The Tests list shows ⚠ when a test's text, JSON or code changed since the user
last acknowledged them as coherent, and "Mark In Sync" re-baselines an unopened
test. Both use the open test's own pieces — the session file's sync baseline
(``session_state``) and the sync hash (:func:`enforced_sync_hash`) over the
tracked files (:attr:`ArtifactManager._SYNC_TRACKED_NAMES`) — so the list and
an opened test always agree.
"""

from __future__ import annotations

import re
from pathlib import Path

from .artifact_manager import ArtifactManager, enforced_sync_hash
from .session_state import read_sync_baseline, write_sync_baseline


def _disk_hash(test_dir: Path, name: str, patterns: list[re.Pattern[str]]) -> str | None:
    """The sync hash of one tracked file on disk; None when it is absent."""
    path = test_dir / name
    if not path.exists():
        return None
    return enforced_sync_hash(test_dir, name, path.read_text(encoding="utf-8"), patterns)


def is_out_of_sync(test_dir: Path, patterns: list[re.Pattern[str]]) -> bool:
    """True when the session says out of sync, or a tracked file on disk no longer
    matches its acknowledged hash (an edit made while the editor was closed).
    No session or no baseline yet (first open, legacy session) reads as in sync;
    an unreadable file is skipped."""
    baseline = read_sync_baseline(test_dir)
    if baseline is None:
        return False
    in_sync, stored_hashes = baseline
    if not in_sync:
        return True
    for name in ArtifactManager._SYNC_TRACKED_NAMES:
        stored = stored_hashes.get(name)
        if not stored:
            continue
        try:
            current = _disk_hash(test_dir, name, patterns)
        except (OSError, ValueError):
            continue
        if current is not None and current != stored:
            return True
    return False


def mark_in_sync(test_dir: Path, patterns: list[re.Pattern[str]]) -> None:
    """Acknowledge an unopened test's files as coherent: their current hashes
    become the baseline; the rest of the session file is kept. Raises OSError or
    ValueError when a file cannot be read or the session cannot be written."""
    hashes = {}
    for name in ArtifactManager._SYNC_TRACKED_NAMES:
        current = _disk_hash(test_dir, name, patterns)
        if current is not None:
            hashes[name] = current
    write_sync_baseline(test_dir, hashes)
