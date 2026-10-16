"""Studio wears Bench's theme; Studio's own colours follow Bench's light / dark state, and none of
them repeats a colour Bench already has."""

from __future__ import annotations

import inspect

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from procedo.bench import fluent_qss
from procedo.bench import theme as bench_theme
from procedo.studio import theme


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

    assert bench_theme.is_dark() is dark
    assert theme.status_warning() == ("#ffa726" if dark else "orange")
    assert theme.muted_color() == app.palette().color(QPalette.ColorRole.PlaceholderText).name()


def _colour(value) -> str:
    """A colour as one comparable string: #rrggbb when Qt reads it, else as given."""
    colour = value if isinstance(value, QColor) else QColor(str(value))
    return colour.name() if colour.isValid() else str(value)


def _colour_helpers(module) -> dict:
    """The module's own public helpers that take no argument (its colours, among others)."""
    return {name: f for name, f in vars(module).items()
            if inspect.isfunction(f) and f.__module__ == module.__name__
            and not name.startswith("_")
            and all(p.default is not p.empty for p in inspect.signature(f).parameters.values())}


def _bench_colours() -> dict:
    """Bench's colours by name: its theme's helpers, its stylesheet's tokens."""
    colours = {f"theme.{name}": f() for name, f in _colour_helpers(bench_theme).items()}
    colours.update({f"token {key}": value
                    for key, value in fluent_qss.tokens(bench_theme.is_dark()).items()})
    return {name: _colour(value) for name, value in colours.items()
            if isinstance(value, (str, QColor))}


def test_no_studio_colour_repeats_one_of_benchs_in_both_themes(app):
    """A colour both apps share is Bench's, called where it is used; Studio's theme keeps only
    its own (a helper equal to one of Bench's in light AND dark is a copy)."""
    seen = {}
    for theme_id in ("original", "dark"):
        bench_theme.apply_app_theme(theme_id, app)
        bench = _bench_colours()
        seen[theme_id] = {name: {b for b, v in bench.items() if v == _colour(f())}
                          for name, f in _colour_helpers(theme).items()}
    assert _colour_helpers(theme)  # not vacuous
    copies = {name: both for name in seen["original"]
              if (both := seen["original"][name] & seen["dark"][name])}
    assert copies == {}
