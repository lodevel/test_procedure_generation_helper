"""Theme-aware colour utilities for the workflow editor.

All UI code should use these helpers instead of hard-coding hex colours
so that widgets remain legible on both dark and light system palettes.
"""

from __future__ import annotations

from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication


# ── Application theme registry ───────────────────────────────────────

ORIGINAL_THEME_ID = "original"
NEUTRAL_DARK_THEME_ID = "dark"
MODERN_DARK_THEME_ID = "modern_dark"

_THEME_LABELS: dict[str, str] = {
    ORIGINAL_THEME_ID: "Original / system",
    NEUTRAL_DARK_THEME_ID: "Neutral dark",
    MODERN_DARK_THEME_ID: "Modern dark",
}

_ORIGINAL_PALETTE: QPalette | None = None
_THEME_ACTIVE = False


def available_app_themes() -> tuple[tuple[str, str], ...]:
    """Return selectable application themes as ``(id, label)`` pairs."""
    return tuple(_THEME_LABELS.items())


def normalize_app_theme_name(name: str | None) -> str:
    """Return a known theme id, falling back to the native/system look."""
    if name in _THEME_LABELS:
        return str(name)
    return ORIGINAL_THEME_ID


def app_theme_label(name: str | None) -> str:
    """Return the display label for a theme id."""
    return _THEME_LABELS[normalize_app_theme_name(name)]


def apply_app_theme(theme_name: str | None, app: QApplication | None = None) -> str:
    """Apply the selected app theme and return the normalized theme id."""
    global _THEME_ACTIVE

    app = app or QApplication.instance()
    theme_id = normalize_app_theme_name(theme_name)
    if app is None:
        return theme_id

    if theme_id == ORIGINAL_THEME_ID:
        if _ORIGINAL_PALETTE is not None:
            app.setPalette(_ORIGINAL_PALETTE)
        if _THEME_ACTIVE:
            app.setStyleSheet("")
        _THEME_ACTIVE = False
        return theme_id

    if theme_id in (NEUTRAL_DARK_THEME_ID, MODERN_DARK_THEME_ID):
        _remember_original_palette(app)
        app.setPalette(_neutral_dark_palette())
        stylesheet = (
            _modern_dark_stylesheet()
            if theme_id == MODERN_DARK_THEME_ID
            else _neutral_dark_stylesheet()
        )
        app.setStyleSheet(stylesheet)
        _THEME_ACTIVE = True
        return theme_id

    if _ORIGINAL_PALETTE is not None:
        app.setPalette(_ORIGINAL_PALETTE)
    _THEME_ACTIVE = False
    return ORIGINAL_THEME_ID


def _remember_original_palette(app: QApplication) -> None:
    """Capture the current native palette before applying an opt-in theme."""
    global _ORIGINAL_PALETTE
    if _ORIGINAL_PALETTE is None:
        _ORIGINAL_PALETTE = QPalette(app.palette())


def _neutral_dark_palette() -> QPalette:
    """Build a conservative dark palette that still feels native Qt."""
    palette = QPalette()

    window = QColor("#24272e")
    panel = QColor("#2d313a")
    base = QColor("#1b1e24")
    alt_base = QColor("#262a32")
    text = QColor("#e8eaed")
    muted = QColor("#b8c0cc")
    disabled = QColor("#8f98a6")
    accent = QColor("#4f9cf9")
    border = QColor("#56606d")

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QPalette.ColorRole.Light, QColor("#4a515e"))
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#3a404a"))
    palette.setColor(QPalette.ColorRole.Dark, QColor("#151820"))
    palette.setColor(QPalette.ColorRole.Mid, border)
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#0f1117"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, accent)
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#c084fc"))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)

    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, window)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#3a414c"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, disabled)
    return palette


def _neutral_dark_stylesheet() -> str:
    """High-contrast defaults for widgets that do not fully honor palettes."""
    return """
        QWidget {
            color: #e8eaed;
            selection-background-color: #4f9cf9;
            selection-color: #ffffff;
        }
        QMainWindow, QDialog, QWidget#centralwidget {
            background-color: #24272e;
        }
        QLabel, QCheckBox, QRadioButton, QGroupBox {
            color: #e8eaed;
        }
        QGroupBox {
            border: 1px solid #56606d;
            border-radius: 4px;
            margin-top: 0.7em;
            padding-top: 0.35em;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 3px;
        }
        QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser, QSpinBox,
        QDoubleSpinBox, QComboBox, QListWidget, QTreeWidget, QTableWidget,
        QListView, QTreeView, QTableView {
            background-color: #1b1e24;
            color: #f1f5f9;
            border: 1px solid #56606d;
            selection-background-color: #4f9cf9;
            selection-color: #ffffff;
        }
        QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
        QTextBrowser:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
        QComboBox:disabled, QListWidget:disabled, QTreeWidget:disabled,
        QTableWidget:disabled {
            background-color: #20242b;
            color: #8f98a6;
        }
        QHeaderView::section {
            background-color: #2d313a;
            color: #e8eaed;
            border: 1px solid #56606d;
            padding: 3px;
        }
        QTabWidget::pane {
            border: 1px solid #56606d;
        }
        QTabBar::tab {
            background-color: #2d313a;
            color: #d7dce3;
            border: 1px solid #56606d;
            padding: 5px 10px;
        }
        QTabBar::tab:selected {
            background-color: #3a404a;
            color: #ffffff;
        }
        QPushButton {
            background-color: #323741;
            color: #f1f5f9;
            border: 1px solid #667080;
            border-radius: 4px;
            padding: 4px 8px;
        }
        QPushButton:hover {
            background-color: #3d4450;
        }
        QPushButton:pressed {
            background-color: #252a32;
        }
        QPushButton:disabled {
            background-color: #292d34;
            color: #8f98a6;
            border-color: #3a414c;
        }
        QMenuBar, QMenu, QToolBar, QStatusBar {
            background-color: #24272e;
            color: #e8eaed;
        }
        QMenu::item:selected, QMenuBar::item:selected {
            background-color: #3a404a;
        }
        QToolTip {
            background-color: #2d313a;
            color: #f1f5f9;
            border: 1px solid #667080;
        }
    """


def _modern_dark_stylesheet() -> str:
    """Dark theme with more explicit card-like surfaces."""
    return _neutral_dark_stylesheet() + """
        QWidget#mainCentral {
            background-color: #171a21;
        }
        QSplitter::handle {
            background-color: #171a21;
        }
        QGroupBox {
            background-color: #20242c;
            border: 1px solid #4a5362;
            border-radius: 10px;
            margin-top: 1.1em;
            padding: 10px 8px 8px 8px;
            font-weight: 600;
        }
        QGroupBox::title {
            color: #f1f5f9;
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
        }
        QPushButton {
            border-radius: 8px;
            padding: 6px 10px;
        }
        QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser, QSpinBox,
        QDoubleSpinBox, QComboBox, QListWidget, QTreeWidget, QTableWidget,
        QListView, QTreeView, QTableView {
            border-radius: 7px;
        }
    """


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
    return "#b8c0cc" if is_dark() else "#666"


def text_color() -> str:
    """Default foreground text colour."""
    return _palette().color(QPalette.ColorRole.WindowText).name()


def success_color() -> str:
    """Readable success foreground colour."""
    return "#81c784" if is_dark() else "#2e7d32"


def error_color() -> str:
    """Readable error foreground colour."""
    return "#ff8a80" if is_dark() else "#c62828"


def warning_color() -> str:
    """Readable warning foreground colour."""
    return "#ffcc80" if is_dark() else "#bf360c"


def border_color() -> str:
    """Visible border colour for the current theme."""
    return "#667080" if is_dark() else "#ccc"


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
    return "#c5ccd6" if is_dark() else "#555"


def chat_timestamp_color() -> str:
    """Color for timestamps in chat."""
    return "#b8c0cc" if is_dark() else "#777"


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
    return "#b8c0cc" if is_dark() else "#888"


def toggle_hover_color() -> str:
    """Hover colour for toggle text."""
    return "#ccc" if is_dark() else "#555"


def thinking_fg() -> str:
    """Foreground for thinking/reasoning text."""
    return "#b8c0cc" if is_dark() else "#777"


def thinking_bg() -> str:
    """Background for thinking/reasoning blocks."""
    return "rgba(255,255,255,0.05)" if is_dark() else "rgba(0,0,0,0.03)"


def thinking_border() -> str:
    """Left-border colour for thinking blocks."""
    return "#667080" if is_dark() else "#ccc"


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
        return "#667080" if is_dark() else "#cccccc"


def groupbox_text() -> str:
    """Text colour for QGroupBox title and content."""
    return "#f1f5f9" if is_dark() else "#333333"


# ── Workspace / test list colours ───────────────────────────────────

def sync_warning_color() -> QColor:
    """QColor for out-of-sync test items."""
    return QColor(255, 165, 0)  # Orange — good contrast on both


def empty_test_color() -> QColor:
    """QColor for empty test folders."""
    return QColor(muted_color())


def ready_test_color() -> QColor:
    """QColor for tests with JSON + code."""
    return QColor(success_color())


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
    return QColor(error_color())


def finding_warning() -> QColor:
    """Colour for warning findings."""
    return QColor(warning_color())


def finding_info() -> QColor:
    """Colour for info findings."""
    return QColor("#42a5f5") if is_dark() else QColor("#1565c0")


def finding_success() -> str:
    """CSS colour string for success status in findings."""
    return success_color()


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


# ── Status label colours (modified / saved indicators) ──────────────

def status_modified() -> str:
    """CSS colour for modified/unsaved content indicator."""
    return "#ffa726" if is_dark() else "orange"


def status_saved() -> str:
    """CSS colour for saved/clean content indicator."""
    return success_color()
