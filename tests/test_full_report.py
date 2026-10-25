"""Editor full-report export reuses the shared procedo.core engine.

Renders a real .docx from a selected test folder via procedo.studio.core.full_report
-> procedo.core.report_export, with NO bundle (extraction returns empty, the doc
still renders the metadata). Proves the cross-tree shared-export plumbing.
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))  # procedo.core
sys.path.insert(0, str(_REPO / "external" / "test_procedure_generation_helper"))  # procedo.studio


def test_export_full_report_renders_docx(tmp_path):
    from docx import Document
    from procedo.studio.core import full_report
    from procedo.core import report_export

    # A template with Jinja placeholders the shared context fills.
    tpl = tmp_path / "tpl.docx"
    d = Document()
    d.add_paragraph("Report title: {{ title }}")
    d.add_paragraph("Test count: {{ tests|length }}")
    d.save(str(tpl))

    # A project with one test folder holding a procedure.json.
    proj = tmp_path / "proj"
    test_dir = proj / "tests" / "T1"
    test_dir.mkdir(parents=True)
    (test_dir / "procedure.json").write_text('{"meta": {"id": "T1"}}', encoding="utf-8")

    sidecar = report_export.create_default_sidecar()
    sidecar["title"] = "HELLO REPORT"
    out = tmp_path / "out.docx"

    result = full_report.export_full_report(
        proj, [test_dir], out, sidecar=sidecar, template_path=tpl
    )

    assert result == out and out.is_file()
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    assert "HELLO REPORT" in text  # metadata from the sidecar
    assert "Test count: 1" in text  # one procedure made it into the context


def test_export_full_report_no_tests_raises(tmp_path):
    from procedo.studio.core import full_report

    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        full_report.export_full_report(
            tmp_path, [empty], tmp_path / "o.docx", template_path=tmp_path / "missing.docx"
        )
        assert False, "expected FullReportError"
    except full_report.FullReportError:
        pass


@pytest.mark.parametrize("include", [False, True])
def test_board_images_are_generated_only_when_the_sidecar_includes_them(
    tmp_path, monkeypatch, include
):
    from docx import Document
    from procedo.studio.core import full_report
    from procedo.core import report_export

    tpl = tmp_path / "tpl.docx"
    Document().save(str(tpl))
    test_dir = tmp_path / "proj" / "tests" / "T1"
    test_dir.mkdir(parents=True)
    (test_dir / "procedure.json").write_text('{"meta": {"id": "T1"}}', encoding="utf-8")
    calls = []
    monkeypatch.setattr(full_report, "_generate_images", lambda *a: calls.append(a))

    sidecar = {**report_export.create_default_sidecar(), "include_images": include}
    full_report.export_full_report(
        tmp_path / "proj", [test_dir], tmp_path / "o.docx", sidecar=sidecar, template_path=tpl
    )
    assert len(calls) == int(include)


def test_the_word_export_asks_the_engine_only_off_the_gui_thread(tmp_path, monkeypatch):
    """File › Export › Word: rendered on a worker, so each test's meta and equipment are
    the project engine's answers (Studio has no display snapshot) and the GUI thread
    never asks the engine."""
    import json
    import threading
    import time

    from docx import Document
    from PySide6.QtWidgets import QApplication, QWidget

    from procedo.core import report_export
    from procedo.studio.dialogs.word_export_job import WordExportJob
    from tests import _env
    from procedo.engine_client import engine

    pytest.importorskip("rules_packager_base.rules.v2_0_2.extractors")
    app = QApplication.instance() or QApplication([])
    proj = tmp_path / "proj"
    (proj / "bundle").mkdir(parents=True)
    (proj / "bundle" / "defaults.json").write_text(
        json.dumps(
            {
                "extractors": {
                    key: {
                        "module": "rules_packager_base.rules.v2_0_2.extractors",
                        "function": function,
                    }
                    for key, function in (
                        ("meta", "extract_meta"),
                        ("equipment", "extract_equipment_requirements"),
                    )
                }
            }
        ),
        encoding="utf-8",
    )
    test_dir = proj / "tests" / "T1"
    test_dir.mkdir(parents=True)
    (test_dir / "procedure.json").write_text(
        json.dumps({"id": "T1", "equipment": [{"id": "PSU7", "type": "psu"}]}), encoding="utf-8"
    )
    tpl = tmp_path / "tpl.docx"
    d = Document()
    d.add_paragraph(
        "{% for t in tests %}[{{ t.id }}|{% for e in t.equipment %}{{ e.id }}{% endfor %}]{% endfor %}"
    )
    d.save(str(tpl))
    monkeypatch.setattr(report_export, "get_project_template", lambda **_kw: tpl)
    asked = []

    def tools_op(_root, spec, _timeout):  # the project's engine, answered here
        asked.append((spec.get("op"), threading.current_thread() is threading.main_thread()))
        if spec.get("op") != "extract":
            return {"ok": False, "error": "not in this test"}
        return _env.engine_answer(spec)

    monkeypatch.setattr(engine, "tools_op", tools_op)
    window, written = QWidget(), []
    out = tmp_path / "o.docx"
    WordExportJob(
        window,
        proj,
        [test_dir],
        out,
        report_export.create_default_sidecar(),
        lambda path, queued: written.append(path),
    )
    deadline = time.monotonic() + 20
    while not written and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert written == [out]
    assert ("extract", False) in asked and not [op for op, on_gui in asked if on_gui]
    assert "[T1|PSU7]" in "\n".join(p.text for p in Document(str(out)).paragraphs)
    window.deleteLater()
