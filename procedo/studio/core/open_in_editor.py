"""Open a file in the user's own editor (a code test's "Open in my editor", a shared phase's
"Open file") — never with an app that would RUN it.

A ``.py`` file's default action may run it (the Python launcher, a Store alias, a ``.bat``
wrapper, a shim): the test would run, instruments and all. So it is always opened with Windows'
Edit action, and refused when Windows has none.
"""

from __future__ import annotations

import os
from pathlib import Path


def open_in_editor(path: Path) -> str:
    """Open *path* with Windows' Edit action; "" when it opened, else why not (for the user)."""
    try:
        os.startfile(str(path), "edit")
    except OSError as exc:
        why = exc.strerror or str(exc)
        return (
            f"{path.name} could not be opened: {why}. "
            "Choose an editor as the default app for .py files."
        )
    return ""
