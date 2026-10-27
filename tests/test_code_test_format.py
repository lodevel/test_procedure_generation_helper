"""Studio and a test's code version (``openhtf/``, design openhtf_results_design.md slice 7).

Every text tool (the editors, Quick Parse / Code, Sync Equipment, review, the assistant) works on
the open text test: a code test opens in the code view instead (test_studio_code_test.py), and a
wizard refuses to write one (``code_test_refusal``). Its exports leave code tests out.
On a text test that also holds a code version, the text artifacts are the folder's own files and
nothing under ``openhtf/`` is read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

from procedo.studio.core.artifact_manager import ArtifactManager, ArtifactType


def _test(tmp_path: Path, active: str) -> Path:
    folder = tmp_path / "tests" / "AUX0_output"
    (folder / "openhtf").mkdir(parents=True)
    (folder / "test.json").write_text(json.dumps({"active": active}), encoding="utf-8")
    (folder / "procedure_text.md").write_text("# AUX0_output\n", encoding="utf-8")
    (folder / "procedure.json").write_text('{"id": "AUX0_output"}', encoding="utf-8")
    (folder / "test.py").write_text("ROOT = 1\n", encoding="utf-8")
    (folder / "openhtf" / "test.py").write_text("import openhtf as htf\n", encoding="utf-8")
    (folder / "openhtf" / "helpers.py").write_text("HELPER = 1\n", encoding="utf-8")
    return folder


def _code_files(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((folder / "openhtf").iterdir())}


def test_the_text_artifacts_of_a_test_with_a_code_version_are_the_folders_own(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])  # a save refreshes the media off the GUI thread
    folder = _test(tmp_path, "text")
    before = _code_files(folder)
    am = ArtifactManager()
    am.set_test_dir(folder)
    am.load_all()
    assert am.get_content(ArtifactType.TEST_CODE) == "ROOT = 1\n"  # never openhtf/test.py
    for kind, text in (
        (ArtifactType.PROCEDURE_TEXT, "# AUX0_output\n\n## Steps\n"),
        (ArtifactType.PROCEDURE_JSON, '{"id": "AUX0_output", "steps": []}'),
        (ArtifactType.TEST_CODE, "ROOT = 2\n"),
    ):
        am.set_content(kind, text)
    am.save_all()
    assert (folder / "test.py").read_text(encoding="utf-8") == "ROOT = 2\n"
    assert _code_files(folder) == before


def test_a_test_json_that_cannot_be_read_is_refused_saying_why(tmp_path):
    from procedo.studio.core.artifact_manager import code_test_refusal

    folder = _test(tmp_path, "text")
    (folder / "test.json").write_text("{", encoding="utf-8")
    assert code_test_refusal(folder).startswith(
        "Its test.json cannot be read (it is not JSON): it is taken as a code test.\n\n"
        "AUX0_output runs its code version (openhtf/test.py): the text tools leave it alone. "
        "To work on its text version, switch it to Text at the top of Studio."
    )


def test_the_exports_leave_code_tests_out(tmp_path):
    from PySide6.QtWidgets import QApplication

    from procedo.studio.core.project_manager import TestFolderInfo
    from procedo.studio.dialogs.test_selection_dialog import TestSelectionDialog

    QApplication.instance() or QApplication([])
    code, text = _test(tmp_path, "code"), tmp_path / "tests" / "EPO"
    text.mkdir()
    (text / "procedure_text.md").write_text("# EPO\n", encoding="utf-8")
    (text / "procedure.json").write_text("{}", encoding="utf-8")
    folders = [TestFolderInfo.of(code), TestFolderInfo.of(text)]
    assert [f.is_code for f in folders] == [True, False]
    for require in ("json", "text"):
        dialog = TestSelectionDialog(folders, require=require)
        assert [f.name for f in dialog.selected_folders()] == ["EPO"]
        dialog.deleteLater()
