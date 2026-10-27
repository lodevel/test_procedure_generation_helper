"""Studio and a code test (design openhtf_results_design.md slices 13-14; boards StudioCode,
StudioCompare, StudioNew, StudioPhases).

A code test opens in the code view: its openhtf/test.py beside the phase list the engine's
describe reads (a click jumps to the line), the export preview, the Check (off the GUI thread);
the header's Text | Code switch writes test.json; the Tests column shows TXT / HTF; "+ New test…"
makes a Python test from the starter; the shared phases show only while a code test is open.

The engine's answers are its real ``rules_packager_base.code_test`` functions, called in place of
the project's TOOLS engine (``procedo.core.code_test_check``).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from PySide6.QtWidgets import QDialog, QLabel
from rules_packager_base import code_test

from procedo.core import code_test_check
from procedo.core.code_test_check import CodeCheck
from procedo.studio.core import open_in_editor as opener
from procedo.studio.core.code_test_preview import check_lines, preview_html
from procedo.studio.dialogs.new_test_dialog import NewTestDialog
from procedo.studio.tabs.code_test_view import PHASES, PREVIEW

from .test_main_window_characterization import (
    _make_project,
    _open_project,
    _settle,
    make_window as make_window,
    mbox as mbox,
    qapp as qapp,
)

_SHARED = '''import openhtf as htf


@htf.PhaseOptions(name="Power off")
def power_off(test):
    """Switch every output off."""


def helper(x):
    return x
'''
_CODE = '''import openhtf as htf
from openhtf.util import units
from common_phases import power_off

PSU1_VISA = "ASRL3::INSTR"
DMM1_VISA = "USB0::0x2A8D::INSTR"


@htf.PhaseOptions(name="Power-up checks")
@htf.plug(psu=Psu, dmm=Dmm)
@htf.measures(htf.Measurement("v5").in_range(4.75, 5.25).with_units(units.VOLT).doc("5 V rail on TP 5V0"))
def power_up(test, psu, dmm):
    """Power the GCU at 28 V and check the 5 V rail."""
    substep("Set 28 V, limit 1 A")
    psu.set_voltage(28)
    substep("Measure the 5 V rail on TP 5V0")
    test.measurements.v5 = dmm.read_voltage()


test = htf.Test(power_up, power_off)
'''


def _project(tmp_path: Path) -> Path:
    """AUX0_output: a code test with an inactive text version; EPO: a text test."""
    root = _make_project(tmp_path / "p", tests=("AUX0_output", "EPO"))
    code = root / "tests" / "AUX0_output"
    (code / "openhtf").mkdir()
    (code / "openhtf" / "test.py").write_bytes(_CODE.replace("\n", "\r\n").encode())
    (code / "test.json").write_text('{"active": "code"}', encoding="utf-8")
    (root / "functions").mkdir()
    (root / "functions" / "common_phases.py").write_text(_SHARED, encoding="utf-8")
    return root


def _real_engine(monkeypatch, asked: list | None = None) -> None:
    """code_test_check's three calls answered by the engine's own functions."""

    def paths(root, test):
        return Path(root) / "tests" / test / "openhtf" / "test.py", Path(root) / "functions"

    def describe(root, test, source=None):
        if asked is not None:
            asked.append(("describe", threading.current_thread().name))
        return code_test.describe(*paths(root, test), source), ""

    def check(root, test):
        if asked is not None:
            asked.append(("check", threading.current_thread().name))
        found = code_test.check(*paths(root, test))
        return CodeCheck((), (), found["summary"], found["describe"])

    monkeypatch.setattr(code_test_check, "describe", describe)
    monkeypatch.setattr(code_test_check, "check", check)
    monkeypatch.setattr(
        code_test_check,
        "shared_phases",
        lambda root: (code_test.shared_phases(Path(root) / "functions", Path(root) / "tests"), ""),
    )


def _opened(make_window, monkeypatch, tmp_path, test="AUX0_output", asked=None):
    _real_engine(monkeypatch, asked)
    root = _project(tmp_path)
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / test)
    _settle()
    return w, root


def _tree(w) -> list[str]:
    tree, out = w.code_view.phase_tree, []
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        out.append(top.text(0))
        out += [f"  {top.child(k).text(0)}" for k in range(top.childCount())]
    return out


def test_a_code_test_opens_in_the_code_view_with_the_phases_describe_reads(
    make_window, monkeypatch, tmp_path
):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    assert w.middle.currentWidget() is w.code_view and w.artifact_manager is None
    assert w.code_view.editor.toPlainText() == _CODE  # its CRLF shown as lines
    assert _tree(w) == [
        "1 Power-up checks",
        "  1.1 Set 28 V, limit 1 A",
        "  1.2 Measure the 5 V rail on TP 5V0",
        "  v5 4.75–5.25 V",
        "2 Power off (shared)",
    ]
    assert w.header.title.text() == "AUX0_output" and w.header.subtitle.text().endswith("2 steps")
    assert w.artifact_status.isHidden() and not w.inactive_pill.isHidden()
    assert w.inactive_pill.text() == "Text version: inactive, not updated"
    assert not w.dock.isEnabled()  # the Assistant works on the text views


def test_clicking_a_phase_or_a_sub_step_jumps_to_its_line(make_window, monkeypatch, tmp_path):
    w, _root = _opened(make_window, monkeypatch, tmp_path)
    view = w.code_view
    view.show_view(PHASES)  # the phases alone: a click brings the editor back
    sub = view.phase_tree.topLevelItem(0).child(1)  # 1.2, line 16
    view._on_phase_clicked(sub, 0)
    assert view.editor.textCursor().blockNumber() + 1 == 16 and view.editor_card.isVisibleTo(view)
    view._on_phase_clicked(view.phase_tree.topLevelItem(0), 0)
    assert view.editor.textCursor().blockNumber() + 1 == 12  # its def
    view._on_phase_clicked(view.phase_tree.topLevelItem(1), 0)  # the shared one: another file
    assert view.editor.textCursor().blockNumber() + 1 == 12


def test_the_check_runs_off_the_gui_thread_and_shows_its_results_line(
    make_window, monkeypatch, tmp_path
):
    asked: list = []
    w, _root = _opened(make_window, monkeypatch, tmp_path, asked=asked)
    w.code_view.check()
    _settle(lambda: w.code_view.result_texts())
    assert [where for what, where in asked if what == "check"] == ["engine-call"]
    assert w.code_view.result_texts() == [
        "✓ Syntax OK",
        "✓ Loads in the bundle's environment",
        "✓ 2 steps · 1 measurement · 2 instruments",
    ]


def test_save_writes_the_file_its_line_endings_kept(make_window, monkeypatch, tmp_path):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    view = w.code_view
    view.editor.setPlainText(_CODE.replace("Set 28 V, limit 1 A", "Set 24 V"))
    assert (
        view.dirty and w.save_all_btn.text() == "Save all · 1" and view.pill.text() == "● Modified"
    )
    assert w._document_controller.on_save_all()
    _settle()
    disk = (root / "tests" / "AUX0_output" / "openhtf" / "test.py").read_bytes()
    assert disk == _CODE.replace("Set 28 V, limit 1 A", "Set 24 V").replace("\n", "\r\n").encode()
    _settle(lambda: _tree(w)[1] == "  1.1 Set 24 V")  # the phases follow the editor
    assert view.pill.text() == "✓ Saved"


def test_the_switch_writes_test_json_and_opens_the_test_in_that_version(
    make_window, monkeypatch, tmp_path
):
    w, root = _opened(make_window, monkeypatch, tmp_path, test="EPO")
    folder = root / "tests" / "EPO"
    assert w.middle.currentWidget() is w.view_column and w.version_switch.isVisibleTo(w)
    assert not w.version_segments["code"].isEnabled()  # EPO has no openhtf/test.py
    w.open_test(root / "tests" / "AUX0_output")
    _settle()
    w.version_segments["text"].click()
    _settle()
    test_json = root / "tests" / "AUX0_output" / "test.json"
    assert json.loads(test_json.read_text(encoding="utf-8")) == {"active": "text"}
    assert w.middle.currentWidget() is w.view_column and w.code_view.test_dir is None
    assert w.artifact_manager.test_dir == root / "tests" / "AUX0_output"
    assert w.inactive_pill.text() == "Code version: inactive, not updated"
    assert _badges(w)["AUX0_output"] == "TXT"
    w.version_segments["code"].click()
    _settle()
    assert json.loads(test_json.read_text(encoding="utf-8")) == {"active": "code"}
    assert w.code_view.test_dir == root / "tests" / "AUX0_output"
    assert _badges(w) == {"AUX0_output": "HTF", "EPO": "TXT"}
    assert folder.exists()


def _badges(w) -> dict[str, str]:
    from procedo.ui.badge_delegate import BADGES_ROLE

    rows = w.workspace_widget.test_list
    return {
        rows.item(i).data(0x0100).name: rows.item(i).data(BADGES_ROLE)[0][0]
        for i in range(rows.count())
    }


def test_the_shared_phases_show_only_while_a_code_test_is_open(make_window, monkeypatch, tmp_path):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    panel = w.workspace_widget.shared_phases
    assert panel.isVisibleTo(w) and [p["name"] for p in panel.phases] == ["Power off"]
    w.open_test(root / "tests" / "EPO")
    _settle()
    assert not panel.isVisibleTo(w) and panel.phases == []


def test_new_test_python_creates_the_starter_and_opens_it(make_window, monkeypatch, tmp_path):
    w, root = _opened(make_window, monkeypatch, tmp_path, test="EPO")

    def chosen(dialog):
        dialog.name_edit.setText("AUX1_burst")
        dialog.options["code"].radio.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(NewTestDialog, "exec", chosen)
    w.workspace_widget._on_create_new_test()
    _settle()
    folder = root / "tests" / "AUX1_burst"
    assert json.loads((folder / "test.json").read_text(encoding="utf-8")) == {"active": "code"}
    assert w.code_view.test_dir == folder and len(w.code_view.described["phases"]) == 1
    assert not (folder / "procedure_text.md").exists()


def test_the_export_preview_is_the_phases_their_sub_steps_expected_and_equipment():
    described = {
        "phases": [
            {
                "name": "Power-up checks",
                "file": "openhtf/test.py",
                "line": 9,
                "description": "Power the GCU at 28 V.",
                "substeps": [{"text": "Set 28 V", "line": 11}],
                "measurements": [
                    {
                        "name": "v5",
                        "units": "V",
                        "validators": ["4.75 <= x <= 5.25"],
                        "description": "5 V rail",
                        "lower": 4.75,
                        "upper": 5.25,
                    }
                ],
            },
            {
                "name": "Power off",
                "file": "functions/common_phases.py",
                "line": 4,
                "description": "Switch every output off.",
                "substeps": [],
                "measurements": [],
            },
        ],
        "instruments": ["Dmm", "Psu"],
        "constants": [{"name": "PSU1_VISA"}, {"name": "DMM1_VISA"}],
    }
    html = preview_html(described)
    for part in (
        "<b>1. Power-up checks</b>",
        "1.1 Set 28 V",
        "Expected: v5 (5 V rail) between 4.75 V and 5.25 V",
        "<b>2. Power off</b>",
        "(shared phase, functions/common_phases.py)",
        "Equipment: Dmm (DMM1), Psu (PSU1)",
    ):
        assert part in html


def test_the_check_results_say_what_failed():
    assert check_lines(CodeCheck(("test.py line 2: '(' was never closed",), (), "", None)) == [
        ("danger", "✗ test.py line 2: '(' was never closed", "")
    ]
    lines = check_lines(CodeCheck(("does not load",), ("w1", "w2"), "1 step", {"phases": []}))
    assert [text for _tone, text, _tip in lines] == [
        "✓ Syntax OK",
        "✗ does not load",
        "✓ 1 step",
        "⚠ 2 warnings",
    ]


def test_open_in_my_editor_never_runs_a_python_file(monkeypatch, tmp_path):
    """A .py whose default app is a Python interpreter, or an app Windows cannot name, opens with
    the Edit action; only an editor opens it the default way."""
    opened = []
    monkeypatch.setattr(opener.os, "startfile", lambda *a: opened.append(a), raising=False)
    path = tmp_path / "test.py"
    apps = (
        r"C:\Windows\py.exe",
        r"C:\Windows\PYW.EXE",
        r"C:\Python313\pythonw.exe",
        r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\python3.12.exe",
        "",
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    )
    for app in apps:
        monkeypatch.setattr(opener, "_default_app", lambda _p, app=app: app)
        assert opener.open_in_editor(path) == ""
    assert opened == [(str(path), "edit")] * 5 + [(str(path),)]


def test_without_an_edit_action_a_file_python_would_run_is_refused(monkeypatch, tmp_path):
    def startfile(*args):
        raise OSError(1155, "No application is associated with the specified file")

    monkeypatch.setattr(opener.os, "startfile", startfile, raising=False)
    monkeypatch.setattr(opener, "_default_app", lambda _p: r"C:\Windows\py.exe")
    assert opener.open_in_editor(tmp_path / "test.py") == (
        "test.py could not be opened: No application is associated with the specified file. "
        "Choose an editor as the default app for .py files."
    )


def test_the_export_preview_view_shows_the_editor_and_the_preview(
    make_window, monkeypatch, tmp_path
):
    w, _root = _opened(make_window, monkeypatch, tmp_path)
    w.code_view.show_view(PREVIEW)
    assert w.code_view.side.currentWidget() is w.code_view.preview_card
    assert "Power-up checks" in w.code_view.preview.toPlainText()


def test_unsaved_code_is_asked_about_before_another_test_opens(
    make_window, monkeypatch, tmp_path, mbox
):
    from PySide6.QtWidgets import QMessageBox

    w, root = _opened(make_window, monkeypatch, tmp_path)
    w.code_view.editor.setPlainText("# edited\n")
    mbox.question_answer = QMessageBox.StandardButton.Cancel
    w.open_test(root / "tests" / "EPO")
    assert [text for _title, text in mbox.question] == [
        "You have unsaved changes in:\n  • openhtf/test.py\n\nSave before continuing?"
    ]
    assert w.code_view.test_dir == root / "tests" / "AUX0_output"  # it stays open, edited
    assert w.code_view.dirty
    w.code_view.close()  # the window closes without asking again


def _file(root: Path) -> Path:
    return root / "tests" / "AUX0_output" / "openhtf" / "test.py"


def test_a_save_asks_before_overwriting_a_change_made_outside_studio(
    make_window, monkeypatch, tmp_path
):
    from procedo.studio.tabs import code_test_view

    w, root = _opened(make_window, monkeypatch, tmp_path)
    view, asked = w.code_view, []
    _file(root).write_bytes(b"# changed outside\r\n")
    view.editor.setPlainText(_CODE + "# mine\n")
    for choice in ("", code_test_view.OVERWRITE):
        monkeypatch.setattr(
            code_test_view, "ask_changed_outside", lambda _p, c=choice: asked.append(c) or c
        )
        saved = w._document_controller.on_save_all()
        assert saved is bool(choice)
    assert asked == ["", "overwrite"]  # Cancel wrote nothing, Overwrite wrote the editor's text
    assert _file(root).read_bytes() == (_CODE + "# mine\n").replace("\n", "\r\n").encode()
    _file(root).write_bytes(b"# outside again\r\n")
    view.editor.setPlainText("# mine again\n")
    monkeypatch.setattr(code_test_view, "ask_changed_outside", lambda _p: code_test_view.RELOAD)
    assert not view.save()
    assert view.editor.toPlainText() == "# outside again\n" and not view.dirty


def test_a_change_made_outside_studio_is_reloaded_when_nothing_is_edited(
    make_window, monkeypatch, tmp_path
):
    from PySide6.QtCore import Qt

    w, root = _opened(make_window, monkeypatch, tmp_path)
    _file(root).write_bytes(b"x = 1\r\n")
    w.code_view._on_app_state(Qt.ApplicationState.ApplicationActive)  # back from the editor
    assert w.code_view.editor.toPlainText() == "x = 1\n" and not w.code_view.dirty
    w.code_view.editor.setPlainText("x = 2\n")
    _file(root).write_bytes(b"x = 3\r\n")
    w.code_view._on_app_state(Qt.ApplicationState.ApplicationActive)
    assert w.code_view.editor.toPlainText() == "x = 2\n"  # edited here: kept, the save asks
    w.code_view.close()


def test_save_and_save_all_write_nothing_when_unmodified(make_window, monkeypatch, tmp_path):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    _file(root).write_bytes(b"# outside\n")  # not reloaded yet: no activation
    w._document_controller.on_save()
    assert w._document_controller.on_save_all()
    assert _file(root).read_bytes() == b"# outside\n"


def test_open_in_my_editor_saves_the_edits_first(make_window, monkeypatch, tmp_path):
    from procedo.studio.tabs import code_test_view

    opened = []
    monkeypatch.setattr(code_test_view, "open_in_editor", lambda path: opened.append(path) or "")
    w, root = _opened(make_window, monkeypatch, tmp_path)
    w.code_view.editor.setPlainText("y = 1\n")
    w.code_view._open_in_editor()
    assert _file(root).read_bytes() == b"y = 1\r\n" and opened == [_file(root)]


def test_the_file_comes_back_byte_for_byte(make_window, monkeypatch, tmp_path):
    """Its BOM, its non-breaking spaces; no Modified on opening it; a file the editor cannot
    give back (mixed line endings) is read-only."""
    w, root = _opened(make_window, monkeypatch, tmp_path)
    w.open_test(root / "tests" / "EPO")
    original = "\ufeffA = 'a\u00a0b'\r\nB = 2\r\n".encode()
    _file(root).write_bytes(original)
    w.open_test(root / "tests" / "AUX0_output")
    _settle()
    view = w.code_view
    assert not view.dirty and view.pill.text() == "✓ Saved" and not view.read_only
    view.editor.setPlainText(view.text() + "C = 3\n")
    assert view.save()
    assert _file(root).read_bytes() == original + b"C = 3\r\n"
    w.open_test(root / "tests" / "EPO")
    _file(root).write_bytes(b"A = 1\r\nB = 2\n")
    w.open_test(root / "tests" / "AUX0_output")
    assert view.read_only and view.editor.isReadOnly() and view.pill.text() == "Read-only"


def test_the_phases_follow_what_the_editor_shows(make_window, monkeypatch, tmp_path):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    w.code_view.editor.setPlainText(_CODE.replace("Power-up checks", "Edited"))
    _settle(lambda: _tree(w)[0] == "1 Edited")  # read from the editor, not the file
    assert b"Power-up checks" in _file(root).read_bytes()
    w.code_view.close()


def test_a_code_test_shows_the_assistants_note_and_saves_as_code(
    make_window, monkeypatch, tmp_path
):
    from procedo.studio import shell

    w, root = _opened(make_window, monkeypatch, tmp_path)
    assert w.assistant.currentIndex() == 1 and w.save_action.text() == "&Save openhtf/test.py"
    texts = [label.text() for label in w.assistant.currentWidget().findChildren(QLabel)]
    assert shell.CODE_ASSISTANT_NOTE in texts
    w.open_test(root / "tests" / "EPO")
    assert w.assistant.currentWidget() is w.dock and w.save_action.text().startswith("&Save Text")


def test_new_test_says_why_a_name_cannot_be_a_new_folder(qapp, tmp_path):
    from procedo.studio.dialogs.new_test_dialog import name_problem

    (tmp_path / "EPO").mkdir()
    cases = {
        "AUX1": "",
        "": "",
        "EPO": "A test named EPO already exists.",
        "a/b": 'A test name cannot hold < > : " / \\ | ? *.',
        "..\\x": 'A test name cannot hold < > : " / \\ | ? *.',
        "..": "A test name cannot end with a dot or a space.",
        "x.": "A test name cannot end with a dot or a space.",
    }
    assert {name: name_problem(name, tmp_path) for name in cases} == cases
    dialog = NewTestDialog(tmp_path)
    dialog.name_edit.setText("EPO")
    assert not dialog.create_button.isEnabled() and not dialog.problem.isHidden()
    dialog.name_edit.setText("AUX1")
    assert dialog.create_button.isEnabled() and dialog.problem.isHidden()
    dialog.deleteLater()
