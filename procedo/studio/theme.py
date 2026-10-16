"""Studio's own colours: what Bench's theme (``test_procedure_gui.theme``) does not have.

Studio wears Bench's theme: its palette and its one app stylesheet, applied by
``test_procedure_gui.theme.apply_app_theme``. The helpers below only add the colours of
Studio's own widgets (syntax, JSON, procedure text) and read Bench's light / dark state; a
colour both apps share is Bench's, and its callers take it from Bench's theme.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from test_procedure_gui import fluent_qss
from test_procedure_gui.theme import (  # Bench's theme state
    is_dark,
    muted_color as muted_color,
)


def tone(token: str) -> QColor:
    """One of Bench's stylesheet colours by token (a pill's, the accent), light or dark as the
    app is: for what Studio paints outside the stylesheet (a diff line, a text format)."""
    return QColor(fluent_qss.tokens(is_dark())[token])


# ── Code syntax highlighting colours ────────────────────────────────

def syntax_keyword() -> QColor:
    """Colour for Python keywords."""
    return QColor("#569cd6") if is_dark() else QColor("#0000cc")


def syntax_string() -> QColor:
    """Colour for string literals."""
    return QColor("#6aab73") if is_dark() else QColor("#008800")


def syntax_comment() -> QColor:
    """Colour for comments."""
    return QColor("#6a9955") if is_dark() else QColor("#888888")


def syntax_function() -> QColor:
    """Colour for function/method names."""
    return QColor("#c586c0") if is_dark() else QColor("#660066")


# ── JSON syntax colours ─────────────────────────────────────────────

def json_key() -> QColor:
    """Colour for JSON keys."""
    return QColor("#9cdcfe") if is_dark() else QColor("#0066cc")


def json_string() -> QColor:
    """Colour for JSON string values."""
    return QColor("#ce9178") if is_dark() else QColor("#008800")


def json_number() -> QColor:
    """Colour for JSON numbers."""
    return QColor("#b5cea8") if is_dark() else QColor("#cc6600")


def json_keyword() -> QColor:
    """Colour for JSON true/false/null."""
    return QColor("#c586c0") if is_dark() else QColor("#cc00cc")


# ── Project bar status ──────────────────────────────────────────────

def status_warning() -> str:
    """CSS colour for 'warning' status."""
    return "#ffa726" if is_dark() else "orange"


# ── procedure_text.md syntax (the boards' text editor) ─────────────

def text_heading() -> QColor:
    """Colour for ``#`` heading lines."""
    return QColor("#8ab4f8") if is_dark() else QColor("#1d4fa3")


def text_meta_key() -> QColor:
    """Colour for a ``## Meta`` key."""
    return QColor("#c4a7ff") if is_dark() else QColor("#7c3aed")


def text_device() -> QColor:
    """Colour for an ``## Equipment`` device id."""
    return QColor("#5eead4") if is_dark() else QColor("#0f766e")
