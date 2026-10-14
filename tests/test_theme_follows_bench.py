"""Studio wears Bench's theme; Studio's own colours follow Bench's light / dark state."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from test_procedure_gui import theme as bench_theme
from workflow_editor import theme


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    palette, stylesheet = QPalette(app.palette()), app.styleSheet()
    yield app
    bench_theme.apply_app_theme("original", app)  # turns the pride effects off
    app.setPalette(palette)
    app.setStyleSheet(stylesheet)


@pytest.mark.parametrize("theme_id, dark", [("original", False), ("dark", True), ("pride_dark", True)])
def test_studio_colours_follow_benchs_theme(app, theme_id, dark):
    bench_theme.apply_app_theme(theme_id, app)

    assert theme.is_dark() is dark
    assert theme.chat_user_bg() == ("#1b3a1b" if dark else "#e8f5e9")
    assert theme.muted_color() == app.palette().color(QPalette.ColorRole.PlaceholderText).name()
