"""The editor's one way to write a file the user (or the app) depends on, to act
on a file another process may be holding for a moment, and to fingerprint what is
sent to the LLM."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from procedo.core import atomic_write

T = TypeVar("T")

# On Windows a rename fails while another process holds the file (or a file
# inside the folder) open — the host's display sweep or a bench run reading
# procedure.json for a few milliseconds. Retry for about a second.
_BUSY_ATTEMPTS = 20
_BUSY_PAUSE_S = 0.05


def content_digest(text: str, files: Sequence[Path]) -> str:
    """The checksum of what is sent: *text* and each attached file's bytes (a file
    whose contents changed changes it)."""
    digest = hashlib.md5(text.encode())
    for path in files:
        digest.update(f"\0{path}\0".encode())
        try:
            digest.update(Path(path).read_bytes())
        except OSError:
            digest.update(b"\0missing")
    return digest.hexdigest()


def retry_while_busy(action: Callable[[], T]) -> T:
    """Run ``action``, retrying for about a second while Windows reports the
    target busy (PermissionError); the last error is raised."""
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            return action()
        except PermissionError:
            if attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(_BUSY_PAUSE_S)
    raise AssertionError("unreachable")


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` (UTF-8) to ``path`` so a crash leaves either the old file
    or the new one, never a truncated mix: the host's ``atomic_write`` (a pid-named
    temp in the same folder, then ``os.replace``, retried while another process
    holds the target). Creates the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write.write_text(path, text)


def atomic_create(path: Path, data: bytes) -> None:
    """Write ``data`` to the NEW file ``path``, all or nothing, never replacing a
    file already there: FileExistsError when it exists. Creates the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write.create_bytes(path, data)
