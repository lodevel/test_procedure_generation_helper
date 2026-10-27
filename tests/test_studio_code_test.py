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

from PySide6.QtWidgets import QDialog
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

    def describe(root, test):
        if asked is not None:
            asked.append(("describe", threading.current_thread().name))
        return code_test.describe(*paths(root, test)), ""

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


def test_save_writes_the_file_its_line_endings_kept_and_reads_the_phases_again(
    make_window, monkeypatch, tmp_path
):
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
    assert _tree(w)[1] == "  1.1 Set 24 V" and view.pill.text() == "✓ Saved"


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
                        "units": "VOLT",
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
    """Windows' default app for .py is often the Python launcher: its Edit action opens it."""
    opened = []
    monkeypatch.setattr(opener.os, "startfile", lambda *a: opened.append(a), raising=False)
    path = tmp_path / "test.py"
    for app in (r"C:\Windows\py.exe", r"C:\Program Files\Microsoft VS Code\Code.exe"):
        monkeypatch.setattr(opener, "_default_app", lambda _p, app=app: app)
        assert opener.open_in_editor(path) == ""
    assert opened == [(str(path), "edit"), (str(path),)]


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
