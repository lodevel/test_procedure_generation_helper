"""Studio and a test's code version (``openhtf/``, design openhtf_results_design.md slice 7).

Every text tool (the editors, Quick Parse / Code, Sync Equipment, review, the assistant) works on
the open test, so Studio does not open a code test (``procedo.core.test_format``): it says why.
On a text test that also holds a code version, the text artifacts are the folder's own files and
nothing under ``openhtf/`` is read or written.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from procedo.studio.controllers import document_controller
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


def test_studio_refuses_to_open_a_code_test_and_says_why(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda _p, title, text: shown.append((title, text)))
    )
    mw = SimpleNamespace(artifact_manager="kept")
    document_controller.DocumentController.on_test_opened(
        SimpleNamespace(_mw=mw), _test(tmp_path, "code")
    )
    assert shown == [
        (
            "Open Test",
            "AUX0_output runs its code version (openhtf/test.py): Studio does not edit code "
            'tests yet. To edit its text version, set "active": "text" in its test.json.',
        )
    ]
    assert mw.artifact_manager == "kept"  # the open test stays open: nothing was loaded


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
