"""Procedo Studio's dialogs (redesign slice 7a, boards S8 Syntax, S9 Skill chat, S10 Validator
errors / Select rules, S12 the small ones): each is Bench's frame — a header band, a footer
band, one primary button — and keeps its functions: find in page, the skill picker, the insert
callback, the rule count, the validator's issue cards, the tests to export; the board's picture
at the board's angle, opened in Bench's image popup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QLabel, QPushButton

from procedo.core import ui_kit
from procedo.studio.authoring import ContextItem, ContextSource
from procedo.studio.core import project_manager
from procedo.studio import shell
from procedo.studio.dialogs import word_keywords_dialog
from procedo.studio.dialogs.about_dialog import VERSION, AboutDialog
from procedo.studio.dialogs.syntax_reference import SyntaxReferenceDialog
from procedo.studio.dialogs import test_selection_dialog
from procedo.studio.dialogs.validator_error_dialog import ValidatorErrorDialog
from procedo.studio.dock.skill_chat_dialog import SkillChatDialog
from procedo.studio.dock.skill_chat_widget import SkillChatWidget
from procedo.studio.llm.validator_dispatch import ValidationIssueView, ValidationOutcome
from procedo.bench.image_popup import ImagePopup
from procedo.core import board_view, config_manager
from procedo.studio.widgets import netlist_panel
from procedo.studio.widgets.netlist_panel import NetlistPanel
from procedo.studio.widgets.rule_selector import RuleSelectorDialog

from .test_main_window_characterization import qapp as qapp


def _frame(dialog):
    """The dialog's header band, footer band and its primary buttons."""
    header = dialog.findChild(ui_kit.HeaderBand)
    footer = dialog.findChild(ui_kit.FooterBand)
    primaries = [b for b in dialog.findChildren(QPushButton) if b.objectName() == "primaryButton"]
    return header, footer, primaries


# ---- S8 DSL Syntax Reference -----------------------------------------------------------------

@pytest.fixture
def rules_root(tmp_path):
    project = tmp_path / "proj"
    (project / "bundle" / "rules").mkdir(parents=True)
    (project / "bundle" / "bundle.json").write_text(
        json.dumps({"id": "baseline", "version": "2.8.1"}), encoding="utf-8")
    (project / "bundle" / "defaults.json").write_text("{}", encoding="utf-8")
    rules = project / "bundle" / "rules"
    (rules / "001_text.md").write_text(
        "# Canonical Text Procedure\n\n## Canonical verbs\n\n| Verb | Used by |\n|---|---|\n"
        "| `Connect` | manual wiring |\n| `Measure` | readout binding |\n", encoding="utf-8")
    (rules / "002_dmm.md").write_text("# DMM rules\n\nThe DMM reads volts.\n", encoding="utf-8")
    return rules


def test_the_syntax_reference_names_the_bundle_lists_its_pages_and_finds_in_the_page(qapp, rules_root):
    SyntaxReferenceDialog._instance = None
    bundle = shell.bundle_name(rules_root.parent.parent)  # the footer's and the header's resolver
    assert bundle == "baseline 2.8.1"
    dlg = SyntaxReferenceDialog.show_reference(rules_root, None, bundle)
    try:
        header, footer, _ = _frame(dlg)
        assert header.title.text() == "DSL Syntax Reference"
        assert header.subtitle.text() == "bundle baseline 2.8.1 · F1"
        assert [b.text() for b in footer.findChildren(QPushButton)] == ["Close"]
        page_list, view = dlg._pages.page_list, dlg._pages.view
        pages = [page_list.item(i).text() for i in range(page_list.count())]
        assert pages == ["Cheat sheet", "FULL RULES", "Canonical Text Procedure", "DMM rules"]
        assert page_list.currentRow() == 0  # the cheat sheet is the landing page
        assert view.font().pointSize() >= 11  # the page's reading size

        page_list.setCurrentRow(3)
        assert "The DMM reads volts." in view.toPlainText()
        dlg._search.setText("volts")
        next_btn = next(b for b in header.findChildren(QPushButton) if b.text() == "Next")
        next_btn.click()
        assert view.textCursor().selectedText() == "volts"
    finally:
        dlg.close()


def test_enter_in_the_syntax_references_search_finds_once(qapp, rules_root):
    """Enter is the search field's own: one press finds the next match once (the Next button,
    the dialog's default, does not find again)."""
    SyntaxReferenceDialog._instance = None
    dlg = SyntaxReferenceDialog.show_reference(rules_root, None, "baseline 2.8.1")
    try:
        dlg._pages.page_list.setCurrentRow(3)  # "DMM rules" then "The DMM reads volts."
        QApplication.processEvents()
        dlg._search.setFocus()
        dlg._search.setText("DMM")
        QTest.keyClick(dlg._search, Qt.Key.Key_Return)
        view = dlg._pages.view
        assert view.textCursor().selectionStart() == view.toPlainText().find("DMM")
    finally:
        dlg.close()

# ---- S9 Skill chat ---------------------------------------------------------------------------

class _Rules(ContextSource):
    def __init__(self):
        super().__init__("rules", "Rules")

    def list_items(self):
        return [ContextItem("rules", f"00{i}_rule.md", f"00{i}_rule.md") for i in (1, 2, 3)]

    def materialize(self, keys):
        return "\n".join(keys)


def _skill(skill_id, title, when):
    return SimpleNamespace(skill_id=skill_id, title=title, version="0.11.9", when_to_use=when,
                           system_prompt="", metadata={})


@pytest.fixture
def chat_dialog(qapp, monkeypatch):
    monkeypatch.setattr("procedo.studio.dialogs.settings_dialog.load_settings", lambda: {})
    inserted = []
    skills = [_skill("dcdc", "DCDC test builder", "Find a power IC."),
              _skill("board", "Board bring-up", "Bring a board up.")]
    dlg = SkillChatDialog(skills, [_Rules()], SimpleNamespace(), None, inserted.append)
    yield dlg, inserted
    dlg.close()


def test_the_skill_chat_has_the_skill_picker_in_its_header_and_insert_as_its_primary(chat_dialog):
    dlg, _ = chat_dialog
    header, footer, primaries = _frame(dlg)
    assert header is dlg._chat.header and header.parentWidget() is dlg  # across the window's top
    combo = header.findChild(QComboBox)
    assert header.title.text() == "Skill chat"
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "DCDC test builder · v0.11.9", "Board bring-up · v0.11.9"]
    assert header.subtitle.text() == "Find a power IC."
    assert [b.text() for b in primaries] == ["▶ Run skill", "Insert into procedure"]
    assert [b.text() for b in footer.findChildren(QPushButton)] == ["Close", "Insert into procedure"]

    combo.setCurrentIndex(1)  # the skill picker switches the skill and its description
    assert dlg._chat._skill.skill_id == "board"
    assert header.subtitle.text() == "Bring a board up."


def test_insert_hands_the_latest_reply_to_the_callback_and_says_so(chat_dialog):
    dlg, inserted = chat_dialog
    assert not dlg._insert_btn.isEnabled()
    dlg._chat._latest_draft = "1. Connect SCOPE1 CH1 to TP31."
    dlg._chat.reply_finished.emit(dlg._chat._latest_draft)
    assert dlg._insert_btn.isEnabled() and dlg._footer.note.text() == "Draft ready"

    dlg._insert_btn.click()
    assert inserted == ["1. Connect SCOPE1 CH1 to TP31."]
    assert dlg._footer.note.text() == "Inserted latest draft into the procedure."

    dlg._chat.clear()  # a reset disables Insert again
    assert not dlg._insert_btn.isEnabled() and dlg._footer.note.text() == ""


def test_the_context_card_counts_the_ticks_per_tab_and_in_its_readout(chat_dialog):
    dlg, _ = chat_dialog
    picker = dlg._chat._picker
    picker.set_checked("rules", ["001_rule.md", "003_rule.md"])
    picker._update_readout()
    assert picker._tabs.tabText(0) == "Rules 2"
    assert picker._readout.text().startswith("Context: 2 items · ~")
    assert picker.selections()[0][1] == ["001_rule.md", "003_rule.md"]

    deselect = next(b for b in picker.findChildren(QPushButton) if b.text() == "Deselect all")
    deselect.click()
    assert picker.selections()[0][1] == []


def test_the_chat_draws_the_turns_as_bubbles_and_its_icon_buttons_are_wired(chat_dialog):
    dlg, _ = chat_dialog
    chat = dlg._chat
    chat._append_line("You", "Build <U86>")
    chat._append_line("Assistant", "U86 is a buck.")
    page = chat._transcript.toHtml()
    assert "Build &lt;U86&gt;" in page and "U86 is a buck." in page
    assert [b.objectName() for b in (chat._stop_btn, chat._trash_btn, chat._restart_btn)] == [
        "lineIconButton"] * 3
    chat._trash_btn.click()
    assert chat._transcript.toPlainText() == ""


def test_a_bare_chat_shows_its_own_skill_picker_and_elides_a_long_status(qapp, monkeypatch):
    """No host takes the header: it is the widget's first row. A long status (the wizard's
    "Loaded N cached IC(s) — …") is cut to the row, whole in its tooltip."""
    monkeypatch.setattr("procedo.studio.dialogs.settings_dialog.load_settings", lambda: {})
    widget = SkillChatWidget([_skill("dcdc", "DCDC", "Find a power IC.")], SimpleNamespace())
    assert widget.layout().itemAt(0).widget() is widget.header
    assert widget.header.findChild(QComboBox) is widget._skill_combo
    width = widget.minimumSizeHint().width()
    text = "Loaded 12 cached IC(s) — 'Restart' to refresh. " * 6
    widget.set_status(text)
    assert widget._status.toolTip() == text and widget._status.full == text
    assert widget.minimumSizeHint().width() == width


def test_an_embedded_chat_with_a_pinned_skill_has_no_header():
    """The DCDC wizard's embed: no skill picker, no window header, the same public API."""
    QApplication.instance() or QApplication([])
    widget = SkillChatWidget([_skill("dcdc", "DCDC", "")], SimpleNamespace(),
                             show_skill_selector=False, show_run_button=False,
                             backend_tab_id="dcdc_classifier")
    assert widget.header is None and widget._run_btn is None
    for name in ("reply_finished", "reply_failed", "busy_changed", "conversation_reset",
                 "shutdown", "stop", "set_skill", "set_pushed_context", "resolved_context",
                 "run_kickoff", "send_user_turn", "set_status", "is_busy", "latest_reply"):
        assert hasattr(widget, name), name


# ---- S10 Validator errors and Select rules ---------------------------------------------------

def test_the_validator_dialog_shows_the_counts_and_one_card_per_issue(qapp):
    outcome = ValidationOutcome(ok=False, reason="schema", issues=[
        ValidationIssueView(code="E-EQUIP-UNKNOWN", message="PSU2 is not declared.",
                            severity="error", location="step 7", fix_hint="Declare PSU2."),
        ValidationIssueView(code="W-UNIT", message="'ms' expected.", severity="warning",
                            location="step 25"),
    ])
    dlg = ValidatorErrorDialog(outcome, title="Validator rejected the proposal",
                               intro="Text → JSON · procedure.json was not changed")
    header, footer, primaries = _frame(dlg)
    assert (header.title.text(), header.subtitle.text()) == (
        "Validator rejected the proposal", "Text → JSON · procedure.json was not changed")
    assert [b.text() for b in primaries] == ["Close"]
    pills = [dlg.counts.itemAt(i).widget().text() for i in range(dlg.counts.count() - 1)]
    assert pills == ["1 error", "1 warning"]
    cards = dlg.findChildren(QFrame, "findingCard")
    assert len(cards) == 2
    texts = [label.text() for label in cards[0].findChildren(QLabel)]
    assert texts[:3] == ["E-EQUIP-UNKNOWN", "error", "step 7"]  # the code, its severity, where
    assert "Fix hint: Declare PSU2." in texts
    assert [label.text() for label in cards[1].findChildren(QLabel)][1] == "warning"

    next(b for b in footer.findChildren(QPushButton) if b.text() == "Copy all").click()
    assert QApplication.clipboard().text().startswith("- [error] E-EQUIP-UNKNOWN: PSU2")


def test_the_rule_selector_counts_the_ticked_rules_and_saves_each_tick(qapp, tmp_path):
    saved = []
    tcm = SimpleNamespace(
        get_all_tasks_for_tab=lambda tab: [SimpleNamespace(selected_rules=["a.md", "c.md"])],
        set_selected_rules_for_tab=lambda tab, rules: saved.append((tab, rules)),
        save_config=lambda: None)
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text("x" * 1500, encoding="utf-8")
    pm = SimpleNamespace(project_root=None, get_rule_docs=lambda: [
        project_manager.RuleDoc(name, tmp_path / name, "bundle") for name in ("a.md", "b.md", "c.md")])
    dlg = RuleSelectorDialog("text_json", pm, tcm, view=shell.VIEWS[1][0])
    header, footer, primaries = _frame(dlg)
    assert (header.title.text(), header.subtitle.text()) == ("Select rules", "Text–JSON view")
    assert [b.text() for b in primaries] == ["OK"]
    assert footer.note.text() == "2 of 3 rules selected · 3 KB"

    dlg.rules.set_checked(["a.md", "b.md", "c.md"])
    assert footer.note.text() == "3 of 3 rules selected · 5 KB"
    assert saved[-1] == ("text_json", ["a.md", "b.md", "c.md"])
    assert dlg.get_selected_rules() == ["a.md", "b.md", "c.md"]


# ---- S12 the small ones ----------------------------------------------------------------------

def test_the_export_selection_returns_the_ticked_tests_and_greys_those_without_the_artifact(qapp):
    info = project_manager.TestFolderInfo
    folders = [info(Path("a"), "AUX0_output", has_json=True, has_text=True),
               info(Path("b"), "AUX1_output", has_json=True, has_text=True, has_code=True),
               info(Path("c"), "EPO", has_text=True)]
    dlg = test_selection_dialog.TestSelectionDialog(folders, require="json")
    header, footer, primaries = _frame(dlg)
    assert (header.title.text(), header.subtitle.text()) == ("Export procedures", "Procedure to Word")
    assert [b.text() for b in primaries] == ["Export"]
    assert footer.note.text() == "2 of 3"
    assert [f.name for f in dlg.selected_folders()] == ["AUX0_output", "AUX1_output"]

    next(b for b in dlg.findChildren(QPushButton) if b.text() == "None").click()
    assert dlg.selected_folders() == [] and footer.note.text() == "0 of 3"
    dlg.tests.set_checked(["AUX1_output", "EPO"])  # EPO has no JSON: never selected
    assert [f.name for f in dlg.selected_folders()] == ["AUX1_output"]


def test_word_keywords_shows_the_main_apps_reference_in_its_own_dialog(qapp):
    dlg = word_keywords_dialog.WordKeywordsDialog(word_keywords_dialog.keywords_markdown())
    header, _, primaries = _frame(dlg)
    assert header.title.text() == "Word export keywords"
    assert [b.text() for b in primaries] == ["Close"]
    assert "Word Export Template Keywords" in dlg.page.toPlainText()
    document = dlg.page.document()
    blocks = [document.findBlockByNumber(i) for i in range(document.blockCount())]
    headings = [(b.blockFormat().headingLevel(), b.text()) for b in blocks if b.blockFormat().headingLevel()]
    assert headings[:3] == [(1, "Word Export Template Keywords"), (2, "Template Location"),
                            (2, "Template Resolution")]
    tables = ui_kit._tables(document.rootFrame())
    assert [tables[0].cellAt(0, c).firstCursorPosition().block().text() for c in range(2)] == [
        "Syntax", "Purpose"]
    assert any(b.blockFormat().nonBreakableLines() and "{{ author.name }}" in b.text() for b in blocks)
    assert document.find("docxtpl docs").charFormat().anchorHref() == (
        "https://docxtpl.readthedocs.io/en/latest/#jinja2-like-syntax")


def test_about_shows_the_studio_mark_its_name_and_version(qapp):
    dlg = AboutDialog()
    header, _, primaries = _frame(dlg)
    assert header.title.text() == "About"
    assert [b.text() for b in primaries] == ["OK"]
    assert dlg.findChild(QLabel, "studioName").text() == "Studio"
    assert dlg.version.text() == f"Version {VERSION}"
    assert any(not label.pixmap().isNull() for label in dlg.findChildren(QLabel))  # the mark


def test_the_board_view_shows_at_the_boards_angle_and_its_popup_is_benchs(qapp, tmp_path, monkeypatch):
    config_manager.write_config_section(tmp_path / "config", "board_view", {"rotation": 90})
    panel = NetlistPanel()
    panel._project_root = tmp_path
    panel.set_hint("KC30_revC.tgz")
    panel.show_image(QPixmap(200, 100), QPixmap(300, 100), "C42 pin 2")
    shown = panel._viewer_label.pixmap()
    assert shown.height() > shown.width()  # the board's 90°

    opened = []

    class _Popup(ImagePopup):
        def exec(self):  # the operator turns the board right once, then closes
            opened.append(self)
            next(b for b in self.findChildren(QPushButton) if b.text() == "⟳").click()
            return 0

    monkeypatch.setattr(netlist_panel, "ImagePopup", _Popup)
    panel._on_viewer_click(None)
    header = opened[0].findChild(ui_kit.HeaderBand)
    assert (header.title.text(), header.subtitle.text()) == ("C42 pin 2", "KC30_revC.tgz")
    assert opened[0].angle == 180 and board_view.rotation(tmp_path) == 180
    board_view.wait_saved()
    shown = panel._viewer_label.pixmap()
    assert shown.width() > shown.height()  # the viewer follows the popup's turn
    panel.close()
