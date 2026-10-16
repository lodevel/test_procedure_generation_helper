"""Procedo Studio's Assistant column (workflow_editor/dock, redesign slice 5): the rules picker
counts the current view's rules and opens their selection, the messages are bubbles per role
and system lines pills in their tone, the composer's buttons are wired, the context meter and
the validator dot follow, the Findings tab carries the count, the streaming bubble folds its
thought."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

import workflow_editor.dock.dock_widget as dock_mod
from workflow_editor.dock.chat_messages import MessageWidget
from workflow_editor.dock.chat_panel import ChatPanel

from .test_main_window_characterization import (
    _make_project, _open_project, make_window as make_window, mbox as mbox, qapp as qapp,
)


@pytest.fixture
def opened(make_window, tmp_path):
    root = _make_project(tmp_path / "proj")
    rules = root / "rules"
    rules.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (rules / name).write_text(f"# {name}\n", encoding="utf-8")
    w = make_window()
    _open_project(w, root)
    w.project_manager.set_rules_root(rules)
    w.open_test(root / "tests" / "t1")
    w._update_project_rules_indicators()
    return w


@pytest.fixture
def panel(qapp):
    widget = ChatPanel(main_window=None)
    widget.resize(340, 600)
    yield widget
    widget.deleteLater()


def test_the_rules_picker_counts_the_views_rules_and_opens_their_selection(opened, monkeypatch, mbox):
    w = opened
    view = w.view_stack.currentWidget()
    w.task_config_manager.set_selected_rules_for_tab(view.tab_context.tab_id, ["a.md", "c.md"])
    w.dock.refresh_rules_picker()
    assert w.dock.rules_picker.text() == "Rules · 2 of 3 ▾"

    opened_for = []

    class _Selector:
        def __init__(self, tab_id, project_manager, task_config_manager, parent, view):
            opened_for.append((tab_id, view))

        def exec(self):
            return 1

        def get_selected_rules(self):
            return ["b.md"]

    monkeypatch.setattr(dock_mod, "RuleSelectorDialog", _Selector)
    w.dock.rules_picker.click()

    assert opened_for == [(view.tab_context.tab_id, "Text")]  # the view's name, as the switch says it
    assert w.dock.rules_picker.text() == "Rules · 1 of 3 ▾"
    assert [t for t, _ in mbox.information] == ["Rules Updated"]


def test_messages_are_bubbles_per_role_and_system_lines_pills(panel):
    panel.add_message("user", "Review <this>")
    panel.add_message("assistant", "Two remarks.", prompt_tokens=18234, completion_tokens=412,
                      total_tokens=18646)
    panel.add_system_message("✓ Deterministic validator passed.", tone="success")
    user, assistant, system = panel._messages()

    assert (user.objectName(), user.property("role"), user.align) == (
        "chatBubble", "user", Qt.AlignmentFlag.AlignRight)
    assert "&lt;this&gt;" in user.content_label.text()  # typed text shows as typed
    assert (assistant.property("role"), assistant.align) == ("assistant", Qt.AlignmentFlag.AlignLeft)
    tokens = assistant.findChild(type(assistant.content_label), "chatTokens")
    assert tokens.text() == "18,646 tokens · 18,234 prompt + 412 completion"
    assert system.content_label.objectName() == "chatSystemLine"
    assert system.content_label.property("pill") == "success"
    assert system.align == Qt.AlignmentFlag.AlignHCenter


def test_a_system_lines_tone_is_its_senders_and_survives_a_view_switch(panel):
    context = SimpleNamespace(tab_id="text_json", messages=[], cumulative_tokens=0,
                              project_manager=None)
    panel._refresh_validator_ui_for_context = lambda ctx: None
    panel.switch_context(context)
    panel.add_system_message("⟳ Validator rejected attempt 1/3 (2 issues). Re-prompting…",
                             tone="warning")
    panel.add_system_message("✓ text that only looks like a pass")  # no tone given: neutral
    panel.switch_context(context)  # rebuilt from the TabContext
    tones = [m.content_label.property("pill") for m in panel._messages()]
    texts = [m.content_label.text() for m in panel._messages()]
    assert tones == ["warning", "neutral"]
    assert texts[0] == "⟳ Validator rejected attempt 1/3 (2 issues). Re-prompting…"


def test_a_replys_thought_folds_behind_show_thinking(qapp):
    message = MessageWidget("assistant", "Done.", thinking_content="Counting steps")
    fold = message._fold
    assert fold.label.isHidden() and fold.button.text() == "▸ Show thinking"
    fold.button.click()
    assert not fold.label.isHidden() and fold.button.text() == "▾ Hide thinking"
    assert fold.button.objectName() == "foldToggle"


def test_details_opens_the_message_details(panel, monkeypatch):
    shown = []
    monkeypatch.setattr("workflow_editor.dock.chat_panel.MessageDetailDialog",
                        lambda prompt, response, parent: SimpleNamespace(
                            exec=lambda: shown.append((prompt, response))))
    panel._current_tab_context = SimpleNamespace(messages=[], cumulative_tokens=0)
    panel.add_message("assistant", "Done.", full_prompt="P", full_response="R", total_tokens=5)
    (bubble,) = panel._messages()
    details = next(b for b in bubble.findChildren(type(panel.send_btn)) if b.text() == "Details…")
    details.click()
    assert shown == [("P", "R")]


def test_the_composers_buttons_are_wired(panel):
    seen = []
    panel.message_sent.connect(lambda text: seen.append(("send", text)))
    panel.cancel_requested.connect(lambda: seen.append("cancel"))
    panel.reset_requested.connect(lambda: seen.append("reset"))
    panel.compact_requested.connect(lambda: seen.append("compact"))
    panel.add_message("user", "old")

    panel.input_field.setPlainText("hello")
    panel.send_btn.click()
    assert not panel.cancel_btn.isEnabled()
    panel.set_llm_active(True)
    assert panel.cancel_btn.isEnabled() and not panel.send_btn.isEnabled()
    panel.cancel_btn.click()
    panel.compact_btn.click()
    panel.reset_btn.click()

    assert seen == [("send", "hello"), "cancel", "compact", "reset"]
    assert panel._messages() == []  # reset clears the chat
    assert panel.send_btn.objectName() == "primaryButton"
    assert [cb.text() for cb in (panel.board_data_checkbox, panel.force_mode_checkbox,
                                 panel.auto_correct_checkbox)] == [
        "Board data", "Force: resend all", "Auto-correct"]


def test_the_context_meter_and_the_validator_dot(panel):
    panel.set_context_limit(400000)
    assert panel.context_label.text() == "Context 0 / 400,000"
    panel.add_message("assistant", "Done.", total_tokens=18646)
    assert panel.context_label.text() == "Context 18,646 / 400,000"
    assert panel.context_bar.value() == 18646 * 1000 // 400000
    assert panel.context_label.toolTip() == "Context: 18,646 / 400,000 tokens (5%)"

    assert panel.validator_status.isHidden()
    panel.reset_btn.click()  # a reset empties the meter too
    assert panel.context_label.text() == "Context 0 / 400,000" and panel.context_bar.value() == 0

    panel.set_validator_status(True)
    assert not panel.validator_status.isHidden() and panel.validator_dot.property("on") is True
    panel.set_validator_status(False, "no wheel")
    assert panel.validator_dot.property("on") is False
    assert "Reason: no wheel" in panel.validator_status.toolTip()


def test_the_streaming_bubble_folds_its_thought(panel):
    panel.add_thinking_message()
    bubble = panel._streaming
    assert bubble._thinking_header.text().startswith("💭 Thinking… 0:0")
    fold = bubble._fold
    assert fold.button.isHidden()
    panel.append_thinking_text("Counting steps")
    assert not fold.button.isHidden() and fold.label.isHidden()
    fold.button.click()
    assert not fold.label.isHidden() and fold.button.text() == "▾ Hide thinking"
    panel.append_response_text('"steps": [')
    assert fold.label.isHidden() and fold.button.text() == "▸ Show thinking"
    assert not bubble._response_label.isHidden()
    assert bubble._response_label.text() == '"steps": ['
    panel.remove_thinking_message()
    assert panel._streaming is None and panel._messages() == []


def test_the_findings_tab_carries_the_count(opened):
    w = opened
    dock = w.dock
    dock.show_validation_result_from_list([
        {"severity": "error", "message": "PSU2 is not declared", "location": "step 7",
         "suggested_fix": "Declare PSU2"},
        {"severity": "warning", "message": "'ms' expected"},
        {"severity": "info", "message": "Could be split"},
    ])
    index = dock.tab_widget.indexOf(dock.findings_panel)
    assert dock.tab_widget.tabText(index) == "Findings 3"
    assert dock.tab_widget.currentWidget() is dock.findings_panel
    pills = [dock.findings_panel.summary_row.itemAt(i).widget()
             for i in range(dock.findings_panel.summary_row.count() - 1)]
    assert [(p.text(), p.property("pill")) for p in pills] == [
        ("1 error", "danger"), ("1 warning", "warning"), ("1 info", "info")]
    assert dock.findings_panel.issue_list.count() == 3
    dock.findings_panel.clear_btn.click()
    assert dock.tab_widget.tabText(index) == "Findings"


def test_the_composer_sits_under_every_tab(opened):
    dock = opened.dock
    layout = dock.layout()
    assert [layout.itemAt(i).widget() for i in range(layout.count())][-1] is dock.chat_panel.composer
    assert dock.tab_widget.property("underline") is True
    assert [dock.tab_widget.tabText(i) for i in range(dock.tab_widget.count())] == [
        "Chat", "Session", "Findings", "Raw"]
