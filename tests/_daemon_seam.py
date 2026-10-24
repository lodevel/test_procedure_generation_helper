"""Fake a helper of the in-process engine daemon in every module that binds it.

The daemon (``_execute_op_subprocess.py``, loaded by file path) and its bare-name siblings
``_safe_off`` / ``_instrument_ops`` each bind the shared helpers by name, so a fake must replace
the name in all of them.  The siblings are shared by every daemon copy in the process, so each
fake is undone at the test's cleanup.
"""
from __future__ import annotations

import sys

_SIBLINGS = ("_safe_off", "_instrument_ops")


def rebind(test, daemon, name: str, value) -> None:
    """Bind ``name`` to ``value`` wherever the daemon's current object is bound; undo at cleanup."""
    current = getattr(daemon, name)
    for module in (daemon, *(sys.modules[s] for s in _SIBLINGS)):
        if getattr(module, name, None) is current:
            setattr(module, name, value)
            test.addCleanup(setattr, module, name, current)
