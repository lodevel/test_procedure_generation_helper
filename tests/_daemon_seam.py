"""Fake a helper of the in-process engine daemon in every module that binds it.

The daemon (``_execute_op_subprocess.py``, loaded by file path) and its bare-name siblings
(``_safe_off``, ``_instrument_ops``, ...) each bind the shared helpers by name, so a fake must
replace the name in all of them.  The siblings are every loaded module whose file sits in the
daemon's folder, so a new sibling is covered without a list here.  They are shared by every
daemon copy in the process, so each fake is undone at the test's cleanup.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _daemon_modules(daemon) -> list:
    """The daemon (loaded by path, so absent from sys.modules) + every loaded sibling."""
    folder = Path(daemon.__file__).resolve().parent
    return [
        daemon,
        *(
            m
            for m in list(sys.modules.values())
            if m is not daemon
            and getattr(m, "__file__", None)
            and Path(m.__file__).resolve().parent == folder
        ),
    ]


def rebind(test, daemon, name: str, value) -> None:
    """Bind ``name`` to ``value`` wherever the daemon's current object is bound; undo at cleanup."""
    current = getattr(daemon, name)
    for module in _daemon_modules(daemon):
        if getattr(module, name, None) is current:
            setattr(module, name, value)
            test.addCleanup(setattr, module, name, current)
