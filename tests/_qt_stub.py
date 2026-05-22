"""Shared Qt-stub helper for pure-logic tests that must run without PySide6.

Usage in a test file (before importing any ``workflow_editor`` submodule)::

    from tests._qt_stub import ensure_workflow_editor_importable
    ensure_workflow_editor_importable()

Calling it multiple times is safe — stubs are registered idempotently.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# Root of the package tree (…/test_procedure_generation_helper)
_REPO_ROOT = Path(__file__).resolve().parent.parent


def ensure_workflow_editor_importable() -> None:
    """Register minimal namespace stubs for ``workflow_editor`` and sub-packages.

    Only adds an entry to ``sys.modules`` when the package is not already
    present, so real imports are never shadowed if PySide6 happens to be
    installed.
    """
    _stub_if_absent("workflow_editor", _REPO_ROOT / "workflow_editor")
    _stub_if_absent("workflow_editor.llm", _REPO_ROOT / "workflow_editor" / "llm")


def _stub_if_absent(pkg: str, path: Path) -> None:
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = [str(path)]  # type: ignore[assignment]
        m.__package__ = pkg
        sys.modules[pkg] = m
