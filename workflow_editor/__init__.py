"""
LLM Workflow Editor - Qt Application for structured test procedure authoring.

This application helps users create, edit, review, and keep consistent
procedure.json and test.py artifacts with LLM assistance.
"""

__version__ = "0.1.0"
__author__ = "Test Procedure Generation Helper"

from .main_window import MainWindow

__all__ = ["MainWindow", "__version__"]
