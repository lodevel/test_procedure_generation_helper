"""Unit tests for the shared work-timer formatter (the pure, headless part).

The QObject ticker needs a running Qt event loop to exercise; the formatter is a
pure function and carries the formatting contract both chat surfaces rely on.
"""
from workflow_editor.widgets.work_timer import format_elapsed


def test_format_elapsed_sub_minute():
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(900) == "0:00"      # <1 s rounds down
    assert format_elapsed(7000) == "0:07"
    assert format_elapsed(59000) == "0:59"


def test_format_elapsed_minutes():
    assert format_elapsed(60000) == "1:00"
    assert format_elapsed(83000) == "1:23"
    assert format_elapsed(600000) == "10:00"


def test_format_elapsed_hours():
    assert format_elapsed(3600000) == "1:00:00"
    assert format_elapsed(3725000) == "1:02:05"


def test_format_elapsed_negative_clamps():
    assert format_elapsed(-5) == "0:00"
