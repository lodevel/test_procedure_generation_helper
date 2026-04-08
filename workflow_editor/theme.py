"""Theme-aware colour utilities for the workflow editor.

All UI code should use these helpers instead of hard-coding hex colours
so that widgets remain legible on both dark and light system palettes.
"""

from __future__ import annotations

from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication


# ── Palette introspection ────────────────────────────────────────────

def _palette() -> QPalette:
    app = QApplication.instance()
    return app.palette() if app else QPalette()


def is_dark() -> bool:
    """Return True when the current application palette is dark."""
    return _palette().color(QPalette.ColorRole.Window).lightness() < 128


# ── Semantic status colours (good contrast on both backgrounds) ──────

SUCCESS_COLOR = "#4caf50"
ERROR_COLOR = "#f44336"
WARNING_COLOR = "#ff9800"
INFO_COLOR = "#42a5f5"


# ── Muted / secondary text ──────────────────────────────────────────

def muted_text() -> str:
    """Secondary / hint text colour."""
    return "#aaa" if is_dark() else "#666"


def border_color() -> str:
    """Visible border colour for the current theme."""
    return "#555" if is_dark() else "#ccc"


# ── Chat panel colours ──────────────────────────────────────────────

def chat_user_bg() -> str:
    """Background for user messages in chat."""
    return "#1b3a1b" if is_dark() else "#e8f5e9"


def chat_user_border() -> str:
    """Border for user messages in chat."""
    return "#2e7d32" if is_dark() else "#c8e6c9"


def chat_assistant_bg() -> str:
    """Background for assistant messages in chat."""
    return "#1a2433" if is_dark() else "#e3f2fd"


def chat_assistant_border() -> str:
    """Border for assistant messages in chat."""
    return "#1565c0" if is_dark() else "#bbdefb"


def chat_system_bg() -> str:
    """Background for system/tool messages in chat."""
    return "#2b2518" if is_dark() else "#fff3e0"


def chat_system_border() -> str:
    """Border for system/tool messages in chat."""
    return "#e65100" if is_dark() else "#ffe0b2"


def chat_input_bg() -> str:
    """Background for the chat input area."""
    return "#2b2b2b" if is_dark() else "#f5f5f5"


def chat_input_border() -> str:
    """Border for the chat input area."""
    return "#444" if is_dark() else "#e0e0e0"


def chat_role_color() -> str:
    """Color for the role label in chat messages."""
    return "#bbb" if is_dark() else "#555"


def chat_timestamp_color() -> str:
    """Color for timestamps in chat."""
    return "#888" if is_dark() else "#777"


def chat_content_color() -> str:
    """Color for chat message content."""
    return "#ddd" if is_dark() else "#333"


def chat_thinking_border() -> str:
    """Border for thinking/processing indicators."""
    return "#5588bb" if is_dark() else "#90caf9"


def chat_accept_bg() -> str:
    """Background for accept/apply buttons."""
    return "#2e7d32" if is_dark() else "#c8e6c9"


def chat_reject_bg() -> str:
    """Background for reject/cancel buttons."""
    return "#b71c1c" if is_dark() else "#ffcdd2"


# Aliases matching reference API names
accept_btn_bg = chat_accept_bg
reject_btn_bg = chat_reject_bg


def proposal_bg() -> str:
    """Background for proposal widgets."""
    return "#1a3020" if is_dark() else "#e8f5e9"


def proposal_border() -> str:
    """Border for proposal widgets."""
    return "#2a5040" if is_dark() else "#c8e6c9"


def proposal_handled_bg() -> str:
    """Background for accepted/rejected proposal."""
    return "#2a2a2a" if is_dark() else "#f5f5f5"


def proposal_handled_border() -> str:
    """Border for accepted/rejected proposal."""
    return "#444" if is_dark() else "#e0e0e0"


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


def toggle_color() -> str:
    """Colour for collapsible-section toggle text."""
    return "#999" if is_dark() else "#888"


def toggle_hover_color() -> str:
    """Hover colour for toggle text."""
    return "#ccc" if is_dark() else "#555"


def thinking_fg() -> str:
    """Foreground for thinking/reasoning text."""
    return "#aaa" if is_dark() else "#777"


def thinking_bg() -> str:
    """Background for thinking/reasoning blocks."""
    return "rgba(255,255,255,0.05)" if is_dark() else "rgba(0,0,0,0.03)"


def thinking_border() -> str:
    """Left-border colour for thinking blocks."""
    return "#555" if is_dark() else "#ccc"


def response_fg() -> str:
    """Foreground for streaming response text."""
    return "#ddd" if is_dark() else "#333"


def response_bg() -> str:
    """Background for streaming response blocks."""
    return "rgba(255,255,255,0.03)" if is_dark() else "rgba(0,0,0,0.02)"


def response_border() -> str:
    """Left-border accent for streaming response blocks."""
    return "#5090c0" if is_dark() else "#90caf9"


def muted_color() -> str:
    """Hex colour for secondary / hint / placeholder text (palette-based)."""
    return _palette().color(QPalette.ColorRole.PlaceholderText).name()


# ── Group box styling ───────────────────────────────────────────────

def groupbox_bg(style: str = "default") -> str:
    """Background colour for styled QGroupBox containers."""
    if style == "file":
        return "#1a2e3a" if is_dark() else "#e8f4f8"
    elif style == "llm":
        return "#2a1a3a" if is_dark() else "#f0e8f8"
    else:
        return "#2b2b2b" if is_dark() else "#f5f5f5"


def groupbox_border(style: str = "default") -> str:
    """Border colour for styled QGroupBox containers."""
    if style == "file":
        return "#2a5a7a" if is_dark() else "#b3d9e8"
    elif style == "llm":
        return "#5a3a7a" if is_dark() else "#d8c8e8"
    else:
        return "#444" if is_dark() else "#cccccc"


def groupbox_text() -> str:
    """Text colour for QGroupBox title and content."""
    return "#ddd" if is_dark() else "#333333"


# ── Workspace / test list colours ───────────────────────────────────

def sync_warning_color() -> QColor:
    """QColor for out-of-sync test items."""
    return QColor(255, 165, 0)  # Orange — good contrast on both


def empty_test_color() -> QColor:
    """QColor for empty test folders."""
    return QColor(150, 150, 150)  # Gray


def ready_test_color() -> QColor:
    """QColor for tests with JSON + code."""
    return QColor(80, 180, 80) if is_dark() else QColor(0, 150, 0)


def default_text_color() -> QColor:
    """QColor for normal text items."""
    p = _palette()
    return QColor(p.color(QPalette.ColorRole.WindowText))


def selected_test_bg() -> QColor:
    """QColor background for the currently opened test."""
    return QColor(50, 90, 140) if is_dark() else QColor(70, 130, 180)


def selected_test_fg() -> QColor:
    """QColor foreground for the currently opened test."""
    return QColor(255, 255, 255)


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


def syntax_step_marker() -> QColor:
    """Colour for step marker comments."""
    return QColor("#dcdcaa") if is_dark() else QColor("#cc6600")


def syntax_step_bg() -> QColor:
    """Background highlight for step marker lines."""
    return QColor("#2a2a1a") if is_dark() else QColor("#fff8e0")


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


# ── Findings panel colours ──────────────────────────────────────────

def finding_error() -> QColor:
    """Colour for error findings."""
    return QColor("#ef5350") if is_dark() else QColor("#c62828")


def finding_warning() -> QColor:
    """Colour for warning findings."""
    return QColor("#ffa726") if is_dark() else QColor("#ef6c00")


def finding_info() -> QColor:
    """Colour for info findings."""
    return QColor("#42a5f5") if is_dark() else QColor("#1565c0")


def finding_success() -> str:
    """CSS colour string for success status in findings."""
    return "#66bb6a" if is_dark() else "green"


# ── Traceability highlight ──────────────────────────────────────────

def traceability_highlight() -> QColor:
    """Background highlight in traceability view."""
    return QColor("#3a3a1a") if is_dark() else QColor("#fffacd")


# ── Danger button ───────────────────────────────────────────────────

def danger_button_style() -> str:
    """Stylesheet fragment for danger/destructive action buttons."""
    if is_dark():
        return "background-color: #b71c1c; color: #fff;"
    return "background-color: #f44336; color: white;"


# ── Rule selector ───────────────────────────────────────────────────

def disabled_text() -> str:
    """CSS colour for disabled/placeholder text."""
    return "#777" if is_dark() else "gray"


# ── Project bar status ──────────────────────────────────────────────

def status_connected() -> str:
    """CSS colour for 'connected' status."""
    return "#66bb6a" if is_dark() else "green"


def status_warning() -> str:
    """CSS colour for 'warning' status."""
    return "#ffa726" if is_dark() else "orange"


def status_error() -> str:
    """CSS colour for 'error' status."""
    return "#ef5350" if is_dark() else "red"


# ── Status label colours (modified / saved indicators) ──────────────

def status_modified() -> str:
    """CSS colour for modified/unsaved content indicator."""
    return "#ffa726" if is_dark() else "orange"


def status_saved() -> str:
    """CSS colour for saved/clean content indicator."""
    return "#66bb6a" if is_dark() else "green"
