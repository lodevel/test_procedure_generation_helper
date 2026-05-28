"""Tests for the reusable FindReplaceBar widget.

Pure-logic tests (replace-all string transform, regex semantics) plus
a handful of integration tests that drive a real QPlainTextEdit
through the bar and check find/replace state.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from workflow_editor.widgets.find_replace_bar import FindReplaceBar


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def bar_and_editor(qapp):
    """A FindReplaceBar bound to a fresh QPlainTextEdit. Returns
    (bar, editor) so tests can drive both sides."""
    editor = QPlainTextEdit()
    bar = FindReplaceBar()
    bar.set_target(editor)
    return bar, editor


# ---------------------------------------------------------------------------
# Pure-logic: _replace_all_in_string
# ---------------------------------------------------------------------------


def test_replace_all_literal_case_insensitive_by_default(bar_and_editor):
    bar, _ = bar_and_editor
    out, n = bar._replace_all_in_string("Foo foo FOO", "foo", "bar")
    assert out == "bar bar bar"
    assert n == 3


def test_replace_all_literal_case_sensitive_when_toggled(bar_and_editor):
    bar, _ = bar_and_editor
    bar.case_toggle.setChecked(True)
    out, n = bar._replace_all_in_string("Foo foo FOO", "foo", "bar")
    assert out == "Foo bar FOO"
    assert n == 1


def test_replace_all_whole_word_only_matches_word_boundaries(bar_and_editor):
    bar, _ = bar_and_editor
    bar.word_toggle.setChecked(True)
    out, n = bar._replace_all_in_string("foo foobar barfoo", "foo", "X")
    assert out == "X foobar barfoo"
    assert n == 1


def test_replace_all_regex_pattern(bar_and_editor):
    bar, _ = bar_and_editor
    bar.regex_toggle.setChecked(True)
    out, n = bar._replace_all_in_string("a1 b22 c333", r"\d+", "N")
    assert out == "aN bN cN"
    assert n == 3


def test_replace_all_regex_with_backreference(bar_and_editor):
    """Sanity: regex replacement supports backreferences via re.subn."""
    bar, _ = bar_and_editor
    bar.regex_toggle.setChecked(True)
    bar.case_toggle.setChecked(True)
    out, n = bar._replace_all_in_string(
        "key=value other=stuff", r"(\w+)=(\w+)", r"\2=\1",
    )
    assert out == "value=key stuff=other"
    assert n == 2


def test_replace_all_no_matches_returns_zero(bar_and_editor):
    bar, _ = bar_and_editor
    out, n = bar._replace_all_in_string("hello world", "xyz", "Q")
    assert out == "hello world"
    assert n == 0


def test_replace_all_escapes_literal_special_chars(bar_and_editor):
    """Literal mode treats `.` as a dot, not 'any char' — the
    regex-escape happens inside _replace_all_in_string."""
    bar, _ = bar_and_editor
    out, n = bar._replace_all_in_string("a.b axb", ".", "_")
    assert out == "a_b axb"
    assert n == 1


# ---------------------------------------------------------------------------
# Integration: find_next / find_prev wrap-around
# ---------------------------------------------------------------------------


def test_find_next_moves_cursor_to_match(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("alpha beta gamma alpha")
    bar.find_field.setText("alpha")
    assert bar.find_next() is True
    sel = editor.textCursor().selectedText()
    assert sel == "alpha"
    # Cursor sits at the first match (position 0..5).
    assert editor.textCursor().selectionStart() == 0


def test_find_next_wraps_at_end(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("alpha beta alpha")
    bar.find_field.setText("alpha")
    bar.find_next()  # first match at 0
    bar.find_next()  # second match at 11
    bar.find_next()  # wraps → back to 0
    assert editor.textCursor().selectionStart() == 0
    assert bar.status_label.text() == "Wrapped"


def test_find_prev_walks_backward(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("alpha beta alpha")
    bar.find_field.setText("alpha")
    # Park cursor at end so prev finds the second alpha first.
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.End)
    editor.setTextCursor(cursor)
    bar.find_prev()
    assert editor.textCursor().selectionStart() == 11


def test_find_with_no_match_reports_no_matches(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("alpha beta")
    bar.find_field.setText("xyz")
    assert bar.find_next() is False
    assert bar.status_label.text() == "No matches"


# ---------------------------------------------------------------------------
# Integration: replace_one advances after replacing
# ---------------------------------------------------------------------------


def test_replace_one_replaces_selection_then_finds_next(bar_and_editor):
    """First find selects; then Replace replaces THAT match and
    advances to the next. Operator-friendly: a sequence of Replace
    clicks walks through matches one at a time."""
    bar, editor = bar_and_editor
    editor.setPlainText("foo bar foo bar")
    bar.find_field.setText("foo")
    bar.replace_field.setText("X")
    bar.find_next()                  # selects first "foo"
    bar.replace_one()                # replaces it + advances
    assert editor.toPlainText() == "X bar foo bar"
    assert editor.textCursor().selectedText() == "foo"


def test_replace_one_without_selection_just_finds(bar_and_editor):
    """No selection yet → Replace acts as Find Next, doesn't
    blindly replace at the cursor position."""
    bar, editor = bar_and_editor
    editor.setPlainText("foo bar")
    bar.find_field.setText("foo")
    bar.replace_field.setText("X")
    bar.replace_one()
    assert editor.toPlainText() == "foo bar"  # unchanged
    assert editor.textCursor().selectedText() == "foo"


def test_replace_one_skips_non_matching_selection(bar_and_editor):
    """If the user manually selected something that is NOT a match
    of the find pattern, Replace must not clobber it. _selection_
    matches_pattern gates this."""
    bar, editor = bar_and_editor
    editor.setPlainText("foo bar baz")
    # Manually select "bar" then ask to replace "foo" → must skip.
    cursor = editor.textCursor()
    cursor.setPosition(4)
    cursor.setPosition(7, QTextCursor.KeepAnchor)
    editor.setTextCursor(cursor)
    bar.find_field.setText("foo")
    bar.replace_field.setText("X")
    bar.replace_one()
    assert editor.toPlainText() == "foo bar baz"  # bar untouched
    # Cursor advanced to the next "foo" via the chained find_next —
    # which wraps back to position 0.
    assert editor.textCursor().selectedText() == "foo"


# ---------------------------------------------------------------------------
# Integration: replace_all uses one undo unit
# ---------------------------------------------------------------------------


def test_replace_all_replaces_every_match(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("foo bar foo baz foo")
    bar.find_field.setText("foo")
    bar.replace_field.setText("X")
    bar.replace_all()
    assert editor.toPlainText() == "X bar X baz X"
    assert bar.status_label.text() == "3 replaced"


def test_replace_all_with_no_matches_reports(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("hello world")
    bar.find_field.setText("xyz")
    bar.replace_field.setText("Q")
    bar.replace_all()
    assert editor.toPlainText() == "hello world"
    assert bar.status_label.text() == "No matches"


def test_replace_all_is_single_undo_unit(bar_and_editor):
    bar, editor = bar_and_editor
    editor.setPlainText("foo foo foo")
    bar.find_field.setText("foo")
    bar.replace_field.setText("X")
    bar.replace_all()
    assert editor.toPlainText() == "X X X"
    # One Undo restores the original — critical guarantee.
    editor.undo()
    assert editor.toPlainText() == "foo foo foo"


# ---------------------------------------------------------------------------
# Bar visibility / focus
# ---------------------------------------------------------------------------


def test_show_find_hides_replace_row(bar_and_editor):
    bar, _ = bar_and_editor
    bar.show_find()
    assert bar.isVisible()
    assert not bar.replace_field.isVisible()


def test_show_replace_shows_replace_row(bar_and_editor):
    bar, _ = bar_and_editor
    bar.show_replace()
    assert bar.isVisible()
    assert bar.replace_field.isVisible()


def test_close_bar_hides_and_clears_status(bar_and_editor):
    bar, _ = bar_and_editor
    bar.show_find()
    bar.status_label.setText("something")
    bar.close_bar()
    assert not bar.isVisible()
    assert bar.status_label.text() == ""


def test_install_shortcuts_registers_editors_on_bar(qapp):
    """install_find_shortcuts stores the editor list on the bar so
    menu-driven invocations (Edit → Find) get focus-vs-leftmost
    target picking too — not just the keyboard shortcut path."""
    from workflow_editor.widgets.find_replace_bar import install_find_shortcuts
    from PySide6.QtWidgets import QWidget

    tab = QWidget()
    left = QPlainTextEdit(tab)
    right = QPlainTextEdit(tab)
    bar = FindReplaceBar(tab)
    install_find_shortcuts(tab, [left, right], bar)
    assert bar._editors == [left, right]


def test_show_find_picks_leftmost_when_focus_outside(qapp):
    """No editor in the list has focus → leftmost (editors[0]) wins."""
    from workflow_editor.widgets.find_replace_bar import install_find_shortcuts
    from PySide6.QtWidgets import QWidget

    tab = QWidget()
    left = QPlainTextEdit(tab)
    right = QPlainTextEdit(tab)
    bar = FindReplaceBar(tab)
    install_find_shortcuts(tab, [left, right], bar)
    bar.show_find()  # no target arg — must pick from registered list
    assert bar._target is left


def test_show_find_prefills_find_field_from_selection(bar_and_editor):
    """Quality-of-life: if the user has a short word selected when
    they hit Ctrl+F, pre-fill the Find field with it."""
    bar, editor = bar_and_editor
    editor.setPlainText("hello world")
    cursor = editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(5, QTextCursor.KeepAnchor)
    editor.setTextCursor(cursor)
    bar.show_find()
    assert bar.find_field.text() == "hello"
