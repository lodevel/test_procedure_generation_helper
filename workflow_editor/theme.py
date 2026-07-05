"""Theme-aware colour utilities for the workflow editor.

All UI code should use these helpers instead of hard-coding hex colours
so that widgets remain legible on both dark and light system palettes.
"""

from __future__ import annotations

from string import Template

from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication


# ── Application theme registry ───────────────────────────────────────

ORIGINAL_THEME_ID = "original"
NEUTRAL_DARK_THEME_ID = "dark"
MODERN_DARK_THEME_ID = "modern_dark"
PRIDE_THEME_ID = "pride"
PRIDE_DARK_THEME_ID = "pride_dark"

_THEME_LABELS: dict[str, str] = {
    ORIGINAL_THEME_ID: "Original / system",
    NEUTRAL_DARK_THEME_ID: "Neutral dark",
    MODERN_DARK_THEME_ID: "Modern dark",
    PRIDE_THEME_ID: "🏳️‍🌈 Pride (light)",
    PRIDE_DARK_THEME_ID: "🏳️‍🌈 Pride (dark)",
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


def apply_app_theme(
    theme_name: str | None,
    app: QApplication | None = None,
    modern_workspace: bool = False,
) -> str:
    """Apply the selected app theme and return the normalized theme id."""
    global _THEME_ACTIVE

    app = app or QApplication.instance()
    theme_id = normalize_app_theme_name(theme_name)
    if app is None:
        return theme_id

    _set_pride_fx(app, theme_id in (PRIDE_THEME_ID, PRIDE_DARK_THEME_ID))

    if theme_id in (PRIDE_THEME_ID, PRIDE_DARK_THEME_ID):
        dark = theme_id == PRIDE_DARK_THEME_ID
        _remember_original_palette(app)
        app.setPalette(_pride_dark_palette() if dark else _pride_light_palette())
        app.setStyleSheet(_pride_app_stylesheet(dark))
        _THEME_ACTIVE = True
        return theme_id

    if modern_workspace:
        # Modern workspace is an app-wide Fluent / Windows-11 design
        # language that reaches every window and dialog (set on the
        # QApplication). The theme choice only selects light vs dark.
        dark = theme_id in (NEUTRAL_DARK_THEME_ID, MODERN_DARK_THEME_ID)
        _remember_original_palette(app)
        app.setPalette(
            _neutral_dark_palette() if dark else _modern_light_palette()
        )
        app.setStyleSheet(_fluent_app_stylesheet(dark))
        _THEME_ACTIVE = True
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


def _modern_light_palette() -> QPalette:
    """Build the Fluent light palette used by the modern workspace."""
    palette = QPalette()
    window = QColor("#dde4ee")
    base = QColor("#ffffff")
    alt_base = QColor("#f5f7fb")
    text = QColor("#1f2937")
    muted = QColor("#5b6675")
    disabled = QColor("#9aa5b5")
    accent = QColor("#2f7dd3")
    border = QColor("#d5dce8")
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QPalette.ColorRole.ToolTipBase, base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, alt_base)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Light, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#f5f7fb"))
    palette.setColor(QPalette.ColorRole.Mid, border)
    palette.setColor(QPalette.ColorRole.Dark, QColor("#b9c3d4"))
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#aab4c2"))
    palette.setColor(QPalette.ColorRole.Link, accent)
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#7c3aed"))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#dbe2ec")
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, disabled
    )
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


# ── Modern-workspace Fluent app stylesheet (light + dark) ──────────
# Applied app-wide (QApplication.setStyleSheet) when the modern workspace
# layout is active, so the editor matches the main GUI's Fluent skin.
# Qt-QSS faithful; adversarially lint-verified. Shared with the main app.

def _fluent_app_stylesheet(is_dark: bool) -> str:
    """Return the app-wide modern-workspace Fluent QSS, dark or light."""
    return _FLUENT_APP_QSS_DARK if is_dark else _FLUENT_APP_QSS_LIGHT


# The light and dark skins are ONE shared QSS skeleton (_FLUENT_APP_QSS_TEMPLATE)
# instantiated with the two $token dicts below; every structural rule lives
# exactly once in the template.
_FLUENT_QSS_TOKENS_LIGHT: dict[str, str] = {
    # Core palette roles (see the header comment inside the template)
    "page": "#dde4ee",
    "panel": "#ffffff",
    "surface2": "#f5f7fb",
    "border": "#d5dce8",
    "hairline": "#e2e7f0",
    "text": "#1f2937",
    "muted": "#5b6675",
    "accent": "#4f9cf9",
    "accent_deep": "#2f7dd3",
    "input_bottom": "#b9c3d4",
    "hover": "#eef4ff",
    "pressed": "#e3ecfb",
    "selected": "#dcecff",
    # Interactive states
    "field_bg": "#ffffff",
    "hover_border": "#c3cee0",
    "pressed_border": "#b6c4da",
    "pressed_text": "#243041",
    "checked_accent": "#2f7dd3",
    "accent_text": "#1f6fc6",
    # Disabled states
    "disabled_bg": "#f1f3f8",
    "disabled_button_bg": "#f1f3f8",
    "disabled_border": "#e2e7f0",
    "disabled_text": "#9aa5b5",
    "disabled_arrow": "#aeb6c4",
    # Inputs / item views
    "input_bottom_hover": "#aab6ca",
    "quiet_bg": "#f5f7fb",
    "alt_bg": "#f5f7fb",
    "spin_down_hover_bg": "#eef4ff",
    "list_border": "#b9c3d4",
    # Check / radio / table indicators
    "indicator_checked_hover": "#2566b3",
    "indicator_checked_disabled": "#c9d6ea",
    "indicator_indeterminate": "#9bbfe8",
    "radio_dot": "#ffffff",
    # Primary CTA (QPushButton#primaryRunButton)
    "cta_grad_start": "#2c75c6",
    "cta_grad_end": "#2566b3",
    "cta_border": "#205aa0",
    "cta_text": "#ffffff",
    "cta_hover_start": "#2e77c7",
    "cta_hover_end": "#286db8",
    "cta_hover_border": "#1f5f9f",
    "cta_pressed_bg": "#2566b3",
    "cta_pressed_border": "#1c5290",
    "cta_focus_border": "#1f5390",
    "cta_disabled_start": "#a9c8ee",
    "cta_disabled_end": "#9bbfe8",
    "cta_disabled_text": "#e8eef7",
    # Scrollbars
    "scroll_handle": "#c6cfde",
    "scroll_handle_hover": "#aab6cb",
    "scroll_handle_pressed": "#93a2bd",
    # Modern footer
    "footer_border": "#d5dce8",
    "footer_sep": "#c3cee0",
    "footer_status_ok": "#1b5e20",
    # Light/dark prose in comments (kept so the QSS stays self-documenting)
    "mode_note": "LIGHT (theme.is_dark() == False)",
    "accent_note": "deep #2f7dd3  input-bottom #b9c3d4",
    "cta_note": "filled accent gradient (white text",
}

_FLUENT_QSS_TOKENS_DARK: dict[str, str] = {
    # Core palette roles (see the header comment inside the template)
    "page": "#171a21",
    "panel": "#20242c",
    "surface2": "#252b35",
    "border": "#465263",
    "hairline": "#3a4350",
    "text": "#f1f5f9",
    "muted": "#aab4c2",
    "accent": "#7ab8ff",
    "accent_deep": "#4f9cf9",
    "input_bottom": "#5b6a80",
    "hover": "#2c3440",
    "pressed": "#242a33",
    "selected": "#314b6f",
    # Interactive states
    "field_bg": "#252b35",
    "hover_border": "#566378",
    "pressed_border": "#5b6a80",
    "pressed_text": "#dfe7f1",
    "checked_accent": "#7ab8ff",
    "accent_text": "#9fc6f5",
    # Disabled states
    "disabled_bg": "#1f242c",
    "disabled_button_bg": "#1f242d",
    "disabled_border": "#353d49",
    "disabled_text": "#6b7585",
    "disabled_arrow": "#6b7585",
    # Inputs / item views
    "input_bottom_hover": "#6b7c93",
    "quiet_bg": "#20242c",
    "alt_bg": "#2c3440",
    "spin_down_hover_bg": "#354050",
    "list_border": "#465263",
    # Check / radio / table indicators
    "indicator_checked_hover": "#7ab8ff",
    "indicator_checked_disabled": "#335073",
    "indicator_indeterminate": "#3f86db",
    "radio_dot": "#171a21",
    # Primary CTA (QPushButton#primaryRunButton)
    "cta_grad_start": "#7ab8ff",
    "cta_grad_end": "#4f9cf9",
    "cta_border": "#4f9cf9",
    "cta_text": "#06203f",
    "cta_hover_start": "#8fc4ff",
    "cta_hover_end": "#5fa6fa",
    "cta_hover_border": "#5fa6fa",
    "cta_pressed_bg": "#3f86db",
    "cta_pressed_border": "#4f9cf9",
    "cta_focus_border": "#9accff",
    "cta_disabled_start": "#3a5a7f",
    "cta_disabled_end": "#335073",
    "cta_disabled_text": "#8fb4dd",
    # Scrollbars
    "scroll_handle": "#3f4856",
    "scroll_handle_hover": "#4d5868",
    "scroll_handle_pressed": "#5b6678",
    # Modern footer
    "footer_border": "#3a4350",
    "footer_sep": "#4d5868",
    "footer_status_ok": "#81c784",
    # Light/dark prose in comments (kept so the QSS stays self-documenting)
    "mode_note": "DARK (theme.is_dark() == True)",
    "accent_note": "#7ab8ff  input-bottom #5b6a80",
    "cta_note": "bright accent gradient (near-black",
}

_FLUENT_APP_QSS_TEMPLATE = r'''/* ============================================================
   MODERN WORKSPACE CHROME — $mode_note
   Fluent / Windows 11. App-level: QApplication.setStyleSheet.
   Reaches the MAIN WINDOW *and every dialog/window*.
   CHROME (verbatim) + DIALOG-WIDGET coverage appended below.
     page $page  panel $panel  surface-2 $surface2
     border $border  hairline $hairline  text $text  muted $muted
     accent #4f9cf9 / $accent_note
     hover $hover  pressed $pressed  selected $selected
   Lint-clean: no box-shadow/text-shadow/transition/animation/
   transform/opacity/calc/CSS-var/::before/::after/::placeholder/
   8-digit-hex/text-transform. Caret = Qt border-triangle idiom.
   NO bare 'QWidget{background}' / '*{}' — pages backgrounded via
   QDialog/QMainWindow/object-name ONLY, so custom-painted and
   delegate-painted widgets are never nuked.
   ============================================================ */

QWidget#mainCentral {
    background-color: $page;
    color: $text;
}

/* ============================================================
   PAGE / WINDOW BACKGROUNDS — dialogs & top-level windows
   Object-name + concrete-class scoped (NEVER bare QWidget).
   ============================================================ */
QMainWindow {
    background-color: $page;
}
QDialog {
    background-color: $page;
    color: $text;
}
QMessageBox {
    background-color: $page;
}
QMessageBox QLabel {
    color: $text;
}
QInputDialog, QFileDialog, QColorDialog, QFontDialog, QWizard, QWizardPage {
    background-color: $page;
    color: $text;
}
QDialog QLabel, QMainWindow QLabel {
    color: $text;
    background: transparent;
}
QToolTip {
    background-color: $field_bg;
    color: $text;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px 8px;
}

/* ---- GroupBox = primary card surface (Fluent 8px) -------- */
QGroupBox {
    background-color: $panel;
    border: 1px solid $border;
    border-radius: 8px;
    margin-top: 1.3em;
    padding: 12px;
    font-weight: 600;
    color: $text;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: 2px;
    padding: 0 6px;
    color: $accent_text;
    font-weight: 700;
}

/* ---- Secondary buttons (quiet Fluent, default QPushButton) */
QPushButton {
    background-color: $surface2;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 7px 14px;
    min-height: 18px;
    color: $text;
    font-weight: 600;
}
QPushButton:hover {
    background-color: $hover;
    border-color: $hover_border;
}
QPushButton:pressed {
    background-color: $pressed;
    border-color: $pressed_border;
    color: $pressed_text;
}
QPushButton:checked, QPushButton:on {
    background-color: $selected;
    border-color: $checked_accent;
    color: $text;
}
QPushButton:focus {
    border: 1px solid $accent;
}
QPushButton:disabled {
    background-color: $disabled_button_bg;
    border-color: $disabled_border;
    color: $disabled_text;
}

/* ---- Primary CTA: $cta_note, AA) - */
QPushButton#primaryRunButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 $cta_grad_start, stop:1 $cta_grad_end);
    border: 1px solid $cta_border;
    border-radius: 6px;
    padding: 8px 18px;
    color: $cta_text;
    font-weight: 700;
}
QPushButton#primaryRunButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 $cta_hover_start, stop:1 $cta_hover_end);
    border-color: $cta_hover_border;
}
QPushButton#primaryRunButton:pressed {
    background-color: $cta_pressed_bg;
    border-color: $cta_pressed_border;
}
QPushButton#primaryRunButton:focus {
    border: 2px solid $cta_focus_border;
}
QPushButton#primaryRunButton:disabled {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 $cta_disabled_start, stop:1 $cta_disabled_end);
    border-color: $cta_disabled_end;
    color: $cta_disabled_text;
}

/* ---- Line edits — Fluent bottom-border accent on focus --- */
QLineEdit {
    background-color: $field_bg;
    border: 1px solid $border;
    border-bottom: 1px solid $input_bottom;
    border-radius: 6px;
    padding: 6px 10px;
    color: $text;
    selection-background-color: $selected;
    selection-color: $text;
}
QLineEdit:hover {
    border-color: $hover_border;
    border-bottom-color: $input_bottom_hover;
}
QLineEdit:focus {
    border: 1px solid $accent;
    border-bottom: 2px solid $accent_deep;
    padding-bottom: 5px;
    background-color: $field_bg;
}
QLineEdit:disabled {
    background-color: $disabled_bg;
    color: $disabled_text;
    border-color: $disabled_border;
    border-bottom-color: $disabled_border;
}
QLineEdit:read-only {
    background-color: $quiet_bg;
    color: $muted;
}

/* ---- Combo boxes (incl. editable line edit + popup) ------ */
/* Inline (double-click) cell editors inherit the standalone-field padding
   (6px top/bottom + borders), which is too tall for an item-view row and
   crops the editor. Trim it (higher-specificity descendant rule) so the
   editor fills the cell and glyphs / spin arrows are not clipped. */
QAbstractItemView QLineEdit,
QAbstractItemView QSpinBox,
QAbstractItemView QDoubleSpinBox,
QAbstractItemView QComboBox {
    padding: 1px 4px;
    border-radius: 3px;
}

QComboBox {
    background-color: $field_bg;
    border: 1px solid $border;
    border-bottom: 1px solid $input_bottom;
    border-radius: 6px;
    padding: 6px 10px;
    color: $text;
    min-height: 18px;
}
QComboBox:hover {
    background-color: $hover;
    border-color: $hover_border;
    border-bottom-color: $input_bottom_hover;
}
QComboBox:focus, QComboBox:on {
    border: 1px solid $accent;
    border-bottom: 2px solid $accent_deep;
    padding-bottom: 5px;
}
QComboBox:disabled {
    background-color: $disabled_bg;
    color: $disabled_text;
    border-color: $disabled_border;
    border-bottom-color: $disabled_border;
}
QComboBox:editable {
    background-color: $field_bg;
}
QComboBox QLineEdit {
    border: none;
    border-radius: 0;
    padding: 0;
    background: transparent;
    color: $text;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid $hairline;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
    background: transparent;
}
QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $muted;
    margin-top: 1px;
    margin-right: 7px;
    margin-left: 7px;
}
QComboBox::down-arrow:disabled {
    border-top-color: $disabled_arrow;
}
QComboBox QAbstractItemView {
    background-color: $field_bg;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    color: $text;
    selection-background-color: $selected;
    selection-color: $text;
}

/* ============================================================
   SPIN BOXES — QSpinBox / QDoubleSpinBox (Fluent inputs)
   ============================================================ */
QSpinBox, QDoubleSpinBox {
    background-color: $field_bg;
    border: 1px solid $border;
    border-bottom: 1px solid $input_bottom;
    border-radius: 6px;
    padding: 6px 10px;
    color: $text;
    min-height: 18px;
    selection-background-color: $selected;
    selection-color: $text;
}
QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: $hover_border;
    border-bottom-color: $input_bottom_hover;
}
QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid $accent;
    border-bottom: 2px solid $accent_deep;
    padding-bottom: 5px;
}
QSpinBox:disabled, QDoubleSpinBox:disabled {
    background-color: $disabled_bg;
    color: $disabled_text;
    border-color: $disabled_border;
    border-bottom-color: $disabled_border;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid $hairline;
    border-top-right-radius: 6px;
    background-color: $alt_bg;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border-left: 1px solid $hairline;
    border-bottom-right-radius: 6px;
    background-color: $alt_bg;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: $spin_down_hover_bg;
}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
    background-color: $pressed;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid $muted;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $muted;
}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {
    border-bottom-color: $disabled_arrow;
}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {
    border-top-color: $disabled_arrow;
}

/* ============================================================
   MULTILINE EDITORS — QPlainTextEdit / QTextEdit / QTextBrowser
   (terminal browser keeps its own per-widget sheet; overrides.)
   ============================================================ */
QPlainTextEdit, QTextEdit, QTextBrowser {
    background-color: $panel;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
    color: $text;
    selection-background-color: $selected;
    selection-color: $text;
}
QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus {
    border: 1px solid $accent;
}
QPlainTextEdit:disabled, QTextEdit:disabled, QTextBrowser:disabled {
    background-color: $disabled_bg;
    color: $disabled_text;
    border-color: $disabled_border;
}

/* ============================================================
   TAB WIDGET / TAB BAR — selected = card, unselected = surface-2
   ============================================================ */
QTabWidget::pane {
    background-color: $panel;
    border: 1px solid $border;
    border-radius: 8px;
    top: -1px;
}
QTabWidget::tab-bar {
    left: 8px;
}
QTabBar {
    background: transparent;
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background-color: $surface2;
    border: 1px solid $border;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 7px 16px;
    margin-right: 2px;
    color: $muted;
    font-weight: 600;
}
QTabBar::tab:hover {
    background-color: $hover;
    color: $text;
}
QTabBar::tab:selected {
    background-color: $panel;
    border-color: $border;
    color: $accent_text;
    border-top: 2px solid $accent_deep;
    padding-top: 6px;
}
QTabBar::tab:!selected {
    margin-top: 2px;
}
QTabBar::tab:first {
    margin-left: 0;
}
QTabBar::tab:disabled {
    color: $disabled_text;
}
QTabBar::tab:left, QTabBar::tab:right {
    border: 1px solid $border;
    border-radius: 0;
    padding: 12px 8px;
}

/* ============================================================
   HEADER VIEW — table/tree headers
   ============================================================ */
QHeaderView {
    background-color: $surface2;
    border: none;
}
QHeaderView::section {
    background-color: $surface2;
    color: $muted;
    padding: 6px 10px;
    border: none;
    border-right: 1px solid $hairline;
    border-bottom: 1px solid $border;
    font-weight: 700;
}
QHeaderView::section:horizontal {
    border-right: 1px solid $hairline;
}
QHeaderView::section:vertical {
    border-right: 1px solid $border;
    border-bottom: 1px solid $hairline;
    text-align: left;
}
QHeaderView::section:hover {
    background-color: $hover;
    color: $text;
}
QHeaderView::section:pressed {
    background-color: $pressed;
}
QHeaderView::section:checked {
    background-color: $selected;
    color: $accent_text;
}
QHeaderView::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $muted;
    subcontrol-position: center right;
    margin-right: 6px;
}
QHeaderView::up-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid $muted;
    subcontrol-position: center right;
    margin-right: 6px;
}

/* ============================================================
   TABLE VIEW / TABLE WIDGET — gridlines, selection, corner
   ============================================================ */
QTableView, QTableWidget {
    background-color: $field_bg;
    alternate-background-color: $alt_bg;
    border: 1px solid $border;
    border-radius: 6px;
    gridline-color: $hairline;
    color: $text;
    selection-background-color: $selected;
    selection-color: $text;
    outline: 0;
}
QTableView::item, QTableWidget::item {
    padding: 4px 6px;
    border: none;
}
QTableView::item:hover, QTableWidget::item:hover {
    background-color: $hover;
}
QTableView::item:selected, QTableWidget::item:selected {
    background-color: $selected;
    color: $text;
}
QTableView::item:focus, QTableWidget::item:focus {
    background-color: $selected;
}
QTableCornerButton::section {
    background-color: $surface2;
    border: none;
    border-right: 1px solid $border;
    border-bottom: 1px solid $border;
}

/* ============================================================
   TREE / LIST VIEWS — items, selection, hover, branches
   Generic QListWidget kept CONSERVATIVE so the main window's
   delegate-painted #procedureCards / #equipmentCards override.
   ============================================================ */
QTreeView, QTreeWidget {
    background-color: $field_bg;
    alternate-background-color: $alt_bg;
    border: 1px solid $border;
    border-radius: 6px;
    color: $text;
    selection-background-color: $selected;
    selection-color: $text;
    outline: 0;
}
QTreeView::item, QTreeWidget::item {
    padding: 4px 2px;
    border: none;
}
QTreeView::item:hover, QTreeWidget::item:hover {
    background-color: $hover;
}
QTreeView::item:selected, QTreeWidget::item:selected {
    background-color: $selected;
    color: $text;
}
QTreeView::branch:hover {
    background-color: $hover;
}
QTreeView::branch:selected, QTreeWidget::branch:selected {
    background-color: $selected;
}

QListView, QListWidget {
    background-color: $field_bg;
    border: 1px solid $list_border;
    border-radius: 6px;
    color: $text;
    selection-background-color: $selected;
    selection-color: $text;
    outline: 0;
}
QListView::item, QListWidget::item {
    padding: 5px 8px;
    border: 1px solid transparent;
    border-radius: 4px;
}
QListView::item:hover, QListWidget::item:hover {
    background-color: $hover;
    border-color: $hairline;
}
QListView::item:selected, QListWidget::item:selected {
    background-color: $selected;
    border-color: $hover_border;
    color: $text;
}

/* ============================================================
   CHECKBOX — indicator drawn purely with border+bg (no asset)
   ============================================================ */
QCheckBox {
    color: $text;
    spacing: 8px;
    background: transparent;
}
QCheckBox::indicator,
QListView::indicator,
QListWidget::indicator,
QTreeView::indicator,
QTreeWidget::indicator,
QTableView::indicator,
QTableWidget::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $input_bottom;
    border-radius: 4px;
    background-color: $field_bg;
}
QCheckBox::indicator:hover {
    border-color: $accent;
    background-color: $hover;
}
QCheckBox::indicator:checked,
QListView::indicator:checked,
QListWidget::indicator:checked,
QTreeView::indicator:checked,
QTreeWidget::indicator:checked,
QTableView::indicator:checked,
QTableWidget::indicator:checked {
    border-color: $accent_deep;
    background-color: $accent_deep;
    image: none;
}
QCheckBox::indicator:checked:hover {
    border-color: $indicator_checked_hover;
    background-color: $indicator_checked_hover;
}
QCheckBox::indicator:indeterminate,
QListView::indicator:indeterminate,
QListWidget::indicator:indeterminate,
QTreeView::indicator:indeterminate,
QTreeWidget::indicator:indeterminate,
QTableView::indicator:indeterminate,
QTableWidget::indicator:indeterminate {
    border-color: $accent_deep;
    background-color: $indicator_indeterminate;
}
QCheckBox::indicator:disabled,
QListView::indicator:disabled,
QListWidget::indicator:disabled,
QTreeView::indicator:disabled,
QTreeWidget::indicator:disabled,
QTableView::indicator:disabled,
QTableWidget::indicator:disabled {
    border-color: $disabled_border;
    background-color: $disabled_button_bg;
}
QCheckBox::indicator:checked:disabled,
QListView::indicator:checked:disabled,
QListWidget::indicator:checked:disabled,
QTreeView::indicator:checked:disabled,
QTreeWidget::indicator:checked:disabled,
QTableView::indicator:checked:disabled,
QTableWidget::indicator:checked:disabled {
    border-color: $indicator_checked_disabled;
    background-color: $indicator_checked_disabled;
}
QCheckBox:disabled {
    color: $disabled_text;
}

/* ============================================================
   RADIO BUTTON — circular indicator (border+bg, no asset)
   ============================================================ */
QRadioButton {
    color: $text;
    spacing: 8px;
    background: transparent;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $input_bottom;
    border-radius: 9px;
    background-color: $field_bg;
}
QRadioButton::indicator:hover {
    border-color: $accent;
    background-color: $hover;
}
QRadioButton::indicator:checked {
    border: 4px solid $accent_deep;
    background-color: $radio_dot;
}
QRadioButton::indicator:checked:hover {
    border-color: $indicator_checked_hover;
}
QRadioButton::indicator:disabled {
    border-color: $disabled_border;
    background-color: $disabled_button_bg;
}
QRadioButton::indicator:checked:disabled {
    border: 4px solid $indicator_checked_disabled;
    background-color: $radio_dot;
}
QRadioButton:disabled {
    color: $disabled_text;
}

/* ============================================================
   DIALOG BUTTON BOX — consistent button sizing
   ============================================================ */
QDialogButtonBox {
}
QDialogButtonBox QPushButton {
    min-width: 84px;
    padding: 6px 12px;
    font-weight: 400;
}

/* ============================================================
   TOOL BUTTON — flat Fluent, lights on hover
   ============================================================ */
QToolButton {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 8px;
    color: $text;
    font-weight: 600;
}
QToolButton:hover {
    background-color: $hover;
    border-color: $hover_border;
}
QToolButton:pressed {
    background-color: $pressed;
    border-color: $pressed_border;
}
QToolButton:checked, QToolButton:on {
    background-color: $selected;
    border-color: $checked_accent;
}
QToolButton:disabled {
    color: $disabled_text;
}
QToolButton::menu-indicator {
    image: none;
    width: 0;
    height: 0;
}

/* ============================================================
   MENU BAR / MENU
   ============================================================ */
QMenuBar {
    background-color: $page;
    color: $text;
    border-bottom: 1px solid $border;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}
QMenuBar::item:selected {
    background-color: $hover;
    color: $text;
}
QMenuBar::item:pressed {
    background-color: $pressed;
}
QMenu {
    background-color: $panel;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 4px;
    color: $text;
}
QMenu::item {
    background: transparent;
    padding: 6px 24px 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: $selected;
    color: $text;
}
QMenu::item:disabled {
    color: $disabled_text;
}
QMenu::separator {
    height: 1px;
    background-color: $hairline;
    margin: 4px 8px;
}
QMenu::indicator {
    width: 14px;
    height: 14px;
    left: 6px;
}

/* ============================================================
   PROGRESS BAR
   ============================================================ */
QProgressBar {
    background-color: $surface2;
    border: 1px solid $border;
    border-radius: 6px;
    text-align: center;
    color: $text;
    min-height: 16px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4f9cf9, stop:1 $checked_accent);
    border-radius: 5px;
    margin: 1px;
}

/* ============================================================
   STATUS BAR (generic, app-wide)
   ============================================================ */
QStatusBar {
    background-color: $quiet_bg;
    color: $muted;
    border-top: 1px solid $border;
}
QStatusBar::item {
    border: none;
}
QStatusBar QLabel {
    color: $muted;
}

/* ============================================================
   SCROLL AREA — keep transparent so card surfaces show through
   ============================================================ */
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}

/* ============================================================
   SLIDER (optional)
   ============================================================ */
QSlider::groove:horizontal {
    height: 4px;
    background-color: $border;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background-color: $accent_deep;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background-color: $panel;
    border: 1px solid $accent_deep;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    background-color: $hover;
}
QSlider::groove:vertical {
    width: 4px;
    background-color: $border;
    border-radius: 2px;
}
QSlider::sub-page:vertical {
    background-color: $accent_deep;
    border-radius: 2px;
}
QSlider::handle:vertical {
    background-color: $panel;
    border: 1px solid $accent_deep;
    width: 14px;
    height: 14px;
    margin: 0 -6px;
    border-radius: 8px;
}

/* ---- Scrollbars: thin modern (vertical + horizontal) ----- */
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background-color: $scroll_handle;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background-color: $scroll_handle_hover;
}
QScrollBar::handle:vertical:pressed {
    background-color: $scroll_handle_pressed;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background-color: $scroll_handle;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background-color: $scroll_handle_hover;
}
QScrollBar::handle:horizontal:pressed {
    background-color: $scroll_handle_pressed;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
    background: none;
    border: none;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}

/* ---- Splitter handle (gap between panes) ----------------- */
QSplitter::handle {
    background-color: $page;
}
QSplitter::handle:horizontal {
    width: 10px;
}
QSplitter::handle:vertical {
    height: 10px;
}
QSplitter::handle:hover {
    background-color: $border;
}

/* ---- Footer = status bar surface ------------------------- */
QWidget#modernFooter {
    background-color: $quiet_bg;
    border: 1px solid $footer_border;
    border-radius: 8px;
}
QWidget#modernFooter QLabel {
    color: $muted;
    font-weight: 500;
    padding: 0 2px;
}
QLabel#footerSep {
    color: $footer_sep;
    font-weight: 400;
}
QLabel#footerStatus {
    color: $footer_status_ok;
    font-weight: 700;
}

/* ---- Inline section header labels (objectName below) ----- */
QLabel#sectionLabel {
    color: $muted;
    font-weight: 700;
    padding-top: 2px;
}'''

_FLUENT_APP_QSS_LIGHT = Template(_FLUENT_APP_QSS_TEMPLATE).substitute(
    _FLUENT_QSS_TOKENS_LIGHT
)
_FLUENT_APP_QSS_DARK = Template(_FLUENT_APP_QSS_TEMPLATE).substitute(
    _FLUENT_QSS_TOKENS_DARK
)


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

# ── Pride theme (LGBTQIA+) ───────────────────────────
# Palette mirrors the existing builders; only the colours differ.
# The app stylesheet reuses the Fluent QSS and appends a pride override
# block (later QSS rules win), so all base widget coverage is retained.


def _pride_light_palette() -> QPalette:
    """Build the light Pride palette (mirrors _modern_light_palette)."""
    palette = QPalette()
    window = QColor("#F4EEF7")
    base = QColor("#FFFFFF")
    alt_base = QColor("#F6F0FA")
    text = QColor("#1F2937")
    muted = QColor("#6A5A78")
    disabled = QColor("#9A8FA6")
    accent = QColor("#7048E8")
    border = QColor("#D9C9E6")
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QPalette.ColorRole.ToolTipBase, base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, QColor("#F2EAF8"))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Light, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Midlight, alt_base)
    palette.setColor(QPalette.ColorRole.Mid, border)
    palette.setColor(QPalette.ColorRole.Dark, QColor("#b9c3d4"))
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#aab4c2"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#3B5BDB"))
    palette.setColor(QPalette.ColorRole.LinkVisited, accent)
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#dbe2ec")
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, disabled
    )
    return palette


def _pride_dark_palette() -> QPalette:
    """Build the dark Pride palette (mirrors _neutral_dark_palette)."""
    palette = QPalette()
    window = QColor("#1A1622")
    panel = QColor("#20242C")
    base = QColor("#1B1E24")
    alt_base = QColor("#252B35")
    text = QColor("#F1F5F9")
    muted = QColor("#A99BBA")
    disabled = QColor("#7A6E8A")
    accent = QColor("#9775FA")
    border = QColor("#465263")
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QPalette.ColorRole.Light, QColor("#4a515e"))
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#3A4350"))
    palette.setColor(QPalette.ColorRole.Dark, QColor("#151820"))
    palette.setColor(QPalette.ColorRole.Mid, border)
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#0f1117"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#5C7CFA"))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#B197FC"))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#0C0E14"))
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


_PRIDE_OVERRIDE_LIGHT = """
QTabBar::tab:selected { background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #e63946, stop:0.2 #f3722c, stop:0.4 #f9c74f, stop:0.6 #2a9d8f, stop:0.8 #3b5bdb, stop:1 #7048e8); color:#1f2937; font-weight:700; }
QProgressBar { text-align:center; color:#1f2937; }
QProgressBar::chunk { background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #e63946, stop:0.2 #f3722c, stop:0.4 #f9c74f, stop:0.6 #2a9d8f, stop:0.8 #3b5bdb, stop:1 #7048e8); border-radius:5px; margin:1px; }
QGroupBox::title { color:#7048e8; font-weight:700; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QComboBox:on, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border:1px solid #7048e8; border-bottom:2px solid #7048e8; }
QPushButton#primaryRunButton { background-color: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #c1304a, stop:0.25 #c75c2e, stop:0.5 #2a7f74, stop:0.75 #2f4fc0, stop:1 #5a37b8); border:1px solid #2f3aa0; border-radius:6px; padding:8px 18px; color:#ffffff; font-weight:700; }
QPushButton#primaryRunButton:hover { border-color:#1f2f90; }
QPushButton#primaryRunButton:pressed { background-color:#5a37b8; }
"""


_PRIDE_OVERRIDE_DARK = """
QTabBar::tab:selected { background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff6b6b, stop:0.2 #ffa94d, stop:0.4 #ffd43b, stop:0.6 #51cf66, stop:0.8 #5c7cfa, stop:1 #b197fc); color:#1b1e24; font-weight:700; }
QProgressBar { text-align:center; color:#1b1e24; }
QProgressBar::chunk { background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff6b6b, stop:0.2 #ffa94d, stop:0.4 #ffd43b, stop:0.6 #51cf66, stop:0.8 #5c7cfa, stop:1 #b197fc); border-radius:5px; margin:1px; }
QGroupBox::title { color:#b197fc; font-weight:700; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QComboBox:on, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border:1px solid #9775fa; border-bottom:2px solid #9775fa; }
QPushButton#primaryRunButton { background-color: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #c1304a, stop:0.25 #c75c2e, stop:0.5 #2a7f74, stop:0.75 #2f4fc0, stop:1 #5a37b8); border:1px solid #2f3aa0; border-radius:6px; padding:8px 18px; color:#ffffff; font-weight:700; }
QPushButton#primaryRunButton:hover { border-color:#1f2f90; }
QPushButton#primaryRunButton:pressed { background-color:#5a37b8; }
"""


def _pride_app_stylesheet(dark: bool) -> str:
    """Return the Fluent QSS for this mode plus the pride override block."""
    base = _FLUENT_APP_QSS_DARK if dark else _FLUENT_APP_QSS_LIGHT
    override = _PRIDE_OVERRIDE_DARK if dark else _PRIDE_OVERRIDE_LIGHT
    return base + override


def _set_pride_fx(app: QApplication, on: bool) -> None:
    """Enable/disable the playful Pride cursor + click sparkles.

    Cosmetic only and fully guarded — it must never affect theming, so any
    failure in the effects module is swallowed here.
    """
    try:
        from . import pride_fx
        pride_fx.set_enabled(app, on)
    except Exception:
        pass  # best-effort: cosmetic pride FX only; a failure here must never affect theming
