"""The editor's one way to write a file the user (or the app) depends on, and to
act on a file another process may be holding for a moment."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

# On Windows a rename fails while another process holds the file (or a file
# inside the folder) open — the host's display sweep or a bench run reading
# procedure.json for a few milliseconds. Retry for about a second.
_BUSY_ATTEMPTS = 20
_BUSY_PAUSE_S = 0.05


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
    or the new one, never a truncated mix: a temp file in the same folder, then
    ``os.replace`` over the target (atomic on Windows too, unlike ``shutil.move``,
    which copies then deletes when the target exists). A target briefly held
    open by another process is retried. Creates the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        retry_while_busy(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # the original error is the one that matters
        raise
