"""The editor's one way to write a file the user (or the app) depends on."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# On Windows a rename over a file fails while another process holds it open
# (the host's display sweep or a bench run reading procedure.json for a few
# milliseconds). Retry for about a second before giving up.
_REPLACE_ATTEMPTS = 20
_REPLACE_PAUSE_S = 0.05


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
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_PAUSE_S)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # the original error is the one that matters
        raise
