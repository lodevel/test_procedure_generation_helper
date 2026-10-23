"""Studio's File › Export › Word and the project's evidence folder.

A report kept in the evidence exports folder (the dialog's default) is rendered into a
temporary file and handed to the evidence store — queued in this PC's outbox, then
pushed; a push that fails leaves it queued. Any other folder is a plain file write.
Nothing is ever written into the dropped in-project ``exports/`` tree.
"""
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from procedo.core import evidence_config, project_exports  # noqa: E402
from procedo.studio.dialogs import word_export_job  # noqa: E402

PID = "a3f19c2b-0000-4000-8000-000000000000"


def _project(tmp_path: Path, *, evidence_folder: bool = True) -> Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text(json.dumps({"project_id": PID}), encoding="utf-8")
    if evidence_folder:
        evidence_config.create_root(root, evidence_config.default_spec(root), PID)
    return root


def _export(monkeypatch, root: Path, output: Path, evidence_folder: Path | None) -> tuple[Path, list, list]:
    """Run one job with the render faked; ``(rendered path, answers, rendered paths)``."""
    rendered: list[Path] = []

    def fake_outcome(_root, _folders, out, *, sidecar, progress):
        out.write_bytes(b"DOCX")
        rendered.append(out)
        return {"path": out}

    monkeypatch.setattr(word_export_job, "export_outcome", fake_outcome)
    app = QApplication.instance() or QApplication([])
    window, answers = QWidget(), []
    word_export_job.WordExportJob(window, root, [root / "tests" / "T1"], output, {"title": "T"},
                                  lambda path, queued: answers.append((path, queued)),
                                  evidence_folder=evidence_folder)
    deadline = time.monotonic() + 10
    while not answers and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    window.deleteLater()
    return output, answers, rendered


def _pending_exports(root: Path) -> list[str]:
    with project_exports._project_store(root) as (store, pid):
        return [i.name for i in store.pending_items(pid) if i.artifact_kind == "export"]


def test_a_report_kept_in_the_evidence_folder_goes_through_the_store(tmp_path, monkeypatch):
    root = _project(tmp_path)
    folder = project_exports.exports_folder(root)
    out, answers, rendered = _export(monkeypatch, root, folder / "r.docx", folder)
    assert answers == [(out, False)]
    assert rendered[0] != out and not rendered[0].parent.exists()  # temporary, removed
    assert out.read_bytes() == b"DOCX" and _pending_exports(root) == []
    assert not (root / "exports").exists()


def test_an_unreachable_evidence_folder_leaves_the_report_queued(tmp_path, monkeypatch):
    root = _project(tmp_path, evidence_folder=False)  # the folder is missing: the push fails
    folder = project_exports.exports_folder(root)
    out, answers, rendered = _export(monkeypatch, root, folder / "r.docx", folder)
    assert answers == [(out, True)]
    assert not rendered[0].parent.exists() and not out.exists()
    assert _pending_exports(root) == ["r.docx"]
    assert not (root / "exports").exists()


def test_any_other_folder_is_a_plain_file(tmp_path, monkeypatch):
    root = _project(tmp_path)
    folder = project_exports.exports_folder(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out, answers, rendered = _export(monkeypatch, root, elsewhere / "r.docx", folder)
    assert answers == [(out, False)] and rendered == [out] and out.read_bytes() == b"DOCX"
    assert not (folder / "r.docx").exists() and _pending_exports(root) == []
