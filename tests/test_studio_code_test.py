"""Studio and a code test (design openhtf_results_design.md slices 13-14; boards StudioCode,
StudioCompare, StudioNew, StudioPhases, StudioPanel).

A code test opens in the code view: its openhtf/test.py alone in the middle, the phase list the
engine's describe reads in the right panel's Phases tab (a click jumps to the line), the Check
(off the GUI thread); the header's Text | Code switch writes test.json; the Tests column shows
TXT / HTF; "+ New test…" makes a Python test from the starter; the shared phases show only while
code is open, each with its own Copy import and Edit, and a file of them opens in the same editor.

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
from procedo.studio.core.code_test_preview import (
    check_lines,
    equipment,
    phase_entries,
    pin_ranges,
)
from procedo.studio.dialogs.new_test_dialog import NewTestDialog

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

    def check_file(root, file):
        found = code_test.check_file(Path(root) / file, file)
        warnings = tuple(found["describe"]["warnings"]) if found["describe"] else ()
        return CodeCheck(tuple(found["problems"]), warnings, found["summary"], found["describe"])

    monkeypatch.setattr(code_test_check, "describe", describe)
    monkeypatch.setattr(code_test_check, "check", check)
    monkeypatch.setattr(
        code_test_check,
        "describe_phase_file",
        lambda root, file, source=None: (
            code_test.describe_file(Path(root) / file, file, source),
            "",
        ),
    )
    monkeypatch.setattr(code_test_check, "check_phase_file", check_file)
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


def _rows(w) -> list[tuple[str, str, list[str]]]:
    """The Phases tab as shown: each phase's title, its mark, its lines."""
    from procedo.studio.widgets.phases_panel import _Line

    out = []
    for row in w.code_view.phases.rows():
        title, *lines = [label.text() for label in row.findChildren(_Line)]
        out.append((title, row.entry.mark, lines))
    return out


def test_a_code_test_opens_in_the_code_view_with_the_phases_describe_reads(
    make_window, monkeypatch, tmp_path
):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    assert w.middle.currentWidget() is w.code_view and w.artifact_manager is None
    assert w.code_view.editor.toPlainText() == _CODE  # its CRLF shown as lines
    rows = _rows(w)  # open by default (owner): each one's details
    assert rows[0] == (
        "1 Power-up checks",
        "L12",
        [
            "Power the GCU at 28 V and check the 5 V rail.",
            "1.1 Set 28 V, limit 1 A",
            "1.2 Measure the 5 V rail on TP 5V0",
            "v5 4.75–5.25 V",
            "Test point: TP 5V0",
        ],
    )
    assert rows[1][:2] == ("2 Power off", "shared")
    w.code_view.phases.rows()[0].arrow.click()  # ▾ folds it into its summary
    _settle()
    assert _rows(w)[0] == ("1 Power-up checks", "L12", ["2 sub-steps · TP 5V0", "v5 4.75–5.25 V"])
    assert w.side_tabs.widget(1) is w.code_view.phases and w.phase_count.text() == "2"
    shown = [label.text() for label in w.code_view.phases.findChildren(QLabel)]
    assert not [t for t in shown if "Click" in t or "export" in t]  # no notes in the GUI
    assert w.header.title.text() == "AUX0_output" and w.header.subtitle.text().endswith("2 steps")
    assert w.artifact_status.isHidden() and not w.inactive_pill.isHidden()
    assert w.inactive_pill.text() == "Text version: inactive, not updated"
    assert not w.dock.isEnabled()  # the Assistant works on the text views


def _click(widget) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        widget.event(
            QMouseEvent(
                kind,
                QPointF(2, 2),
                QPointF(2, 2),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
    _settle()


def _line_of(w) -> int:
    return w.code_view.editor.textCursor().blockNumber() + 1


def test_clicking_a_phase_or_a_sub_step_jumps_to_its_line(make_window, monkeypatch, tmp_path):
    """Board StudioPanel A: a click on a phase opens it on its details and goes to its def; a
    click on a sub-step, to its line; ▾ folds it again."""
    from procedo.studio.widgets.phases_panel import _Line

    w, _root = _opened(make_window, monkeypatch, tmp_path)
    row = w.code_view.phases.rows()[0]
    _click(row)
    assert _line_of(w) == 12  # its def
    assert _rows(w)[0][2] == [
        "Power the GCU at 28 V and check the 5 V rail.",
        "1.1 Set 28 V, limit 1 A",
        "1.2 Measure the 5 V rail on TP 5V0",
        "v5 4.75–5.25 V",
        "Test point: TP 5V0",
    ]
    assert w.code_view.phases.rows()[0].property("selected") is True
    sub = [
        x for x in w.code_view.phases.rows()[0].findChildren(_Line) if x.text().startswith("1.2")
    ]
    _click(sub[0])
    assert _line_of(w) == 16
    w.code_view.phases.rows()[0].arrow.click()
    _settle()
    assert _rows(w)[0][2] == ["2 sub-steps · TP 5V0", "v5 4.75–5.25 V"]


def test_the_check_runs_off_the_gui_thread_and_shows_its_results_line(
    make_window, monkeypatch, tmp_path
):
    asked: list = []
    w, _root = _opened(make_window, monkeypatch, tmp_path, asked=asked)
    w.code_view.check()
    _settle(lambda: w.code_view.result_texts())
    assert [where for what, where in asked if what == "check"] == ["engine-call"]
    row = w.code_view.results.itemAt(0).widget()
    assert row.layout().hasHeightForWidth()  # the ✓ results wrap when the row is too narrow
    assert w.code_view.result_texts() == [
        "✓ Syntax OK",
        "✓ Loads in the bundle's environment",
        "✓ 2 steps · 1 measurement · 2 instruments",
    ]
    w.code_view.editor.insertPlainText("# edited\n")  # board StudioPanel B
    _settle(lambda: w.code_view.result_texts() == ["Not checked since the last edit"])
    w.code_view.editor.insertPlainText("# again\n")
    assert w.code_view.result_texts() == ["Not checked since the last edit"]


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
    _settle(lambda: w.code_view.phases.entries[0].substeps[0][0] == "Set 24 V")  # they follow
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


def test_the_right_panel_is_assistant_and_phases_and_folds_to_a_rail_remembered(
    make_window, monkeypatch, tmp_path
):
    """Board StudioPanel A-C: a code test's middle is its editor alone (no Code · Phases · Export
    preview switch, no side preview); the right panel's tabs are Assistant | Phases (its count),
    a text test's only Assistant; » folds it to a rail of one button per tab, remembered."""
    from PySide6.QtWidgets import QPushButton, QTabWidget

    from procedo.core import app_settings
    from procedo.ui.ui_kit import CollapsibleSidePane

    w, root = _opened(make_window, monkeypatch, tmp_path)
    view = w.code_view
    assert not view.findChildren(QTabWidget) and not hasattr(view, "preview")
    assert [w.side_tabs.tabText(i) for i in range(w.side_tabs.count())] == ["Assistant", "Phases"]
    assert w.side_tabs.isTabVisible(1) and not w.phase_count.isHidden()
    pane = w.findChild(CollapsibleSidePane)
    rail = [b for b in pane.findChildren(QPushButton) if b.objectName() == "railTab"]
    pane.set_collapsed(True)
    assert app_settings.get_side_pane_collapsed(app_settings.STUDIO_SIDE_PANE)
    assert [b.text() or b.toolTip() for b in rail if b.isVisibleTo(pane)] == ["Assistant", "Phases"]
    rail[1].click()
    assert not pane.is_collapsed() and w.side_tabs.currentIndex() == 1
    w.open_test(root / "tests" / "EPO")
    _settle()
    assert not w.side_tabs.isTabVisible(1) and w.side_tabs.currentIndex() == 0
    assert [b.toolTip() for b in rail if not b.isHidden()] == ["Assistant"]
    assert not app_settings.get_side_pane_collapsed(app_settings.STUDIO_SIDE_PANE)


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


def test_the_phases_tab_lists_each_phase_folded_or_open_and_the_equipment():
    """Board StudioPanel A: one entry per phase from describe — its questions, sub-steps,
    measurements and limits, test points; a shared phase of the teardown marked."""

    def media(*captions):
        return [{"type": "image", "ref": {"component": c}, "caption": c} for c in captions]

    described = {
        "phases": [
            {
                "name": "Power-up checks",
                "file": "openhtf/test.py",
                "line": 9,
                "description": "Power the GCU at 28 V.",
                "questions": [{"text": "Plug J2, then press Enter.", "line": 10}],
                "substeps": [{"text": "Set 28 V", "line": 11}],
                "media": media("J2 pins 1–10", "TP 5V0"),
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
                "function": "power_off",
                "file": "functions/common_phases.py",
                "line": 4,
                "description": "Switch every output off.",
                "substeps": [],
                "measurements": [],
                "teardown": True,
            },
        ],
        "instruments": ["Dmm", "Psu"],
        "constants": [{"name": "PSU1_VISA"}, {"name": "DMM1_VISA"}],
    }
    up, off = phase_entries(described)
    assert (up.mark, up.summary()) == (
        "L9",
        ["1 sub-step · 1 question · J2 pins 1–10, TP 5V0", "v5 4.75–5.25 V"],
    )
    assert [text for _kind, text, _line in up.details()] == [
        "Power the GCU at 28 V.",
        "? “Plug J2, then press Enter.”",
        "1.1 Set 28 V",
        "v5 4.75–5.25 V",
        "Test points: J2 pins 1–10, TP 5V0",
    ]
    assert (off.mark, off.here, off.summary()) == (
        "shared · teardown",
        False,
        ["common_phases.py, line 4 (opens the file)"],
    )
    assert equipment(described) == "Equipment: Dmm (DMM1), Psu (PSU1)"
    from procedo.studio.core.code_test_preview import limits

    assert limits({"lower": 5, "upper": 5, "units": "V"}) == "= 5 V"
    pins = [f"J2 pin {n}" for n in (6, 7, 8, 9, 10, 1, 2, 3, 4, 5)]
    assert pin_ranges(["TP 5V0", *pins, "P15 pin 29", "P15 pin 27", "P15 pin 28", "D3"]) == (
        "TP 5V0",
        "J2 pins 1–10",
        "P15 pins 27–29",
        "D3",
    )
    assert pin_ranges(["P15 pin 29", "P15 pin 31", "P15 pin 29"]) == ("P15 pin 29", "P15 pin 31")
    (alone,) = phase_entries(
        {"phases": [{**described["phases"][0], "media": media("P15 pin 29", "P15 pin 30")}]}
    )
    assert alone.details()[-1][1] == "Test points: P15 pins 29–30"
    assert equipment({"phases": []}) == ""
    # a file of shared phases: each phase's tests
    mine = phase_entries(
        {"phases": described["phases"][1:]},
        "functions/common_phases.py",
        {("functions/common_phases.py", "power_off"): ["AUX0_output"]},
    )
    assert (mine[0].mark, mine[0].summary()) == ("L4", ["used by AUX0_output"])
    assert mine[0].details()[-1][1] == "no sub-steps yet · used by AUX0_output"
    assert phase_entries({"phases": described["phases"][1:]}, "functions/common_phases.py", {})[
        0
    ].summary() == ["used by no test"]


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
    found = CodeCheck((), (), "1 step", {"phases": []})
    assert [text for _tone, text, _tip in check_lines(found, loads=False)] == [
        "✓ Syntax OK",
        "✓ 1 step",  # a file of shared phases: its tests load it
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
            code_test_view, "ask_changed_outside", lambda _p, _f, c=choice: asked.append(c) or c
        )
        saved = w._document_controller.on_save_all()
        assert saved is bool(choice)
    assert asked == ["", "overwrite"]  # Cancel wrote nothing, Overwrite wrote the editor's text
    assert _file(root).read_bytes() == (_CODE + "# mine\n").replace("\n", "\r\n").encode()
    _file(root).write_bytes(b"# outside again\r\n")
    view.editor.setPlainText("# mine again\n")
    monkeypatch.setattr(code_test_view, "ask_changed_outside", lambda *_: code_test_view.RELOAD)
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
    _settle(lambda: _rows(w)[0][0] == "1 Edited")  # read from the editor, not the file
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


def _shared_row(w):
    (row,) = w.workspace_widget.shared_phases.rows()
    return row


def test_a_shared_phase_row_shows_what_a_test_writes_and_has_its_own_buttons(
    make_window, monkeypatch, tmp_path
):
    """Board StudioPanel D: name, ``power_off()``, description, file · used by N; its own
    "⧉ Copy import" (the real line, in its tooltip; "✓ Copied" for a moment) and "✎ Edit"; the
    two bottom buttons are gone, "+ New shared phase…" is under the list."""
    from PySide6.QtWidgets import QApplication, QPushButton

    from procedo.studio.widgets import shared_phases_panel

    w, _root = _opened(make_window, monkeypatch, tmp_path)
    panel, row = w.workspace_widget.shared_phases, _shared_row(w)
    assert [label.text() for label in row.findChildren(QLabel)] == [
        "Power off",
        "power_off()",
        "Switch every output off.",
        "functions/common_phases.py · used by 1 test",
    ]
    assert [b.text() for b in panel.findChildren(QPushButton) if b.isVisibleTo(panel)] == [
        "⧉ Copy import",
        "✎ Edit",
        "+ New shared phase…",
    ]
    assert row.copy_button.toolTip() == "Copies: from common_phases import power_off"
    monkeypatch.setattr(shared_phases_panel, "COPIED_MS", 20)
    row.copy_button.click()
    assert QApplication.clipboard().text() == "from common_phases import power_off"
    assert row.copy_button.text() == "✓ Copied"
    _settle(lambda: row.copy_button.text() == "⧉ Copy import")


def test_edit_opens_the_shared_phase_file_in_studios_own_editor(
    make_window, monkeypatch, tmp_path, mbox
):
    """Board StudioPanel E: the file in the middle like a test — its header, Save, Check (no
    load: its tests load it) — and its phases, with the tests that use each, in the Phases tab."""
    from PySide6.QtWidgets import QMessageBox

    w, root = _opened(make_window, monkeypatch, tmp_path)
    shared = root / "functions" / "common_phases.py"
    _shared_row(w).edit_button.click()
    _settle()
    view = w.code_view
    assert view.phase_file == shared and view.test_dir is None
    assert view.editor.toPlainText() == _SHARED and _line_of(w) == 5
    assert w.header.title.text() == "functions/common_phases.py"
    assert w.header.subtitle.text().endswith("shared phases")
    assert w.used_by_pill.text() == "used by 1 test" and w.used_by_pill.isVisibleTo(w)
    assert not w.version_switch.isVisibleTo(w) and w.middle.currentWidget() is view
    assert view.file_label.text() == "functions/common_phases.py"
    assert w.save_action.text() == "&Save functions/common_phases.py"
    assert _rows(w) == [
        (
            "1 Power off",
            "L5",
            ["Switch every output off.", "no sub-steps yet · used by AUX0_output"],
        )
    ]
    assert _shared_row(w).property("selected") is True
    assert w.workspace_widget._current_opened_test is None
    view.check()
    _settle(lambda: view.result_texts())
    assert view.result_texts() == ["✓ Syntax OK", "✓ 1 step · 0 measurements · 0 instruments"]
    view.editor.setPlainText(_SHARED + "# edited\n")
    mbox.question_answer = QMessageBox.StandardButton.Cancel
    w.open_test(root / "tests" / "EPO")
    assert [text for _title, text in mbox.question] == [
        "You have unsaved changes in:\n  • functions/common_phases.py\n\nSave before continuing?"
    ]
    assert view.phase_file == shared
    assert w._document_controller.on_save_all()
    assert shared.read_text(encoding="utf-8") == _SHARED + "# edited\n"


def test_a_shared_phase_clicked_in_the_tests_phases_opens_its_file(
    make_window, monkeypatch, tmp_path
):
    w, root = _opened(make_window, monkeypatch, tmp_path)
    _click(w.code_view.phases.rows()[1])  # 2 Power off, shared
    assert w.code_view.phase_file == root / "functions" / "common_phases.py"
    assert _line_of(w) == 5 and _shared_row(w).property("selected") is True
    w.open_test(root / "tests" / "AUX0_output")
    _settle()
    assert w.code_view.test_dir is not None and _shared_row(w).property("selected") is False


def test_new_shared_phase_asks_its_name_function_and_file(qapp):
    """Board StudioPanel F: the function follows the name until edited; one the file already
    has, or a new file that exists, is refused under its field."""
    from procedo.studio.dialogs.new_phase_dialog import NewPhaseDialog

    dialog = NewPhaseDialog({"common_phases.py": _SHARED, "power.py": ""}, "common_phases.py")
    assert dialog.existing.isChecked() and not dialog.create_button.isEnabled()
    dialog.name_edit.setText("Discharge the bus")
    assert dialog.function == "discharge_the_bus" and dialog.create_button.isEnabled()
    assert dialog.function_note.text() == (
        "Filled from the name; what a test writes: from common_phases import discharge_the_bus"
    )
    for function in ("power_off", "helper"):
        dialog.function_edit.setText(function)
        assert dialog.function_problem.text() == f"{function} is already in common_phases.py."
        assert not dialog.function_problem.isHidden() and not dialog.create_button.isEnabled()
    dialog.function_edit.setText("discharge_bus")
    dialog.name_edit.setText("Discharge the bus now")
    assert dialog.function == "discharge_bus"  # edited by hand: it no longer follows
    assert dialog.create_button.isEnabled() and dialog.file == "common_phases.py"
    dialog.new.setChecked(True)
    dialog.new_edit.setText("power")
    assert dialog.is_new and dialog.new_problem.text() == "functions/power.py already exists."
    assert not dialog.create_button.isEnabled()
    dialog.new_edit.setText("Power")
    assert dialog.new_problem.text() == "functions/Power.py already exists."  # Windows: one file
    dialog.new_edit.setText("bus_phases")
    assert dialog.file == "bus_phases.py" and dialog.create_button.isEnabled()
    assert dialog.new_edit.placeholderText() == ""
    dialog.deleteLater()
    mixed = NewPhaseDialog({"mixed.py": ""}, "mixed.py", {"mixed.py"})
    mixed.name_edit.setText("A")
    assert not mixed.create_button.isEnabled() and mixed.new_problem.text() == (
        "Studio cannot keep functions/mixed.py exactly as it is (mixed line endings or special "
        "characters): edit it in your editor."
    )
    mixed.deleteLater()


def _new_phase(monkeypatch, name, function, new_file=""):
    from procedo.studio.dialogs.new_phase_dialog import NewPhaseDialog

    def chosen(dialog):
        dialog.name_edit.setText(name)
        dialog.function_edit.setText(function)
        if new_file:
            dialog.new.setChecked(True)
            dialog.new_edit.setText(new_file)
        return QDialog.DialogCode.Accepted if dialog.create_button.isEnabled() else 0

    monkeypatch.setattr(NewPhaseDialog, "exec", chosen)


def test_create_appends_a_starter_phase_in_the_editor_the_cursor_in_its_body(
    make_window, monkeypatch, tmp_path
):
    """Nothing is written until Save: the file shows Modified; a new file starts with the usual
    imports."""
    from procedo.core import starter_test

    w, root = _opened(make_window, monkeypatch, tmp_path)
    view, shared = w.code_view, root / "functions" / "common_phases.py"
    _new_phase(monkeypatch, "Discharge the bus", "discharge_bus")
    w.workspace_widget.shared_phases.new_button.click()
    _settle()
    starter = starter_test.starter_phase("Discharge the bus", "discharge_bus")
    assert view.phase_file == shared and view.text() == _SHARED + starter
    assert view.dirty and view.pill.text() == "● Modified"
    assert shared.read_text(encoding="utf-8") == _SHARED
    cursor = view.editor.textCursor()
    assert cursor.atEnd() and cursor.block().text() == "    "  # in its body
    _settle(lambda: [e.name for e in view.phases.entries] == ["Power off", "Discharge the bus"])
    assert view.save()
    _new_phase(monkeypatch, "Bus off", "bus_off", new_file="bus_phases")
    w.workspace_widget.shared_phases.new_button.click()
    _settle()
    made = root / "functions" / "bus_phases.py"
    assert view.phase_file == made and not made.exists()  # written on Save
    assert view.text() == starter_test.PHASE_FILE_HEAD + starter_test.starter_phase(
        "Bus off", "bus_off"
    )
    assert view.dirty and view.save()
    assert made.read_text(encoding="utf-8") == view.text()


def test_a_read_only_file_of_phases_is_never_written(make_window, monkeypatch, tmp_path, mbox):
    """Mixed line endings: Studio cannot give it back. "+ New shared phase…" refuses it; text put
    in its editor all the same is never saved (Save off, a save refused)."""
    from procedo.studio.dialogs.new_phase_dialog import NewPhaseDialog

    w, root = _opened(make_window, monkeypatch, tmp_path)
    mixed = root / "functions" / "mixed.py"
    original = b"import openhtf as htf\r\nx = 1\n"
    mixed.write_bytes(original)
    seen = []

    def chosen(dialog):
        dialog.file_combo.setCurrentText("functions/mixed.py")
        dialog.name_edit.setText("A")
        seen.append((dialog.create_button.isEnabled(), dialog.new_problem.isHidden()))
        return 0

    monkeypatch.setattr(NewPhaseDialog, "exec", chosen)
    w.workspace_widget.shared_phases.new_button.click()
    assert seen == [(False, False)]
    w._code_test_controller.open_phase_file(mixed)
    _settle()
    view = w.code_view
    assert view.read_only and view.pill.text() == "Read-only"
    view.append("y = 2\n")  # QTextCursor writes into a read-only editor
    assert view.dirty and not view.save_button.isEnabled()
    assert not view.save() and mixed.read_bytes() == original
    assert mbox.warning[-1][0] == "Save Failed"
    view.close()


def test_a_phase_of_the_tests_own_folder_opens_at_its_real_path(
    make_window, monkeypatch, tmp_path, mbox
):
    """A phase from openhtf/helpers.py opens tests/<test>/openhtf/helpers.py (named so), one of
    functions/pkg/ is named by its path; a file that is not there never opens."""
    w, root = _opened(make_window, monkeypatch, tmp_path)
    test_dir = root / "tests" / "AUX0_output"
    (test_dir / "openhtf" / "helpers.py").write_text(
        "import openhtf as htf\n\n\n@htf.PhaseOptions(name='Settle')\ndef settle(test):\n    pass\n",
        encoding="utf-8",
    )
    test_py = test_dir / "openhtf" / "test.py"
    test_py.write_bytes(
        _CODE.replace(
            "from common_phases import power_off",
            "from common_phases import power_off\nfrom helpers import settle",
        )
        .replace("htf.Test(power_up, power_off)", "htf.Test(power_up, settle, power_off)")
        .encode()
    )
    w.code_view.reload()
    _settle(lambda: len(w.code_view.phases.entries) == 3)
    settle = w.code_view.phases.rows()[1]
    assert settle.entry.file == "openhtf/helpers.py" and settle.entry.mark == ""
    _click(settle)
    view = w.code_view
    assert view.phase_file == test_dir / "openhtf" / "helpers.py" and not view.shared_file
    assert view.name == "tests/AUX0_output/openhtf/helpers.py" and _line_of(w) == 5
    assert w.header.title.text() == view.name and not w.used_by_pill.isVisibleTo(w)
    _settle(lambda: _rows(w) == [("1 Settle", "L5", [])])
    view.check()
    _settle(lambda: view.result_texts())
    assert view.result_texts() == ["✓ Syntax OK", "✓ 1 step · 0 measurements · 0 instruments"]
    w.open_test(test_dir)
    _settle()
    (root / "functions" / "pkg").mkdir()
    (root / "functions" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    view._open_shared("functions/pkg/mod.py", "f", 1)
    _settle()
    assert view.name == "functions/pkg/mod.py" and not view.shared_file
    w.open_test(test_dir)
    _settle()
    view._open_shared("functions/gone.py", "f", 1)
    assert view.test_dir == test_dir
    assert mbox.warning[-1] == (
        "Open File",
        f"functions/gone.py is not there: {root / 'functions' / 'gone.py'}",
    )


def test_a_starter_appended_to_a_file_without_the_openhtf_import_adds_it(
    make_window, monkeypatch, tmp_path
):
    from procedo.core import starter_test

    w, root = _opened(make_window, monkeypatch, tmp_path)
    plain = root / "functions" / "plain.py"
    plain.write_bytes(b'\xef\xbb\xbf"""Doc."""\nimport os\n')  # its BOM: read as UTF-8-sig

    from procedo.studio.dialogs.new_phase_dialog import NewPhaseDialog

    def chosen(dialog):
        dialog.file_combo.setCurrentText("functions/plain.py")
        dialog.name_edit.setText("Décharge")
        return QDialog.DialogCode.Accepted if dialog.create_button.isEnabled() else 0

    monkeypatch.setattr(NewPhaseDialog, "exec", chosen)
    w.workspace_widget.shared_phases.new_button.click()
    _settle()
    assert w.code_view.text() == (
        '"""Doc."""\nimport openhtf as htf\nimport os\n'
        + starter_test.starter_phase("Décharge", "decharge")
    )
