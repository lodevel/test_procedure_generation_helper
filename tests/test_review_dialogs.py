"""Procedo Studio's diff window and message details, as on the approved boards (S7, Small's
"Message details"; redesign slice 7b): the diff window keeps a failed proposal's validator banner
in view, shows the Original | Proposed panes with their changed lines and the unified diff, counts
+a −r from the proposal's own diff, and decides "accept" / "reject" / "" as before; each file of a
proposal comes in turn; the message details show who sent it, PROMPT and RESPONSE with JSON
pretty-printed, Copy and Close."""

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPlainTextEdit, QPushButton

from workflow_editor import theme
from workflow_editor.core.proposal import FileChange, Proposal, changed_lines, diff_lines
from workflow_editor.dialogs.diff_viewer import DiffViewer, _diff_marks
from workflow_editor.widgets.editor_card import tint_lines
from workflow_editor.dock.chat_messages import MessageDetailDialog, review_proposal
from workflow_editor.llm.tab_context import ChatMessage

from .test_main_window_characterization import qapp as qapp

ORIGINAL = "a\nb\nc\n"
PROPOSED = "a\nB\nc\nd\ne\nf\n"
BANNER = "Proposal failed validation — review and fix before applying:\nStep 1: no verb"


def _tinted(editor):
    """The 1-based lines the editor draws tinted."""
    document = editor.document()
    return sorted({document.findBlock(s.cursor.selectionStart()).blockNumber() + 1
                   for s in editor.extraSelections()})


def _banner(dialog):
    return dialog.findChild(QFrame, "validatorBanner")


def test_changed_lines_numbers_each_side_and_reads_a_removed_rule_as_a_line():
    assert changed_lines(diff_lines(ORIGINAL, PROPOSED)) == ([2], [2, 4, 5, 6])
    assert changed_lines(diff_lines("x\n---\ny\n", "x\ny\n")) == ([2], [])  # "----" is a removed "---"


def test_a_failed_validation_keeps_its_banner_in_view(qapp):
    dialog = DiffViewer(ORIGINAL, PROPOSED, "procedure_text.md", banner=BANNER)
    banner = _banner(dialog)
    assert banner is not None and banner.isVisibleTo(dialog)
    headline = banner.findChild(QLabel)
    assert headline.text() == "Proposal failed validation — review and fix before applying:"
    assert headline.font().bold()
    assert dialog.banner_findings.toPlainText() == "Step 1: no verb"
    assert dialog.banner_findings.isReadOnly()
    dialog.show()
    QApplication.processEvents()
    assert dialog.banner_findings.verticalScrollBar().maximum() == 0  # one finding: no scrolling
    dialog.close()
    assert banner.styleSheet() == ""  # its colours are the app stylesheet's
    assert _banner(DiffViewer(ORIGINAL, PROPOSED, "procedure_text.md")) is None


def test_every_finding_is_reachable_at_the_minimum_size(qapp):
    findings = [f"Step {n}: finding number {n}" for n in range(1, 31)]
    dialog = DiffViewer(ORIGINAL, PROPOSED, "procedure_text.md",
                        banner="Proposal failed validation:\n" + "\n".join(findings))
    dialog.resize(dialog.minimumSize())
    dialog.show()
    QApplication.processEvents()
    view = dialog.banner_findings
    assert view.toPlainText().splitlines() == findings  # all of them, none cut
    assert view.verticalScrollBar().maximum() > 0  # they scroll
    assert view.height() < 8 * view.fontMetrics().lineSpacing()  # within its few rows
    assert _banner(dialog).findChild(QLabel).isVisible()  # the headline stays in view
    assert dialog.accept_btn.isVisible() and dialog.reject_btn.isVisible()
    dialog.close()


def test_enter_never_accepts_nor_rejects(qapp):
    dialog = DiffViewer(ORIGINAL, PROPOSED, "procedure_text.md", banner=BANNER)
    dialog.show()
    QApplication.processEvents()
    assert not any(b.isDefault() or b.autoDefault() for b in dialog.findChildren(QPushButton))
    for target in (dialog, dialog.accept_btn, dialog.reject_btn, dialog.proposed_view):
        target.setFocus()
        QTest.keyClick(target, Qt.Key.Key_Return)
        QTest.keyClick(target, Qt.Key.Key_Enter)
        assert dialog.isVisible() and dialog.decision == ""
    dialog.close()


def test_a_changed_line_that_looks_like_a_header_is_a_change(qapp):
    diff = diff_lines("a\n---\n", "a\n++x\n")
    assert diff[4:] == ["----", "+++x"]
    marks = _diff_marks(diff)
    assert marks[5].background().color() == theme.diff_removed_bg()
    assert marks[6].background().color() == theme.diff_added_bg()
    assert all(marks[n].foreground().color() == theme.diff_header_color() for n in (1, 2, 3))
    proposal = Proposal([FileChange("procedure_text.md", lambda: "a\n---\n", "a\n")],
                        lambda: None, lambda: None)
    assert proposal.counts() == (0, 1) and proposal.preview() == ["− ---"]


def test_the_last_line_is_tinted_across_every_row_without_a_final_newline(qapp):
    editor = QPlainTextEdit()
    editor.resize(220, 200)
    editor.setPlainText("short\n" + "word " * 20)  # the last line wraps; no line break after it
    editor.show()
    QApplication.processEvents()
    red = QTextCharFormat()
    red.setBackground(QColor("#ff0000"))
    tint_lines(editor, {2: red})
    QApplication.processEvents()
    image = editor.viewport().grab().toImage()
    block = editor.document().lastBlock()
    top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
    layout = block.layout()
    assert layout.lineCount() > 1
    x = image.width() - 30  # right of every row's text
    for row in range(layout.lineCount()):
        line = layout.lineAt(row)
        pixel = image.pixelColor(x, round(top + line.y() + line.height() / 2))
        assert (pixel.red(), pixel.green()) == (255, 0), row
    first = image.pixelColor(x, round(editor.blockBoundingGeometry(editor.document().firstBlock())
                                      .translated(editor.contentOffset()).center().y()))
    assert first.green() > 200  # the line not marked is left as it is
    editor.close()


def test_the_panes_tint_the_changed_lines_and_the_footer_counts_them(qapp):
    dialog = DiffViewer(ORIGINAL, PROPOSED, "procedure_text.md")
    assert dialog.original_view.toPlainText() == ORIGINAL
    assert dialog.proposed_view.toPlainText() == PROPOSED
    assert _tinted(dialog.original_view) == [2]
    assert _tinted(dialog.proposed_view) == [2, 4, 5, 6]
    assert dialog.diff_view.toPlainText().splitlines() == diff_lines(ORIGINAL, PROPOSED)
    note = dialog.findChild(QLabel, "dialogNote").text()
    assert ">+4</b> lines" in note and ">−1</b> lines" in note
    subtitle = dialog.findChild(QLabel, "dialogSubtitle").text()
    assert subtitle == "procedure_text.md · nothing is written until you accept"
    assert dialog.accept_btn.text() == "Accept changes"
    assert dialog.accept_btn.objectName() == "primaryButton"  # drawn primary, not Enter's
    assert dialog.reject_btn.property("danger") is True


def test_review_returns_accept_reject_or_nothing(qapp, monkeypatch):
    for button, expected in (("accept_btn", "accept"), ("reject_btn", "reject"), ("", "")):
        monkeypatch.setattr(DiffViewer, "exec",
                            lambda dialog, b=button: b and getattr(dialog, b).click())
        assert DiffViewer.review(ORIGINAL, PROPOSED, "procedure_text.md") == expected


def test_each_file_of_a_proposal_comes_in_turn_and_the_last_accept_applies_it(qapp, monkeypatch):
    log, shown = [], []
    proposal = Proposal([FileChange("procedure.json", lambda: "{}\n", '{"a": 1}\n'),
                         FileChange("test.py", lambda: "x\n", "y\n")],
                        lambda: log.append("applied"), lambda: log.append("rejected"))

    def fake_exec(dialog):
        shown.append(dialog.findChild(QLabel, "dialogSubtitle").text())
        assert log == []  # nothing applied before the last file's Accept
        dialog.accept_btn.click()
    monkeypatch.setattr(DiffViewer, "exec", fake_exec)
    review_proposal(proposal)
    assert shown == ["procedure.json · file 1 of 2 · nothing is written until you accept",
                     "test.py · file 2 of 2 · nothing is written until you accept"]
    assert log == ["applied"]


def test_message_details_show_the_sender_and_both_sections_json_pretty(qapp):
    prompt = json.dumps({"messages": json.dumps([{"role": "user", "content": "hi"}])})
    message = ChatMessage("assistant", "Done.", full_prompt=prompt, full_response="All good.",
                          total_tokens=18646)
    dialog = MessageDetailDialog(message)
    assert dialog.findChild(QLabel, "dialogTitle").text() == "Message details"
    assert dialog.findChild(QLabel, "dialogSubtitle").text() == "Assistant reply · 18,646 tokens"
    lines = dialog.text.toPlainText().splitlines()
    assert lines[1] == "PROMPT" and "RESPONSE" in lines and lines[-1] == "All good."
    assert '      "role": "user",' in lines  # the nested JSON string expanded, indented
    buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
    copy, close = buttons["Copy"], buttons["Close"]
    assert close.objectName() == "primaryButton" and close.isDefault()
    copy.click()
    assert QApplication.clipboard().text() == dialog.text.toPlainText()


def test_message_details_without_a_prompt_say_so(qapp):
    dialog = MessageDetailDialog(ChatMessage("user", "hello"))
    assert dialog.findChild(QLabel, "dialogSubtitle").text() == "Your message"
    assert dialog.text.toPlainText() == "(No prompt recorded)\n\n(No response recorded)"
