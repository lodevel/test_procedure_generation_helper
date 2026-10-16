"""Sync state of a test folder that is not open in the editor.

The Tests list shows which of a test's text, JSON and code changed since the
user last acknowledged them as coherent, and "Mark In Sync" re-baselines an
unopened test. Both use the open test's own pieces — the session file's sync baseline
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


def stale_artifacts(test_dir: Path, patterns: list[re.Pattern[str]]) -> tuple[str, ...]:
    """The tracked files (:attr:`ArtifactManager._SYNC_TRACKED_NAMES`) not acknowledged as in
    sync with the others: those on disk that no longer match their acknowledged hash (the
    baseline only moves on acknowledgment, so this names an edit made in or out of the editor).
    When the session says out of sync but no file names itself (the edit was reverted, the
    baseline is partial, a file is unreadable), the flag alone cannot say which: every tracked
    name. No session or no baseline yet (first open, legacy session) reads as in sync: ()."""
    baseline = read_sync_baseline(test_dir)
    if baseline is None:
        return ()
    in_sync, stored_hashes = baseline
    stale = []
    for name in ArtifactManager._SYNC_TRACKED_NAMES:
        stored = stored_hashes.get(name)
        if not stored:
            continue
        try:
            current = _disk_hash(test_dir, name, patterns)
        except (OSError, ValueError):
            continue
        if current is not None and current != stored:
            stale.append(name)
    if not stale and not in_sync:
        return ArtifactManager._SYNC_TRACKED_NAMES
    return tuple(stale)


def is_out_of_sync(test_dir: Path, patterns: list[re.Pattern[str]]) -> bool:
    """True when any tracked file is stale (:func:`stale_artifacts`)."""
    return bool(stale_artifacts(test_dir, patterns))


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
