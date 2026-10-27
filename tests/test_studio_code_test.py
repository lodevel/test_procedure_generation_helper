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
        found = code_test.check(*paths(root, test))  # its load: whether this venv has openhtf
        warnings = tuple(found["describe"]["warnings"]) if found["describe"] else ()
        return CodeCheck((), warnings, found["summary"], found["describe"])

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
        '<hr><p style="margin:0">Equipment: Dmm (DMM1), Psu (PSU1)</p>',
    ):
        assert part in html
    assert "Test points" not in html  # no phase names one


def test_the_export_preview_lists_the_test_points_the_phases_name_under_the_equipment():
    """Slice 15, board StudioCode: "Test points: TP 5V0" — each phase's media (describe), each once."""

    def media(*captions):
        return [{"type": "image", "ref": {"component": c}, "caption": c} for c in captions]

    described = {
        "phases": [
            {
                "name": "A",
                "file": "openhtf/test.py",
                "line": 1,
                "media": media("TP 5V0", "P15 pin 16"),
            },
            {"name": "B", "file": "openhtf/test.py", "line": 5, "media": media("TP 5V0")},
            {"name": "C", "file": "openhtf/test.py", "line": 9},
        ],
        "instruments": ["Psu"],
        "constants": [{"name": "PSU1_VISA"}],
    }
    assert preview_html(described).endswith(
        '<hr><p style="margin:0">Equipment: Psu (PSU1)<br>Test points: TP 5V0, P15 pin 16</p>'
    )
    assert preview_html({"phases": described["phases"]}).endswith(
        '<hr><p style="margin:0">Test points: TP 5V0, P15 pin 16</p>'
    )


def test_the_check_results_say_what_failed():
    assert check_lines(CodeCheck(("test.py line 2: '(' was never closed",), (), "", None)) == [
        ("danger", "✗ test.py line 2: '(' was never closed", "")
    ]
    lines = check_lines(CodeCheck(("does not load",), ("w1", "w2"), "1 step", {"phases": []}))
    assert [(tone, text) for tone, text, _tip in lines] == [
        ("success", "✓ Syntax OK"),
        ("danger", "✗ does not load"),
        ("success", "✓ 1 step"),
        ("warning", "⚠ w1"),
        ("warning", "⚠ w2"),
    ]


def test_open_in_my_editor_always_uses_the_edit_action(monkeypatch, tmp_path):
    """Never the default action, which may run the test (launcher, alias, wrapper, shim)."""
    opened = []
    monkeypatch.setattr(opener.os, "startfile", lambda *a: opened.append(a), raising=False)
    assert opener.open_in_editor(tmp_path / "test.py") == ""
    assert opened == [(str(tmp_path / "test.py"), "edit")]


def _no_edit_action(monkeypatch):
    def startfile(*args):
        assert args[1:] == ("edit",)  # never the default action
        raise OSError(1155, "No application is associated with the specified file")

    monkeypatch.setattr(opener.os, "startfile", startfile, raising=False)


def test_without_an_edit_action_the_file_opens_in_notepad(monkeypatch, tmp_path):
    _no_edit_action(monkeypatch)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    launched = []
    monkeypatch.setattr(opener.subprocess, "Popen", lambda argv: launched.append(argv))
    assert opener.open_in_editor(tmp_path / "test.py") == ""
    assert launched == [
        [str(Path(r"C:\Windows") / "System32" / "notepad.exe"), str(tmp_path / "test.py")]
    ]


def test_without_an_edit_action_or_notepad_the_file_is_refused(monkeypatch, tmp_path):
    _no_edit_action(monkeypatch)

    def popen(argv):
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(opener.subprocess, "Popen", popen)
    assert opener.open_in_editor(tmp_path / "test.py") == (
        "test.py could not be opened: The system cannot find the file specified. "
        "Choose an editor as the default app for .py files."
    )


def test_ctrl_f_in_the_code_view_opens_find_and_finds_text(make_window, monkeypatch, tmp_path):
    w, _ = _opened(make_window, monkeypatch, tmp_path)
    bar = w.code_view.find_bar
    w.code_view.editor.setPlainText("alpha\nbeta\ngamma\n")
    w._on_find()  # Edit › Find (Ctrl+F)
    assert not bar.isHidden()
    bar.find_field.setText("beta")
    assert bar.find_next()
    assert w.code_view.editor.textCursor().selectedText() == "beta"


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


def test_a_file_deleted_outside_studio_keeps_the_editors_text_to_save_back(
    make_window, monkeypatch, tmp_path, mbox
):
    from PySide6.QtCore import Qt

    w, root = _opened(make_window, monkeypatch, tmp_path)
    _file(root).unlink()
    w.code_view._on_app_state(Qt.ApplicationState.ApplicationActive)
    assert mbox.warning == [
        ("Changed outside Studio", "openhtf/test.py was deleted outside Studio.")
    ]
    assert w.code_view.editor.toPlainText() == _CODE and w.code_view.dirty
    w.code_view._on_app_state(Qt.ApplicationState.ApplicationActive)
    assert len(mbox.warning) == 1  # said once
    assert w.code_view.save()  # written back, unasked
    assert _file(root).read_bytes() == _CODE.replace("\n", "\r\n").encode()


def test_a_change_is_looked_for_by_time_and_size_before_the_bytes(monkeypatch, tmp_path):
    import os

    from procedo.studio.core.code_file import CodeFile

    path = tmp_path / "test.py"
    path.write_bytes(b"x = 1\n")
    code_file = CodeFile(path)
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    assert not code_file.changed_outside() and reads == []  # same time and size: not read
    os.utime(path, ns=(1, 1))
    assert not code_file.changed_outside() and reads == [path]  # touched, same bytes
    assert not code_file.changed_outside() and reads == [path]  # its new time is taken
    path.write_bytes(b"x = 2\n")
    assert code_file.changed_outside()


def test_the_checks_warnings_are_each_a_line_under_its_results(make_window, monkeypatch, tmp_path):
    w, _root = _opened(make_window, monkeypatch, tmp_path)
    w.code_view.editor.setPlainText(_CODE + "print('loaded')\n")
    w.code_view.check()
    _settle(lambda: w.code_view.result_texts())
    assert w.code_view.result_texts() == [
        "✓ Syntax OK",
        "✓ Loads in the bundle's environment",
        "✓ 2 steps · 1 measurement · 2 instruments",
        "⚠ openhtf/test.py runs code when it is loaded (line 21: print('loaded')): "
        "move it into a plug or a phase.",
    ]
