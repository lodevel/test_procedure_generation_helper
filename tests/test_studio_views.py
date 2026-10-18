"""Procedo Studio's four views (redesign slice 4): every editor is a card with its file name, its
Saved / ● Modified pill and its own Save; one action bar per view (the assistant row, whose
force variants are a split button's ▾ menu, and the FILE row, whose ⚡ pack actions and
validator buttons stay hidden until available); Traceability has no action bar."""

import pytest
from PySide6.QtWidgets import QGroupBox, QToolButton

from procedo.studio.llm import LLMTask
from procedo.studio.pack_ops import pack_parsers
from procedo.studio.widgets.action_bar import ActionBar

from .test_main_window_characterization import (
    _make_project, _open_project, make_window as make_window, mbox as mbox, qapp as qapp,
)


@pytest.fixture
def opened(make_window, tmp_path):
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    return w, root


def _views(w):
    return [w.view_stack.widget(i) for i in range(w.view_stack.count())]


def test_every_editor_is_a_card_and_the_old_groups_are_gone(opened):
    w, _root = opened
    cards = [(view.tab_id, card.file_name) for view in _views(w) for card in view.cards]
    assert cards == [("text_only", "procedure_text.md"),
                     ("text_json", "procedure_text.md"), ("text_json", "procedure.json"),
                     ("json_code", "procedure.json"), ("json_code", "test.py"),
                     ("traceability", "test.py")]
    assert not w.view_stack.findChildren(QGroupBox)
    for view in _views(w):
        for old in ("save_text_btn", "save_json_btn", "save_code_btn", "text_status",
                    "json_status", "code_status", "_llm_group"):
            assert not hasattr(view, old), (view.tab_id, old)


def test_a_cards_save_writes_its_file_and_the_header_follows(opened):
    w, root = opened
    tab = w.json_code_tab
    w.view_stack.setCurrentWidget(tab)
    test_py, procedure_json = root / "tests" / "t1" / "test.py", root / "tests" / "t1" / "procedure.json"
    json_before = procedure_json.read_text(encoding="utf-8")

    tab.code_editor.appendPlainText("# edited")
    assert tab.code_card.pill.text() == "● Modified" and tab.code_card.save_button.isEnabled()
    assert w.artifact_pills["Code"].text() == "Code ● modified"
    assert tab.json_card.pill.text() == "✓ Saved" and not tab.json_card.save_button.isEnabled()

    tab.code_card.save_button.click()
    assert test_py.read_text(encoding="utf-8").endswith("# edited")
    assert procedure_json.read_text(encoding="utf-8") == json_before
    assert tab.code_card.pill.text() == "✓ Saved"
    assert w.artifact_pills["Code"].text() == "Code ✓"


def test_traceability_has_no_action_bar_and_its_code_card_saves(opened):
    w, root = opened
    tt = w.traceability_tab
    w.view_stack.setCurrentWidget(tt)
    assert not hasattr(tt, "action_bar") and not tt.findChildren(ActionBar)
    tt.rebuild_llm_buttons()  # settings saved: nothing to rebuild, no error
    assert tt.status_label.objectName() == "viewBanner"

    tt.code_editor.setPlainText("# Step 1\nx = 1\n")
    tt.code_card.save_button.click()
    assert (root / "tests" / "t1" / "test.py").read_text(encoding="utf-8") == "# Step 1\nx = 1\n"
    assert tt.code_card.pill.text() == "✓ Saved"


def test_the_force_variant_is_in_the_split_buttons_menu(opened):
    w, _root = opened
    tab = w.text_json_tab
    runs = []
    tab._run_task_async = lambda task, **kw: runs.append((task, kw))
    tab.text_editor.setPlainText("# t1\n\nBody.\n")
    layout = tab.action_bar.assistant_layout
    buttons = [layout.itemAt(i).widget() for i in range(layout.count())]
    split = [b for b in buttons if isinstance(b, QToolButton) and b.menu() is not None]
    assert [b.property("task_id_override") for b in split] == [LLMTask.DERIVE_JSON_FROM_TEXT.value]

    strict, force = split[0].menu().actions()
    force.trigger()
    strict.trigger()
    assert runs == [(LLMTask.DERIVE_JSON_FROM_TEXT, {"strict_mode": False}),
                    (LLMTask.DERIVE_JSON_FROM_TEXT, {"strict_mode": True})]


def test_pack_buttons_stay_hidden_until_the_wheel_provides_them(opened, monkeypatch):
    w, _root = opened
    tj, to, jc = w.text_json_tab, w.text_only_tab, w.json_code_tab
    packs = [tj.quick_parse_btn, tj.quick_render_btn, tj.parse_and_generate_btn,
             to.parse_and_generate_btn, jc.quick_code_btn]

    monkeypatch.setattr(pack_parsers, "is_available", lambda root=None: (False, "no wheel"))
    for tab in (tj, to):
        tab.refresh_parser_button()
    jc.refresh_code_parser_button()
    assert all(b.isHidden() for b in packs)
    assert tj.sync_meta_btn.isHidden() and to.sync_meta_btn.isHidden()
    assert not _divider_visible(tj)

    monkeypatch.setattr(pack_parsers, "is_available", lambda root=None: (True, ""))
    monkeypatch.setattr(pack_parsers, "can", lambda cap, text, root=None: (
        (False, [("PSU1", "labscpi")]) if cap == "emit" else (True, [])))
    monkeypatch.setattr(pack_parsers, "can_for_types", lambda cap, types, root=None: (True, []))
    for tab in (tj, to):
        tab.refresh_parser_button()
    jc.refresh_code_parser_button()
    assert not any(b.isHidden() for b in packs)
    assert not tj.sync_meta_btn.isHidden() and not to.sync_meta_btn.isHidden()
    assert not tj.quick_render_btn.isEnabled()  # needs "emit": no pack provides it
    assert "PSU1 (labscpi)" in tj.quick_render_btn.toolTip()
    assert tj.quick_parse_btn.isEnabled() and jc.quick_code_btn.isEnabled()
    assert _divider_visible(tj)


def _divider_visible(tab) -> bool:
    from PySide6.QtCore import QCoreApplication, QEvent
    row = tab.action_bar.file_layout.parentWidget()
    QCoreApplication.sendEvent(row, QEvent(QEvent.Type.LayoutRequest))
    return not row.divider.isHidden()


def test_validator_buttons_follow_the_projects_switch(opened, monkeypatch):
    from procedo.studio.llm import validator_loop_settings
    w, _root = opened
    tab = w.text_only_tab

    def validators():
        layout = tab.action_bar.file_layout
        widgets = (layout.itemAt(i).widget() for i in range(layout.count()))
        return [b for b in widgets if b is not None and b.property("validator_id")]

    monkeypatch.setattr(validator_loop_settings, "is_enabled", lambda root: False)
    tab.rebuild_validator_buttons()
    assert validators() == []
