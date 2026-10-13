"""The editor's logic packages load without Qt, so a Python server can run them.

Every module of ``core``, ``llm``, ``authoring`` and ``pack_ops`` is imported in
a fresh interpreter where PySide6 cannot load. Only the Qt adapters listed below
may fail; anything else that drags Qt in is a regression.
"""

import subprocess
import sys
from pathlib import Path

# The Qt adapters that live next to the logic they drive.
_QT_ADAPTERS = {
    "workflow_editor.llm.worker",  # QThread around one LLM request
    "workflow_editor.authoring.skill_menu",  # the Skills menu (QAction / dialogs)
}

_PROBE = """
import importlib, pkgutil, sys
sys.modules["PySide6"] = None
failed = []
for pkg in ("core", "llm", "authoring", "pack_ops"):
    mod = importlib.import_module("workflow_editor." + pkg)
    for info in pkgutil.iter_modules(mod.__path__):
        name = f"workflow_editor.{pkg}.{info.name}"
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
print("\\n".join(failed))
"""


def test_logic_packages_import_without_qt():
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=root, capture_output=True,
                         text=True, check=True).stdout
    failed = {line.split(":", 1)[0]: line for line in out.splitlines() if line}
    assert set(failed) == _QT_ADAPTERS, "\n".join(failed[n] for n in set(failed) - _QT_ADAPTERS)
