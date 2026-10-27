"""Open a file in the user's own editor (a code test's "Open in my editor", a shared phase's
"Open file"): the PC's default app for it — never one that would RUN it.

On Windows a ``.py`` file's default app is often a Python interpreter (the launcher, a Store
alias): opening the test with it would run it, instruments and all. Then — and whenever Windows
cannot say which app it is — Windows' Edit action opens it, and without one it is refused.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: The default apps that run a Python file rather than show it: python.exe, pythonw.exe,
#: python3.12.exe (a Store alias), py.exe, pyw.exe — any case.
_INTERPRETER = re.compile(r"^(pythonw?[\d.]*|pyw?)\.exe$", re.IGNORECASE)


def _default_app(path: Path) -> str:
    """The program Windows opens *path*'s kind of file with ("" when it cannot say)."""
    if sys.platform != "win32":
        return ""
    import ctypes

    size = ctypes.c_ulong(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    found = ctypes.windll.shlwapi.AssocQueryStringW(  # ASSOCF_NONE, ASSOCSTR_EXECUTABLE
        0, 2, path.suffix, "open", buffer, ctypes.byref(size)
    )
    return buffer.value if found == 0 else ""


def runs_python(app: str) -> bool:
    """*app* (a program's path) is a Python interpreter, or unknown ("")."""
    return not app or bool(_INTERPRETER.match(Path(app).name))


def open_in_editor(path: Path) -> str:
    """Open *path* with the PC's default app for it, or with its Edit action when that app may
    run it (:func:`runs_python`); "" when it opened, else why not (for the user)."""
    edit = runs_python(_default_app(path))
    try:
        if edit:
            os.startfile(str(path), "edit")
        else:
            os.startfile(str(path))
    except OSError as exc:
        why = exc.strerror or str(exc)
        hint = " Choose an editor as the default app for .py files." if edit else ""
        return f"{path.name} could not be opened: {why}.{hint}"
    return ""
