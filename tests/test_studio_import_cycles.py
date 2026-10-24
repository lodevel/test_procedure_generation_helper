"""Every Studio package imports on its own, in a fresh interpreter.

A circular import hides when another test has already loaded the modules in a lucky order; it fails
only when a package is the first thing imported. One subprocess per package, so a new cycle fails here
by name instead of as a flake in an unrelated test.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_STUDIO = Path(__file__).resolve().parents[1] / "procedo" / "studio"
_PACKAGES = ["procedo.studio"] + sorted(
    f"procedo.studio.{p.parent.name}" for p in _STUDIO.glob("*/__init__.py"))


@pytest.mark.parametrize("package", _PACKAGES)
def test_package_imports_alone(package):
    run = subprocess.run([sys.executable, "-c", f"import {package}"], cwd=_STUDIO.parents[1],
                          capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-1500:]
