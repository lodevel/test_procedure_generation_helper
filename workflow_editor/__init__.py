"""
LLM Workflow Editor - Qt Application for structured test procedure authoring.

This application helps users create, edit, review, and keep consistent
procedure.json and test.py artifacts with LLM assistance.
"""

__version__ = "0.1.0"
__author__ = "Test Procedure Generation Helper"

def __getattr__(name):
    # Lazily import the Qt entry point so pure-Python subpackages (e.g.
    # ``workflow_editor.authoring``) are importable without PySide6.
    if name == "MainWindow":
        from .main_window import MainWindow
        return MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MainWindow", "__version__"]
