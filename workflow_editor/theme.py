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
    window = QColor("#eef1f7")
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


_FLUENT_APP_QSS_LIGHT = r'''/* ============================================================
   MODERN WORKSPACE CHROME — LIGHT (theme.is_dark() == False)
   Fluent / Windows 11. App-level: QApplication.setStyleSheet.
   Reaches the MAIN WINDOW *and every dialog/window*.
   CHROME (verbatim) + DIALOG-WIDGET coverage appended below.
     page #eef1f7  panel #ffffff  surface-2 #f5f7fb
     border #d5dce8  hairline #e2e7f0  text #1f2937  muted #5b6675
     accent #4f9cf9 / deep #2f7dd3  input-bottom #b9c3d4
     hover #eef4ff  pressed #e3ecfb  selected #dcecff
   Lint-clean: no box-shadow/text-shadow/transition/animation/
   transform/opacity/calc/CSS-var/::before/::after/::placeholder/
   8-digit-hex/text-transform. Caret = Qt border-triangle idiom.
   NO bare 'QWidget{background}' / '*{}' — pages backgrounded via
   QDialog/QMainWindow/object-name ONLY, so custom-painted and
   delegate-painted widgets are never nuked.
   ============================================================ */

QWidget#mainCentral {
    background-color: #eef1f7;
    color: #1f2937;
}

/* ============================================================
   PAGE / WINDOW BACKGROUNDS — dialogs & top-level windows
   Object-name + concrete-class scoped (NEVER bare QWidget).
   ============================================================ */
QMainWindow {
    background-color: #eef1f7;
}
QDialog {
    background-color: #eef1f7;
    color: #1f2937;
}
QMessageBox {
    background-color: #eef1f7;
}
QMessageBox QLabel {
    color: #1f2937;
}
QInputDialog, QFileDialog, QColorDialog, QFontDialog, QWizard, QWizardPage {
    background-color: #eef1f7;
    color: #1f2937;
}
QDialog QLabel, QMainWindow QLabel {
    color: #1f2937;
    background: transparent;
}
QToolTip {
    background-color: #ffffff;
    color: #1f2937;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    padding: 4px 8px;
}

/* ---- GroupBox = primary card surface (Fluent 8px) -------- */
QGroupBox {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-radius: 8px;
    margin-top: 1.3em;
    padding: 12px;
    font-weight: 600;
    color: #1f2937;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: 2px;
    padding: 0 6px;
    color: #1f6fc6;
    font-weight: 700;
}

/* ---- Secondary buttons (quiet Fluent, default QPushButton) */
QPushButton {
    background-color: #f5f7fb;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    padding: 7px 14px;
    min-height: 18px;
    color: #1f2937;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #eef4ff;
    border-color: #c3cee0;
}
QPushButton:pressed {
    background-color: #e3ecfb;
    border-color: #b6c4da;
    color: #243041;
}
QPushButton:checked, QPushButton:on {
    background-color: #dcecff;
    border-color: #2f7dd3;
    color: #1f2937;
}
QPushButton:focus {
    border: 1px solid #4f9cf9;
}
QPushButton:disabled {
    background-color: #f1f3f8;
    border-color: #e2e7f0;
    color: #9aa5b5;
}

/* ---- Primary CTA: filled accent gradient (white text, AA) - */
QPushButton#primaryRunButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2c75c6, stop:1 #2566b3);
    border: 1px solid #205aa0;
    border-radius: 6px;
    padding: 8px 18px;
    color: #ffffff;
    font-weight: 700;
}
QPushButton#primaryRunButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2e77c7, stop:1 #286db8);
    border-color: #1f5f9f;
}
QPushButton#primaryRunButton:pressed {
    background-color: #2566b3;
    border-color: #1c5290;
}
QPushButton#primaryRunButton:focus {
    border: 2px solid #1f5390;
}
QPushButton#primaryRunButton:disabled {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #a9c8ee, stop:1 #9bbfe8);
    border-color: #9bbfe8;
    color: #e8eef7;
}

/* ---- Line edits — Fluent bottom-border accent on focus --- */
QLineEdit {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-bottom: 1px solid #b9c3d4;
    border-radius: 6px;
    padding: 6px 10px;
    color: #1f2937;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
}
QLineEdit:hover {
    border-color: #c3cee0;
    border-bottom-color: #aab6ca;
}
QLineEdit:focus {
    border: 1px solid #4f9cf9;
    border-bottom: 2px solid #2f7dd3;
    padding-bottom: 5px;
    background-color: #ffffff;
}
QLineEdit:disabled {
    background-color: #f1f3f8;
    color: #9aa5b5;
    border-color: #e2e7f0;
    border-bottom-color: #e2e7f0;
}
QLineEdit:read-only {
    background-color: #f5f7fb;
    color: #5b6675;
}

/* ---- Combo boxes (incl. editable line edit + popup) ------ */
QComboBox {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-bottom: 1px solid #b9c3d4;
    border-radius: 6px;
    padding: 6px 10px;
    color: #1f2937;
    min-height: 18px;
}
QComboBox:hover {
    background-color: #eef4ff;
    border-color: #c3cee0;
    border-bottom-color: #aab6ca;
}
QComboBox:focus, QComboBox:on {
    border: 1px solid #4f9cf9;
    border-bottom: 2px solid #2f7dd3;
    padding-bottom: 5px;
}
QComboBox:disabled {
    background-color: #f1f3f8;
    color: #9aa5b5;
    border-color: #e2e7f0;
    border-bottom-color: #e2e7f0;
}
QComboBox:editable {
    background-color: #ffffff;
}
QComboBox QLineEdit {
    border: none;
    border-radius: 0;
    padding: 0;
    background: transparent;
    color: #1f2937;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid #e2e7f0;
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
    border-top: 5px solid #5b6675;
    margin-top: 1px;
    margin-right: 7px;
    margin-left: 7px;
}
QComboBox::down-arrow:disabled {
    border-top-color: #aeb6c4;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    color: #1f2937;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
}

/* ============================================================
   SPIN BOXES — QSpinBox / QDoubleSpinBox (Fluent inputs)
   ============================================================ */
QSpinBox, QDoubleSpinBox {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-bottom: 1px solid #b9c3d4;
    border-radius: 6px;
    padding: 6px 10px;
    color: #1f2937;
    min-height: 18px;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
}
QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: #c3cee0;
    border-bottom-color: #aab6ca;
}
QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #4f9cf9;
    border-bottom: 2px solid #2f7dd3;
    padding-bottom: 5px;
}
QSpinBox:disabled, QDoubleSpinBox:disabled {
    background-color: #f1f3f8;
    color: #9aa5b5;
    border-color: #e2e7f0;
    border-bottom-color: #e2e7f0;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid #e2e7f0;
    border-top-right-radius: 6px;
    background-color: #f5f7fb;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border-left: 1px solid #e2e7f0;
    border-bottom-right-radius: 6px;
    background-color: #f5f7fb;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #eef4ff;
}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
    background-color: #e3ecfb;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #5b6675;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #5b6675;
}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {
    border-bottom-color: #aeb6c4;
}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {
    border-top-color: #aeb6c4;
}

/* ============================================================
   MULTILINE EDITORS — QPlainTextEdit / QTextEdit / QTextBrowser
   (terminal browser keeps its own per-widget sheet; overrides.)
   ============================================================ */
QPlainTextEdit, QTextEdit, QTextBrowser {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    padding: 4px;
    color: #1f2937;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
}
QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus {
    border: 1px solid #4f9cf9;
}
QPlainTextEdit:disabled, QTextEdit:disabled, QTextBrowser:disabled {
    background-color: #f1f3f8;
    color: #9aa5b5;
    border-color: #e2e7f0;
}

/* ============================================================
   TAB WIDGET / TAB BAR — selected = card, unselected = surface-2
   ============================================================ */
QTabWidget::pane {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
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
    background-color: #f5f7fb;
    border: 1px solid #d5dce8;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 7px 16px;
    margin-right: 2px;
    color: #5b6675;
    font-weight: 600;
}
QTabBar::tab:hover {
    background-color: #eef4ff;
    color: #1f2937;
}
QTabBar::tab:selected {
    background-color: #ffffff;
    border-color: #d5dce8;
    color: #1f6fc6;
    border-top: 2px solid #2f7dd3;
    padding-top: 6px;
}
QTabBar::tab:!selected {
    margin-top: 2px;
}
QTabBar::tab:first {
    margin-left: 0;
}
QTabBar::tab:disabled {
    color: #9aa5b5;
}
QTabBar::tab:left, QTabBar::tab:right {
    border: 1px solid #d5dce8;
    border-radius: 0;
    padding: 12px 8px;
}

/* ============================================================
   HEADER VIEW — table/tree headers
   ============================================================ */
QHeaderView {
    background-color: #f5f7fb;
    border: none;
}
QHeaderView::section {
    background-color: #f5f7fb;
    color: #5b6675;
    padding: 6px 10px;
    border: none;
    border-right: 1px solid #e2e7f0;
    border-bottom: 1px solid #d5dce8;
    font-weight: 700;
}
QHeaderView::section:horizontal {
    border-right: 1px solid #e2e7f0;
}
QHeaderView::section:vertical {
    border-right: 1px solid #d5dce8;
    border-bottom: 1px solid #e2e7f0;
    text-align: left;
}
QHeaderView::section:hover {
    background-color: #eef4ff;
    color: #1f2937;
}
QHeaderView::section:pressed {
    background-color: #e3ecfb;
}
QHeaderView::section:checked {
    background-color: #dcecff;
    color: #1f6fc6;
}
QHeaderView::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #5b6675;
    subcontrol-position: center right;
    margin-right: 6px;
}
QHeaderView::up-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #5b6675;
    subcontrol-position: center right;
    margin-right: 6px;
}

/* ============================================================
   TABLE VIEW / TABLE WIDGET — gridlines, selection, corner
   ============================================================ */
QTableView, QTableWidget {
    background-color: #ffffff;
    alternate-background-color: #f5f7fb;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    gridline-color: #e2e7f0;
    color: #1f2937;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
    outline: 0;
}
QTableView::item, QTableWidget::item {
    padding: 4px 6px;
    border: none;
}
QTableView::item:hover, QTableWidget::item:hover {
    background-color: #eef4ff;
}
QTableView::item:selected, QTableWidget::item:selected {
    background-color: #dcecff;
    color: #1f2937;
}
QTableView::item:focus, QTableWidget::item:focus {
    background-color: #dcecff;
}
QTableCornerButton::section {
    background-color: #f5f7fb;
    border: none;
    border-right: 1px solid #d5dce8;
    border-bottom: 1px solid #d5dce8;
}

/* ============================================================
   TREE / LIST VIEWS — items, selection, hover, branches
   Generic QListWidget kept CONSERVATIVE so the main window's
   delegate-painted #procedureCards / #equipmentCards override.
   ============================================================ */
QTreeView, QTreeWidget {
    background-color: #ffffff;
    alternate-background-color: #f5f7fb;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    color: #1f2937;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
    outline: 0;
}
QTreeView::item, QTreeWidget::item {
    padding: 4px 2px;
    border: none;
}
QTreeView::item:hover, QTreeWidget::item:hover {
    background-color: #eef4ff;
}
QTreeView::item:selected, QTreeWidget::item:selected {
    background-color: #dcecff;
    color: #1f2937;
}
QTreeView::branch:hover {
    background-color: #eef4ff;
}
QTreeView::branch:selected, QTreeWidget::branch:selected {
    background-color: #dcecff;
}

QListView, QListWidget {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    color: #1f2937;
    selection-background-color: #dcecff;
    selection-color: #1f2937;
    outline: 0;
}
QListView::item, QListWidget::item {
    padding: 5px 8px;
    border: 1px solid transparent;
    border-radius: 4px;
}
QListView::item:hover, QListWidget::item:hover {
    background-color: #eef4ff;
    border-color: #e2e7f0;
}
QListView::item:selected, QListWidget::item:selected {
    background-color: #dcecff;
    border-color: #c3cee0;
    color: #1f2937;
}

/* ============================================================
   CHECKBOX — indicator drawn purely with border+bg (no asset)
   ============================================================ */
QCheckBox {
    color: #1f2937;
    spacing: 8px;
    background: transparent;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #b9c3d4;
    border-radius: 4px;
    background-color: #ffffff;
}
QCheckBox::indicator:hover {
    border-color: #4f9cf9;
    background-color: #eef4ff;
}
QCheckBox::indicator:checked {
    border-color: #2f7dd3;
    background-color: #2f7dd3;
    image: none;
}
QCheckBox::indicator:checked:hover {
    border-color: #2566b3;
    background-color: #2566b3;
}
QCheckBox::indicator:indeterminate {
    border-color: #2f7dd3;
    background-color: #9bbfe8;
}
QCheckBox::indicator:disabled {
    border-color: #e2e7f0;
    background-color: #f1f3f8;
}
QCheckBox::indicator:checked:disabled {
    border-color: #c9d6ea;
    background-color: #c9d6ea;
}
QCheckBox:disabled {
    color: #9aa5b5;
}

/* ============================================================
   RADIO BUTTON — circular indicator (border+bg, no asset)
   ============================================================ */
QRadioButton {
    color: #1f2937;
    spacing: 8px;
    background: transparent;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #b9c3d4;
    border-radius: 9px;
    background-color: #ffffff;
}
QRadioButton::indicator:hover {
    border-color: #4f9cf9;
    background-color: #eef4ff;
}
QRadioButton::indicator:checked {
    border: 4px solid #2f7dd3;
    background-color: #ffffff;
}
QRadioButton::indicator:checked:hover {
    border-color: #2566b3;
}
QRadioButton::indicator:disabled {
    border-color: #e2e7f0;
    background-color: #f1f3f8;
}
QRadioButton::indicator:checked:disabled {
    border: 4px solid #c9d6ea;
    background-color: #ffffff;
}
QRadioButton:disabled {
    color: #9aa5b5;
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
    color: #1f2937;
    font-weight: 600;
}
QToolButton:hover {
    background-color: #eef4ff;
    border-color: #c3cee0;
}
QToolButton:pressed {
    background-color: #e3ecfb;
    border-color: #b6c4da;
}
QToolButton:checked, QToolButton:on {
    background-color: #dcecff;
    border-color: #2f7dd3;
}
QToolButton:disabled {
    color: #9aa5b5;
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
    background-color: #eef1f7;
    color: #1f2937;
    border-bottom: 1px solid #d5dce8;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}
QMenuBar::item:selected {
    background-color: #eef4ff;
    color: #1f2937;
}
QMenuBar::item:pressed {
    background-color: #e3ecfb;
}
QMenu {
    background-color: #ffffff;
    border: 1px solid #d5dce8;
    border-radius: 8px;
    padding: 4px;
    color: #1f2937;
}
QMenu::item {
    background: transparent;
    padding: 6px 24px 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #dcecff;
    color: #1f2937;
}
QMenu::item:disabled {
    color: #9aa5b5;
}
QMenu::separator {
    height: 1px;
    background-color: #e2e7f0;
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
    background-color: #f5f7fb;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    text-align: center;
    color: #1f2937;
    min-height: 16px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4f9cf9, stop:1 #2f7dd3);
    border-radius: 5px;
    margin: 1px;
}

/* ============================================================
   STATUS BAR (generic, app-wide)
   ============================================================ */
QStatusBar {
    background-color: #f5f7fb;
    color: #5b6675;
    border-top: 1px solid #d5dce8;
}
QStatusBar::item {
    border: none;
}
QStatusBar QLabel {
    color: #5b6675;
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
QAbstractScrollArea {
    background: transparent;
}

/* ============================================================
   SLIDER (optional)
   ============================================================ */
QSlider::groove:horizontal {
    height: 4px;
    background-color: #d5dce8;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background-color: #2f7dd3;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background-color: #ffffff;
    border: 1px solid #2f7dd3;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    background-color: #eef4ff;
}
QSlider::groove:vertical {
    width: 4px;
    background-color: #d5dce8;
    border-radius: 2px;
}
QSlider::sub-page:vertical {
    background-color: #2f7dd3;
    border-radius: 2px;
}
QSlider::handle:vertical {
    background-color: #ffffff;
    border: 1px solid #2f7dd3;
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
    background-color: #c6cfde;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background-color: #aab6cb;
}
QScrollBar::handle:vertical:pressed {
    background-color: #93a2bd;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background-color: #c6cfde;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #aab6cb;
}
QScrollBar::handle:horizontal:pressed {
    background-color: #93a2bd;
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
    background-color: #eef1f7;
}
QSplitter::handle:horizontal {
    width: 10px;
}
QSplitter::handle:vertical {
    height: 10px;
}
QSplitter::handle:hover {
    background-color: #d5dce8;
}

/* ---- Footer = status bar surface ------------------------- */
QWidget#modernFooter {
    background-color: #f5f7fb;
    border: 1px solid #d5dce8;
    border-radius: 8px;
}
QWidget#modernFooter QLabel {
    color: #5b6675;
    font-weight: 500;
    padding: 0 2px;
}
QLabel#footerSep {
    color: #c3cee0;
    font-weight: 400;
}
QLabel#footerStatus {
    color: #1b5e20;
    font-weight: 700;
}

/* ---- Inline section header labels (objectName below) ----- */
QLabel#sectionLabel {
    color: #5b6675;
    font-weight: 700;
    padding-top: 2px;
}'''

_FLUENT_APP_QSS_DARK = r'''/* ============================================================
   MODERN WORKSPACE CHROME — DARK (theme.is_dark() == True)
   Fluent / Windows 11. App-level: QApplication.setStyleSheet.
   Reaches the MAIN WINDOW *and every dialog/window*.
   CHROME (verbatim) + DIALOG-WIDGET coverage appended below.
     page #171a21  panel #20242c  surface-2 #252b35
     border #465263  hairline #3a4350  text #f1f5f9  muted #aab4c2
     accent #4f9cf9 / #7ab8ff  input-bottom #5b6a80
     hover #2c3440  pressed #242a33  selected #314b6f
   Lint-clean: no box-shadow/text-shadow/transition/animation/
   transform/opacity/calc/CSS-var/::before/::after/::placeholder/
   8-digit-hex/text-transform. Caret = Qt border-triangle idiom.
   NO bare 'QWidget{background}' / '*{}' — pages backgrounded via
   QDialog/QMainWindow/object-name ONLY, so custom-painted and
   delegate-painted widgets are never nuked.
   ============================================================ */

QWidget#mainCentral {
    background-color: #171a21;
    color: #f1f5f9;
}

/* ============================================================
   PAGE / WINDOW BACKGROUNDS — dialogs & top-level windows
   Object-name + concrete-class scoped (NEVER bare QWidget).
   ============================================================ */
QMainWindow {
    background-color: #171a21;
}
QDialog {
    background-color: #171a21;
    color: #f1f5f9;
}
QMessageBox {
    background-color: #171a21;
}
QMessageBox QLabel {
    color: #f1f5f9;
}
QInputDialog, QFileDialog, QColorDialog, QFontDialog, QWizard, QWizardPage {
    background-color: #171a21;
    color: #f1f5f9;
}
QDialog QLabel, QMainWindow QLabel {
    color: #f1f5f9;
    background: transparent;
}
QToolTip {
    background-color: #252b35;
    color: #f1f5f9;
    border: 1px solid #465263;
    border-radius: 6px;
    padding: 4px 8px;
}

/* ---- GroupBox = primary card surface (Fluent 8px) -------- */
QGroupBox {
    background-color: #20242c;
    border: 1px solid #465263;
    border-radius: 8px;
    margin-top: 1.3em;
    padding: 12px;
    font-weight: 600;
    color: #f1f5f9;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: 2px;
    padding: 0 6px;
    color: #9fc6f5;
    font-weight: 700;
}

/* ---- Secondary buttons (quiet Fluent, default QPushButton) */
QPushButton {
    background-color: #252b35;
    border: 1px solid #465263;
    border-radius: 6px;
    padding: 7px 14px;
    min-height: 18px;
    color: #f1f5f9;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #2c3440;
    border-color: #566378;
}
QPushButton:pressed {
    background-color: #242a33;
    border-color: #5b6a80;
    color: #dfe7f1;
}
QPushButton:checked, QPushButton:on {
    background-color: #314b6f;
    border-color: #7ab8ff;
    color: #f1f5f9;
}
QPushButton:focus {
    border: 1px solid #7ab8ff;
}
QPushButton:disabled {
    background-color: #1f242d;
    border-color: #353d49;
    color: #6b7585;
}

/* ---- Primary CTA: bright accent gradient (near-black, AA) - */
QPushButton#primaryRunButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #7ab8ff, stop:1 #4f9cf9);
    border: 1px solid #4f9cf9;
    border-radius: 6px;
    padding: 8px 18px;
    color: #06203f;
    font-weight: 700;
}
QPushButton#primaryRunButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #8fc4ff, stop:1 #5fa6fa);
    border-color: #5fa6fa;
}
QPushButton#primaryRunButton:pressed {
    background-color: #3f86db;
    border-color: #4f9cf9;
}
QPushButton#primaryRunButton:focus {
    border: 2px solid #9accff;
}
QPushButton#primaryRunButton:disabled {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a5a7f, stop:1 #335073);
    border-color: #335073;
    color: #8fb4dd;
}

/* ---- Line edits — Fluent bottom-border accent on focus --- */
QLineEdit {
    background-color: #252b35;
    border: 1px solid #465263;
    border-bottom: 1px solid #5b6a80;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f1f5f9;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
}
QLineEdit:hover {
    border-color: #566378;
    border-bottom-color: #6b7c93;
}
QLineEdit:focus {
    border: 1px solid #7ab8ff;
    border-bottom: 2px solid #4f9cf9;
    padding-bottom: 5px;
    background-color: #252b35;
}
QLineEdit:disabled {
    background-color: #1f242c;
    color: #6b7585;
    border-color: #353d49;
    border-bottom-color: #353d49;
}
QLineEdit:read-only {
    background-color: #20242c;
    color: #aab4c2;
}

/* ---- Combo boxes (incl. editable line edit + popup) ------ */
QComboBox {
    background-color: #252b35;
    border: 1px solid #465263;
    border-bottom: 1px solid #5b6a80;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f1f5f9;
    min-height: 18px;
}
QComboBox:hover {
    background-color: #2c3440;
    border-color: #566378;
    border-bottom-color: #6b7c93;
}
QComboBox:focus, QComboBox:on {
    border: 1px solid #7ab8ff;
    border-bottom: 2px solid #4f9cf9;
    padding-bottom: 5px;
}
QComboBox:disabled {
    background-color: #1f242c;
    color: #6b7585;
    border-color: #353d49;
    border-bottom-color: #353d49;
}
QComboBox:editable {
    background-color: #252b35;
}
QComboBox QLineEdit {
    border: none;
    border-radius: 0;
    padding: 0;
    background: transparent;
    color: #f1f5f9;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid #3a4350;
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
    border-top: 5px solid #aab4c2;
    margin-top: 1px;
    margin-right: 7px;
    margin-left: 7px;
}
QComboBox::down-arrow:disabled {
    border-top-color: #6b7585;
}
QComboBox QAbstractItemView {
    background-color: #252b35;
    border: 1px solid #465263;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    color: #f1f5f9;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
}

/* ============================================================
   SPIN BOXES — QSpinBox / QDoubleSpinBox (Fluent inputs)
   ============================================================ */
QSpinBox, QDoubleSpinBox {
    background-color: #252b35;
    border: 1px solid #465263;
    border-bottom: 1px solid #5b6a80;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f1f5f9;
    min-height: 18px;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
}
QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: #566378;
    border-bottom-color: #6b7c93;
}
QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #7ab8ff;
    border-bottom: 2px solid #4f9cf9;
    padding-bottom: 5px;
}
QSpinBox:disabled, QDoubleSpinBox:disabled {
    background-color: #1f242c;
    color: #6b7585;
    border-color: #353d49;
    border-bottom-color: #353d49;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid #3a4350;
    border-top-right-radius: 6px;
    background-color: #2c3440;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border-left: 1px solid #3a4350;
    border-bottom-right-radius: 6px;
    background-color: #2c3440;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #354050;
}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
    background-color: #242a33;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #aab4c2;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #aab4c2;
}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {
    border-bottom-color: #6b7585;
}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {
    border-top-color: #6b7585;
}

/* ============================================================
   MULTILINE EDITORS — QPlainTextEdit / QTextEdit / QTextBrowser
   (terminal browser keeps its own per-widget sheet; overrides.)
   ============================================================ */
QPlainTextEdit, QTextEdit, QTextBrowser {
    background-color: #20242c;
    border: 1px solid #465263;
    border-radius: 6px;
    padding: 4px;
    color: #f1f5f9;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
}
QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus {
    border: 1px solid #7ab8ff;
}
QPlainTextEdit:disabled, QTextEdit:disabled, QTextBrowser:disabled {
    background-color: #1f242c;
    color: #6b7585;
    border-color: #353d49;
}

/* ============================================================
   TAB WIDGET / TAB BAR — selected = card, unselected = surface-2
   ============================================================ */
QTabWidget::pane {
    background-color: #20242c;
    border: 1px solid #465263;
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
    background-color: #252b35;
    border: 1px solid #465263;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 7px 16px;
    margin-right: 2px;
    color: #aab4c2;
    font-weight: 600;
}
QTabBar::tab:hover {
    background-color: #2c3440;
    color: #f1f5f9;
}
QTabBar::tab:selected {
    background-color: #20242c;
    border-color: #465263;
    color: #9fc6f5;
    border-top: 2px solid #4f9cf9;
    padding-top: 6px;
}
QTabBar::tab:!selected {
    margin-top: 2px;
}
QTabBar::tab:first {
    margin-left: 0;
}
QTabBar::tab:disabled {
    color: #6b7585;
}
QTabBar::tab:left, QTabBar::tab:right {
    border: 1px solid #465263;
    border-radius: 0;
    padding: 12px 8px;
}

/* ============================================================
   HEADER VIEW — table/tree headers
   ============================================================ */
QHeaderView {
    background-color: #252b35;
    border: none;
}
QHeaderView::section {
    background-color: #252b35;
    color: #aab4c2;
    padding: 6px 10px;
    border: none;
    border-right: 1px solid #3a4350;
    border-bottom: 1px solid #465263;
    font-weight: 700;
}
QHeaderView::section:horizontal {
    border-right: 1px solid #3a4350;
}
QHeaderView::section:vertical {
    border-right: 1px solid #465263;
    border-bottom: 1px solid #3a4350;
    text-align: left;
}
QHeaderView::section:hover {
    background-color: #2c3440;
    color: #f1f5f9;
}
QHeaderView::section:pressed {
    background-color: #242a33;
}
QHeaderView::section:checked {
    background-color: #314b6f;
    color: #9fc6f5;
}
QHeaderView::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #aab4c2;
    subcontrol-position: center right;
    margin-right: 6px;
}
QHeaderView::up-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #aab4c2;
    subcontrol-position: center right;
    margin-right: 6px;
}

/* ============================================================
   TABLE VIEW / TABLE WIDGET — gridlines, selection, corner
   ============================================================ */
QTableView, QTableWidget {
    background-color: #20242c;
    alternate-background-color: #252b35;
    border: 1px solid #465263;
    border-radius: 6px;
    gridline-color: #3a4350;
    color: #f1f5f9;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
    outline: 0;
}
QTableView::item, QTableWidget::item {
    padding: 4px 6px;
    border: none;
}
QTableView::item:hover, QTableWidget::item:hover {
    background-color: #2c3440;
}
QTableView::item:selected, QTableWidget::item:selected {
    background-color: #314b6f;
    color: #f1f5f9;
}
QTableView::item:focus, QTableWidget::item:focus {
    background-color: #314b6f;
}
QTableCornerButton::section {
    background-color: #252b35;
    border: none;
    border-right: 1px solid #465263;
    border-bottom: 1px solid #465263;
}

/* ============================================================
   TREE / LIST VIEWS — items, selection, hover, branches
   Generic QListWidget kept CONSERVATIVE so the main window's
   delegate-painted #procedureCards / #equipmentCards override.
   ============================================================ */
QTreeView, QTreeWidget {
    background-color: #20242c;
    alternate-background-color: #252b35;
    border: 1px solid #465263;
    border-radius: 6px;
    color: #f1f5f9;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
    outline: 0;
}
QTreeView::item, QTreeWidget::item {
    padding: 4px 2px;
    border: none;
}
QTreeView::item:hover, QTreeWidget::item:hover {
    background-color: #2c3440;
}
QTreeView::item:selected, QTreeWidget::item:selected {
    background-color: #314b6f;
    color: #f1f5f9;
}
QTreeView::branch:hover {
    background-color: #2c3440;
}
QTreeView::branch:selected, QTreeWidget::branch:selected {
    background-color: #314b6f;
}

QListView, QListWidget {
    background-color: #20242c;
    border: 1px solid #465263;
    border-radius: 6px;
    color: #f1f5f9;
    selection-background-color: #314b6f;
    selection-color: #f1f5f9;
    outline: 0;
}
QListView::item, QListWidget::item {
    padding: 5px 8px;
    border: 1px solid transparent;
    border-radius: 4px;
}
QListView::item:hover, QListWidget::item:hover {
    background-color: #2c3440;
    border-color: #3a4350;
}
QListView::item:selected, QListWidget::item:selected {
    background-color: #314b6f;
    border-color: #566378;
    color: #f1f5f9;
}

/* ============================================================
   CHECKBOX — indicator drawn purely with border+bg (no asset)
   ============================================================ */
QCheckBox {
    color: #f1f5f9;
    spacing: 8px;
    background: transparent;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #5b6a80;
    border-radius: 4px;
    background-color: #252b35;
}
QCheckBox::indicator:hover {
    border-color: #7ab8ff;
    background-color: #2c3440;
}
QCheckBox::indicator:checked {
    border-color: #4f9cf9;
    background-color: #4f9cf9;
    image: none;
}
QCheckBox::indicator:checked:hover {
    border-color: #7ab8ff;
    background-color: #7ab8ff;
}
QCheckBox::indicator:indeterminate {
    border-color: #4f9cf9;
    background-color: #3f86db;
}
QCheckBox::indicator:disabled {
    border-color: #353d49;
    background-color: #1f242d;
}
QCheckBox::indicator:checked:disabled {
    border-color: #335073;
    background-color: #335073;
}
QCheckBox:disabled {
    color: #6b7585;
}

/* ============================================================
   RADIO BUTTON — circular indicator (border+bg, no asset)
   ============================================================ */
QRadioButton {
    color: #f1f5f9;
    spacing: 8px;
    background: transparent;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #5b6a80;
    border-radius: 9px;
    background-color: #252b35;
}
QRadioButton::indicator:hover {
    border-color: #7ab8ff;
    background-color: #2c3440;
}
QRadioButton::indicator:checked {
    border: 4px solid #4f9cf9;
    background-color: #171a21;
}
QRadioButton::indicator:checked:hover {
    border-color: #7ab8ff;
}
QRadioButton::indicator:disabled {
    border-color: #353d49;
    background-color: #1f242d;
}
QRadioButton::indicator:checked:disabled {
    border: 4px solid #335073;
    background-color: #171a21;
}
QRadioButton:disabled {
    color: #6b7585;
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
    color: #f1f5f9;
    font-weight: 600;
}
QToolButton:hover {
    background-color: #2c3440;
    border-color: #566378;
}
QToolButton:pressed {
    background-color: #242a33;
    border-color: #5b6a80;
}
QToolButton:checked, QToolButton:on {
    background-color: #314b6f;
    border-color: #7ab8ff;
}
QToolButton:disabled {
    color: #6b7585;
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
    background-color: #171a21;
    color: #f1f5f9;
    border-bottom: 1px solid #465263;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}
QMenuBar::item:selected {
    background-color: #2c3440;
    color: #f1f5f9;
}
QMenuBar::item:pressed {
    background-color: #242a33;
}
QMenu {
    background-color: #20242c;
    border: 1px solid #465263;
    border-radius: 8px;
    padding: 4px;
    color: #f1f5f9;
}
QMenu::item {
    background: transparent;
    padding: 6px 24px 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #314b6f;
    color: #f1f5f9;
}
QMenu::item:disabled {
    color: #6b7585;
}
QMenu::separator {
    height: 1px;
    background-color: #3a4350;
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
    background-color: #252b35;
    border: 1px solid #465263;
    border-radius: 6px;
    text-align: center;
    color: #f1f5f9;
    min-height: 16px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4f9cf9, stop:1 #7ab8ff);
    border-radius: 5px;
    margin: 1px;
}

/* ============================================================
   STATUS BAR (generic, app-wide)
   ============================================================ */
QStatusBar {
    background-color: #20242c;
    color: #aab4c2;
    border-top: 1px solid #465263;
}
QStatusBar::item {
    border: none;
}
QStatusBar QLabel {
    color: #aab4c2;
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
QAbstractScrollArea {
    background: transparent;
}

/* ============================================================
   SLIDER (optional)
   ============================================================ */
QSlider::groove:horizontal {
    height: 4px;
    background-color: #465263;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background-color: #4f9cf9;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background-color: #20242c;
    border: 1px solid #4f9cf9;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    background-color: #2c3440;
}
QSlider::groove:vertical {
    width: 4px;
    background-color: #465263;
    border-radius: 2px;
}
QSlider::sub-page:vertical {
    background-color: #4f9cf9;
    border-radius: 2px;
}
QSlider::handle:vertical {
    background-color: #20242c;
    border: 1px solid #4f9cf9;
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
    background-color: #3f4856;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background-color: #4d5868;
}
QScrollBar::handle:vertical:pressed {
    background-color: #5b6678;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background-color: #3f4856;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #4d5868;
}
QScrollBar::handle:horizontal:pressed {
    background-color: #5b6678;
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
    background-color: #171a21;
}
QSplitter::handle:horizontal {
    width: 10px;
}
QSplitter::handle:vertical {
    height: 10px;
}
QSplitter::handle:hover {
    background-color: #465263;
}

/* ---- Footer = status bar surface ------------------------- */
QWidget#modernFooter {
    background-color: #20242c;
    border: 1px solid #3a4350;
    border-radius: 8px;
}
QWidget#modernFooter QLabel {
    color: #aab4c2;
    font-weight: 500;
    padding: 0 2px;
}
QLabel#footerSep {
    color: #4d5868;
    font-weight: 400;
}
QLabel#footerStatus {
    color: #81c784;
    font-weight: 700;
}

/* ---- Inline section header labels (objectName below) ----- */
QLabel#sectionLabel {
    color: #aab4c2;
    font-weight: 700;
    padding-top: 2px;
}'''


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
