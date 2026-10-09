"""The review diff compares lines: a final newline on one side only is no change."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from workflow_editor.dialogs import DiffViewer  # noqa: E402


def test_only_the_changed_line_shows_against_a_stripped_original():
    QApplication.instance() or QApplication([])
    old = "def main():\n    return 0\n\nmain()"            # callers strip the base
    new = "def main():\n    return 1\n\nmain()\n"
    diff = DiffViewer(old, new)._generate_diff().splitlines()
    assert diff[:2] == ["--- Original", "+++ Proposed"]
    assert [ln for ln in diff[2:] if ln[:1] in "+-"] == ["-    return 0", "+    return 1"]


def test_identical_lines_need_no_review(monkeypatch):
    monkeypatch.setattr(DiffViewer, "exec", lambda self: pytest.fail("review opened"))
    assert DiffViewer.show_diff("x\nmain()", "x\nmain()\n") == (True, "x\nmain()\n")
