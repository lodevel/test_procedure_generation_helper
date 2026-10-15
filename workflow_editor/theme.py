"""Studio's own colours: what Bench's theme (``test_procedure_gui.theme``) does not have.

Studio wears Bench's theme: its palette and its one app stylesheet, applied by
``test_procedure_gui.theme.apply_app_theme``. The helpers below only add the colours of
Studio's own widgets (the skill chat, diff, syntax...) and read Bench's theme
state; the colours both apps share are Bench's, imported here.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from test_procedure_gui.theme import (  # Bench's theme state and shared colours
    ERROR_COLOR as ERROR_COLOR,
    WARNING_COLOR as WARNING_COLOR,
    border_color as border_color,
    error_color,
    is_dark,
    muted_color,
    success_color,
)


# ── Chat panel colours ──────────────────────────────────────────────

def chat_user_border() -> str:
    """Border for user messages in chat."""
    return "#2e7d32" if is_dark() else "#c8e6c9"


def chat_assistant_border() -> str:
    """Border for assistant messages in chat."""
    return "#1565c0" if is_dark() else "#bbdefb"


def chat_system_border() -> str:
    """Border for system/tool messages in chat."""
    return "#e65100" if is_dark() else "#ffe0b2"


def chat_content_color() -> str:
    """Color for chat message content."""
    return "#ddd" if is_dark() else "#333"


def message_bg(role: str) -> str:
    """Background for chat message bubbles by role."""
    if is_dark():
        return {"user": "#1a3050", "assistant": "#2a2a2a", "system": "#3a2a10"}.get(role, "#2a2a2a")
    return {"user": "#e3f2fd", "assistant": "#f5f5f5", "system": "#fff3e0"}.get(role, "#f5f5f5")


def message_border(role: str) -> str:
    """Border for chat message bubbles by role."""
    if is_dark():
        return {"user": "#2a5080", "assistant": "#444", "system": "#5a4a20"}.get(role, "#444")
    return {"user": "#bbdefb", "assistant": "#e0e0e0", "system": "#ffe0b2"}.get(role, "#e0e0e0")


# ── Diff viewer colours ─────────────────────────────────────────────

def diff_added_bg() -> QColor:
    """Background for added lines in diff viewer."""
    return QColor("#1e3a1e") if is_dark() else QColor("#d4edda")


def diff_added_fg() -> QColor:
    """Foreground for added lines in diff viewer."""
    return QColor("#66bb6a") if is_dark() else QColor("#155724")


def diff_removed_bg() -> QColor:
    """Background for removed lines in diff viewer."""
    return QColor("#3a1e1e") if is_dark() else QColor("#f8d7da")


def diff_removed_fg() -> QColor:
    """Foreground for removed lines in diff viewer."""
    return QColor("#ef5350") if is_dark() else QColor("#721c24")


def diff_header_color() -> QColor:
    """Colour for diff section headers."""
    return QColor("#64b5f6") if is_dark() else QColor("#0066cc")


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


# ── Danger button ───────────────────────────────────────────────────

def danger_button_style() -> str:
    """Stylesheet fragment for danger/destructive action buttons."""
    if is_dark():
        return "background-color: #b71c1c; color: #fff;"
    return "background-color: #f44336; color: white;"


# ── Rule selector ───────────────────────────────────────────────────

def disabled_text() -> str:
    """CSS colour for disabled/placeholder text."""
    return muted_color()


# ── Project bar status ──────────────────────────────────────────────

def status_connected() -> str:
    """CSS colour for 'connected' status."""
    return success_color()


def status_warning() -> str:
    """CSS colour for 'warning' status."""
    return "#ffa726" if is_dark() else "orange"


def status_error() -> str:
    """CSS colour for 'error' status."""
    return error_color()


# ── procedure_text.md syntax (the boards' text editor) ─────────────

def text_heading() -> QColor:
    """Colour for ``#`` heading lines."""
    return QColor("#8ab4f8") if is_dark() else QColor("#1d4fa3")


def text_step_number() -> QColor:
    """Colour for a step's leading ``N.``."""
    return QColor(muted_color())


def text_operator() -> QColor:
    """Colour for a step's ``Operator:``."""
    return QColor("#f1c27d") if is_dark() else QColor("#7a3f00")


def text_meta_key() -> QColor:
    """Colour for a ``## Meta`` key."""
    return QColor("#c4a7ff") if is_dark() else QColor("#7c3aed")


def text_device() -> QColor:
    """Colour for an ``## Equipment`` device id."""
    return QColor("#5eead4") if is_dark() else QColor("#0f766e")
