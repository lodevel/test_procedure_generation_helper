"""Open a file in the user's own editor (a code test's "Open in my editor", a shared phase's
"Open file") — never with an app that would RUN it.

A ``.py`` file's default action may run it (the Python launcher, a Store alias, a ``.bat``
wrapper, a shim): the test would run, instruments and all. So it is opened with Windows' Edit
action; a PC that has none gets Notepad, which only edits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _notepad() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"


def open_in_editor(path: Path) -> str:
    """Open *path* with Windows' Edit action, else Notepad; "" when it opened, else why not."""
    try:
        os.startfile(str(path), "edit")
        return ""
    except OSError:
        pass  # no Edit action on this PC: Notepad
    try:
        subprocess.Popen([str(_notepad()), str(path)])
    except OSError as exc:
        why = exc.strerror or str(exc)
        return (
            f"{path.name} could not be opened: {why}. "
            "Choose an editor as the default app for .py files."
        )
    return ""
