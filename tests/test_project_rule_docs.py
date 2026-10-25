"""Project rule documents (docs/plans/project_rules_scope.md): ``<project>/config/project_rules/``
holds .md/.txt documents sent ON TOP of the bundle rules, keyed ``project/<name>`` so a bundle
rule of the same name never clashes; "all" expands to both; the LLM context sends them after
the bundle rules under a header saying they win; the picker adds one by copying it in (never
over an existing name, refusing above 64 KB, warning above 16 KB) and ticks it in every
selection."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from procedo.studio.core import fileio
from procedo.studio.core.project_manager import ProjectManager, RuleDocError
from procedo.studio.core.task_config import TaskConfigManager
from procedo.studio.llm import LLMTask, TabContext
from procedo.studio.core.artifact_manager import ArtifactManager
from procedo.studio.widgets.rule_selector import RuleSelectorDialog

from .test_main_window_characterization import qapp as qapp

TABS = ("text_json", "json_code")


def _project(tmp_path, *, bundle_rules=("01_a.md", "02_b.md"), project_docs=()):
    """A project; *bundle_rules* in its active bundle's rules, *project_docs* (name → text, or
    names) in config/project_rules/."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "config").mkdir()
    if bundle_rules is not None:
        (root / "bundle" / "rules").mkdir(parents=True)
        (root / "bundle" / "bundle.json").write_text(
            json.dumps({"id": "baseline", "version": "2.10.0"}), encoding="utf-8"
        )
        (root / "bundle" / "defaults.json").write_text(
            json.dumps(
                {
                    "workflows": {  # runnable tabs
                        tab: {"tasks": [{"id": task, "prompt_template": "P"}]}
                        for tab, task in (
                            ("text_only", "review_text_procedure"),
                            ("text_json", "derive_json_from_text"),
                            ("json_code", "generate_code_from_json"),
                        )
                    }
                }
            ),
            encoding="utf-8",
        )
        for name in bundle_rules:
            (root / "bundle" / "rules" / name).write_text(f"bundle {name}", encoding="utf-8")
    docs = (
        project_docs
        if isinstance(project_docs, dict)
        else {n: f"project {n}" for n in project_docs}
    )
    for name, text in docs.items():
        (root / "config" / "project_rules").mkdir(exist_ok=True)
        (root / "config" / "project_rules" / name).write_text(text, encoding="utf-8")
    return root


def _pm(root):
    pm = ProjectManager()
    assert pm.set_project_root(root)
    pm.detect_rules_root()
    return pm


def _tcm(root, selections=None):
    """The project's TaskConfigManager; *selections* tab → selected_rules for every task."""
    tcm = TaskConfigManager(root / "absent_fallback.json", root)
    for tab, selected in (selections or {}).items():
        tcm.set_selected_rules_for_tab(tab, selected)
    return tcm


def _context(root, pm, tcm, tab="text_json"):
    return TabContext(
        tab_id=tab,
        backend_factory=None,
        project_manager=pm,
        artifact_manager=ArtifactManager(),
        task_config_manager=tcm,
    )


# ---- S1: keys, expansion, LLM content ------------------------------------------------------


def test_rule_docs_are_the_bundle_rules_then_the_project_documents_by_key(tmp_path):
    root = _project(tmp_path, project_docs=("zz.md", "notes.txt", "skip.docx", "b.pdf"))
    pm = _pm(root)
    rules, own = root / "bundle" / "rules", root / "config" / "project_rules"

    assert pm.get_rule_docs() == [
        ("01_a.md", rules / "01_a.md", "bundle"),
        ("02_b.md", rules / "02_b.md", "bundle"),
        ("project/b.pdf", own / "b.pdf", "project"),
        ("project/notes.txt", own / "notes.txt", "project"),
        ("project/zz.md", own / "zz.md", "project"),
    ]


def test_a_project_document_named_like_a_bundle_rule_has_its_own_key(tmp_path):
    root = _project(tmp_path, project_docs={"01_a.md": "project says otherwise"})
    pm = _pm(root)
    tcm = _tcm(root, {"text_json": ["project/01_a.md"]})

    assert [d.key for d in pm.get_rule_docs()] == ["01_a.md", "02_b.md", "project/01_a.md"]
    content = _context(root, pm, tcm).get_selected_rules_content()
    assert "project says otherwise" in content and "bundle 01_a.md" not in content


def test_the_documents_are_scanned_on_every_call(tmp_path):
    root = _project(tmp_path)
    pm = _pm(root)
    assert [d.origin for d in pm.get_rule_docs()] == ["bundle", "bundle"]

    (root / "config" / "project_rules").mkdir()
    (root / "config" / "project_rules" / "new.md").write_text("x", encoding="utf-8")
    assert pm.get_rule_docs()[-1].key == "project/new.md"


def test_a_picked_rules_folder_replaces_the_bundle_rules_not_the_project_documents(tmp_path):
    root = _project(tmp_path, project_docs=("own.md",))
    (root / "picked").mkdir()
    (root / "picked" / "p.md").write_text("picked", encoding="utf-8")
    pm = _pm(root)
    assert pm.set_rules_root(root / "picked")

    assert [(d.key, d.origin) for d in pm.get_rule_docs()] == [
        ("p.md", "bundle"),
        ("project/own.md", "project"),
    ]


@pytest.mark.parametrize("raw", [None, "all"])
def test_all_selects_the_bundle_rules_and_the_project_documents(tmp_path, raw):
    root = _project(tmp_path, project_docs=("own.md",))
    pm = _pm(root)
    tcm = _tcm(root, {"text_json": raw})
    context = _context(root, pm, tcm)

    assert context.get_selected_rules() == ["01_a.md", "02_b.md", "project/own.md"]
    content = context.get_selected_rules_content()
    assert all(
        f"{who} {name}" in content
        for who, name in (("bundle", "01_a.md"), ("bundle", "02_b.md"), ("project", "own.md"))
    )


def test_project_documents_come_after_the_bundle_rules_under_a_header_saying_they_win(tmp_path):
    root = _project(tmp_path, project_docs=("own.md", "more.txt"))
    pm = _pm(root)
    # The list's order is not the content's: bundle rules first, then project documents.
    tcm = _tcm(root, {"text_json": ["project/own.md", "02_b.md", "project/more.txt"]})

    content = _context(root, pm, tcm).get_selected_rules_content()

    order = [
        content.index(s)
        for s in (
            "# Rules from: 02_b.md",
            "bundle 02_b.md",
            "project rules win",
            "# Project rules from: more.txt",
            "project more.txt",
            "# Project rules from: own.md",
            "project own.md",
        )
    ]
    assert order == sorted(order)
    assert content.count("project rules win") == 1
    assert "01_a.md" not in content


def test_project_documents_reach_the_llm_without_a_bundle(tmp_path):
    root = _project(tmp_path, bundle_rules=None, project_docs=("own.md",))
    pm = _pm(root)
    assert pm.rules_root is None

    context = _context(root, pm, _tcm(root))
    assert context.get_selected_rules() == ["project/own.md"]
    assert "project own.md" in context.get_selected_rules_content()


def test_no_bundle_and_no_project_documents_send_nothing(tmp_path):
    root = _project(tmp_path, bundle_rules=None)
    pm = _pm(root)
    assert _context(root, pm, _tcm(root)).get_selected_rules_content() is None


def test_an_edited_project_document_changes_the_rules_checksum(tmp_path):
    root = _project(tmp_path, project_docs=("own.md",))
    context = _context(root, _pm(root), _tcm(root))
    context._update_rules_checksum(context.get_selected_rules_content())

    (root / "config" / "project_rules" / "own.md").write_text("changed", encoding="utf-8")
    assert context._has_rules_changed(context.get_selected_rules_content())


def test_a_byte_order_mark_is_not_sent(tmp_path):
    root = _project(tmp_path, bundle_rules=None)
    (root / "config" / "project_rules").mkdir()
    (root / "config" / "project_rules" / "bom.md").write_bytes("\ufeff# Board".encode("utf-8"))
    content = _context(root, _pm(root), _tcm(root)).get_selected_rules_content()
    assert "\ufeff" not in content and "\n# Board" in content


def test_a_selected_document_no_longer_there_is_skipped(tmp_path):
    root = _project(tmp_path)
    tcm = _tcm(root, {"text_json": ["project/gone.md", "01_a.md"]})
    assert (
        _context(root, _pm(root), tcm)
        .get_selected_rules_content()
        .strip()
        .endswith("bundle 01_a.md")
    )


# ---- S2: adding a document ------------------------------------------------------------------


def _source(tmp_path, name, data: bytes) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


def test_adding_copies_the_document_verbatim_creating_the_folder(tmp_path):
    root = _project(tmp_path)
    pm = _pm(root)
    data = "# Board\r\nGPO 3 is inverted. – µ\n".encode("utf-8")

    doc = pm.add_project_rule_doc(_source(tmp_path, "board.md", data))

    target = root / "config" / "project_rules" / "board.md"
    assert doc == ("project/board.md", target, "project")
    assert target.read_bytes() == data
    assert sorted(p.name for p in target.parent.iterdir()) == ["board.md"]  # no temp left


def test_adding_never_overwrites_an_existing_name(tmp_path):
    root = _project(tmp_path, project_docs={"board.md": "mine"})
    pm = _pm(root)

    with pytest.raises(RuleDocError, match="already"):
        pm.add_project_rule_doc(_source(tmp_path, "board.md", b"theirs"))
    folder = root / "config" / "project_rules"
    assert (folder / "board.md").read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in folder.iterdir()) == ["board.md"]


def test_the_atomic_create_refuses_an_existing_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        fileio.atomic_create(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.md"]


@pytest.mark.skipif(os.name != "nt", reason="os.rename refuses an existing target on Windows")
def test_a_file_appearing_after_the_check_is_not_overwritten_either(tmp_path, monkeypatch):
    target = tmp_path / "f.md"
    target.write_bytes(b"old")
    monkeypatch.setattr(Path, "exists", lambda self: False)  # it appeared after the check
    with pytest.raises(FileExistsError):
        fileio.atomic_create(target, b"new")
    monkeypatch.undo()
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.md"]  # the temp file went


@pytest.mark.parametrize(
    "name,data,match",
    [
        ("big.md", b"x" * (64 * 1024 + 1), "64 KB"),
        ("manual.docx", b"PK", "PDF"),
        ("latin.txt", "café".encode("latin-1"), "UTF-8"),
    ],
    ids=["over-64-KB", "docx", "not-utf8"],
)
def test_adding_refuses_too_big_a_pdf_or_non_utf8(tmp_path, name, data, match):
    root = _project(tmp_path)
    pm = _pm(root)
    with pytest.raises(RuleDocError, match=match):
        pm.add_project_rule_doc(_source(tmp_path, name, data))
    assert not (root / "config" / "project_rules" / name).exists()


def test_a_pdf_is_added_as_it_is_whatever_its_size(tmp_path):
    root = _project(tmp_path)
    data = b"%PDF-1.7\n\xff\xfe" + b"z" * (200 * 1024)  # not text, over 64 KB
    doc = _pm(root).add_project_rule_doc(_source(tmp_path, "Board.PDF", data))
    assert doc.key == "project/Board.PDF" and doc.path.read_bytes() == data


def test_exactly_64_kb_is_accepted(tmp_path):
    root = _project(tmp_path)
    doc = _pm(root).add_project_rule_doc(_source(tmp_path, "edge.txt", b"x" * (64 * 1024)))
    assert doc.path.stat().st_size == 64 * 1024


def test_a_new_document_is_ticked_in_every_explicit_selection_and_saved(tmp_path):
    root = _project(tmp_path)
    # text_only sends no rules on purpose ([]): it stays at none (owner decision).
    tcm = _tcm(root, {"text_json": ["01_a.md"], "json_code": "all", "text_only": []})
    tcm.save_config()

    tcm.add_rule_to_selections("project/board.md")
    tcm.add_rule_to_selections("project/board.md")  # once only
    tcm.save_config()

    assert all(
        t.selected_rules == ["01_a.md", "project/board.md"]
        for t in tcm.get_all_tasks_for_tab("text_json")
    )
    assert all(t.selected_rules == "all" for t in tcm.get_all_tasks_for_tab("json_code"))
    assert all(t.selected_rules == [] for t in tcm.get_all_tasks_for_tab("text_only"))
    saved = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    # Saved: the bundle's task only (the editor-only ones stay inherited).
    assert {t["id"]: t["selected_rules"] for t in saved["workflows"]["text_json"]["tasks"]} == {
        "derive_json_from_text": ["01_a.md", "project/board.md"]
    }


# ---- S7: PDF project documents ride the message as files ---------------------------------


def _pdf_project(tmp_path, selected):
    root = _project(tmp_path, project_docs=("own.md",))
    (root / "config" / "project_rules" / "board.pdf").write_bytes(b"%PDF-1.7 board")
    pm = _pm(root)
    tcm = _tcm(root, {"text_json": selected})
    return root, _context(root, pm, tcm)


def test_a_selected_pdf_is_attached_and_named_in_the_rules_text(tmp_path):
    root, context = _pdf_project(tmp_path, "all")
    pdf = root / "config" / "project_rules" / "board.pdf"

    request = context._build_request(LLMTask.AD_HOC_CHAT)

    assert request.attachments == [pdf]
    assert "%PDF" not in request.rules_content  # sent as a file, never as text
    assert "# Project rules from: board.pdf" in request.rules_content
    assert "attached" in request.rules_content.split("# Project rules from:")[0]


def test_without_a_selected_pdf_the_request_attaches_nothing(tmp_path):
    root, context = _pdf_project(tmp_path, ["01_a.md", "project/own.md"])
    request = context._build_request(LLMTask.AD_HOC_CHAT)
    assert request.attachments == []
    assert "attached" not in request.rules_content


def test_a_changed_pdf_sends_the_rules_and_the_file_again(tmp_path):
    root, context = _pdf_project(tmp_path, "all")
    pdf = root / "config" / "project_rules" / "board.pdf"
    first = context._build_request(LLMTask.AD_HOC_CHAT)
    context.confirm_request_delivered(first)
    context._first_interaction = False

    unchanged = context._build_request(LLMTask.AD_HOC_CHAT)
    assert unchanged.rules_content is None and unchanged.attachments == []

    pdf.write_bytes(b"%PDF-1.7 BOARD")  # same size, new bytes
    again = context._build_request(LLMTask.AD_HOC_CHAT)
    assert again.rules_content is not None and again.attachments == [pdf]


# ---- S2: the picker -------------------------------------------------------------------------


def _dialog(root, pm, tcm, tab="text_json"):
    return RuleSelectorDialog(tab, pm, tcm)


def _rows(dlg):
    """The list's rows: ("msg", text) for a message, (key, label, detail, ticked) for a box."""
    from PySide6.QtWidgets import QCheckBox, QLabel

    out = []
    layout = dlg.rules._rows
    for i in range(layout.count()):
        row = layout.itemAt(i).widget()
        if row is None:
            continue
        box = row.findChild(QCheckBox)
        if box is None:
            out.append(("msg", row.findChild(QLabel).text()))
            continue
        key = next(k for k, b in dlg.rules._boxes.items() if b is box)
        detail = [lbl.full for lbl in row.findChildren(QLabel) if lbl.objectName() == "fieldNote"]
        out.append((key, box.full, detail[0] if detail else "", box.isChecked()))
    return out


def test_the_picker_lists_bundle_rules_then_a_project_section(qapp, tmp_path):
    root = _project(tmp_path, project_docs={"own.md": "x" * 3000})
    pm = _pm(root)
    tcm = _tcm(root, {"text_json": ["02_b.md", "project/own.md"]})
    dlg = _dialog(root, pm, tcm)

    assert _rows(dlg) == [
        ("01_a.md", "01_a.md", "1 KB", False),
        ("02_b.md", "02_b.md", "1 KB", True),
        ("msg", "Project"),
        ("project/own.md", "own.md", "project · 3 KB", True),
    ]
    assert dlg.count_label.text().startswith("2 of 3 rules selected")


def test_adding_in_the_picker_ticks_the_document_in_every_view_and_warns_above_16_kb(
    qapp, tmp_path, monkeypatch
):
    root = _project(tmp_path)
    pm = _pm(root)
    tcm = _tcm(root, {"text_json": ["01_a.md"], "json_code": ["02_b.md"]})
    src = _source(tmp_path, "board.md", b"y" * (16 * 1024 + 1))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(src), ""))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dlg = _dialog(root, pm, tcm)

    dlg._on_add_document()

    assert (root / "config" / "project_rules" / "board.md").read_bytes() == src.read_bytes()
    assert ("project/board.md", "board.md", "project · 17 KB", True) in _rows(dlg)
    assert len(warned) == 1 and "16 KB" in warned[0]
    saved = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    for tab in TABS:
        assert all(
            "project/board.md" in t["selected_rules"] for t in saved["workflows"][tab]["tasks"]
        )


def test_adding_a_small_document_does_not_warn_and_a_refusal_is_shown(qapp, tmp_path, monkeypatch):
    root = _project(tmp_path, project_docs={"board.md": "mine"})
    pm = _pm(root)
    tcm = _tcm(root)
    small = _source(tmp_path, "small.md", b"ok")
    taken = tmp_path / "outside" / "board.md"
    taken.write_bytes(b"theirs")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dlg = _dialog(root, pm, tcm)

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(small), ""))
    dlg._on_add_document()
    assert warned == [] and ("project/small.md", "small.md", "project · 1 KB", True) in _rows(dlg)

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(taken), ""))
    dlg._on_add_document()
    assert len(warned) == 1 and "already" in warned[0]
    assert (root / "config" / "project_rules" / "board.md").read_text(encoding="utf-8") == "mine"


def test_a_tick_that_cannot_be_saved_says_so_in_the_footer(qapp, tmp_path):
    root = _project(tmp_path)
    tcm = _tcm(root, {"text_json": ["01_a.md"]})
    dlg = _dialog(root, _pm(root), tcm)
    (root / "config" / "config.json").write_text('{"project_id": ', encoding="utf-8")  # damaged
    dlg.rules._boxes["02_b.md"].setChecked(True)
    assert dlg.count_label.text().startswith("Not saved: ")
    assert "config.json" in dlg.count_label.text()  # why
    assert (root / "config" / "config.json").read_text(encoding="utf-8") == '{"project_id": '


def test_open_folder_that_cannot_be_created_says_so(qapp, tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / "config" / "project_rules").write_text("a file in the way", encoding="utf-8")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    _dialog(root, _pm(root), _tcm(root))._on_open_folder()
    assert len(warned) == 1 and "project_rules" in warned[0]


def test_without_a_project_the_picker_says_to_open_one(qapp, tmp_path):
    from procedo.studio.core.task_config import TaskConfigManager as _T

    dlg = RuleSelectorDialog("text_json", ProjectManager(), _T(tmp_path / "absent.json"))
    assert ("msg", "Open a project to add documents") in _rows(dlg)


def test_a_document_vanishing_while_the_picker_fills_is_skipped(qapp, tmp_path, monkeypatch):
    root = _project(tmp_path, project_docs=("gone.md",))
    pm = _pm(root)
    docs = pm.get_rule_docs()
    (root / "config" / "project_rules" / "gone.md").unlink()
    monkeypatch.setattr(pm, "get_rule_docs", lambda: docs)  # scanned before it went
    dlg = _dialog(root, pm, _tcm(root))
    assert [r[0] for r in _rows(dlg)] == ["01_a.md", "02_b.md", "msg", "msg"]


def test_open_folder_opens_the_project_rules_folder(qapp, tmp_path, monkeypatch):
    from procedo.core import os_open

    root = _project(tmp_path)
    opened = []
    monkeypatch.setattr(os_open, "open_path", opened.append)
    dlg = _dialog(root, _pm(root), _tcm(root))

    dlg._on_open_folder()

    assert [Path(p) for p in opened] == [root / "config" / "project_rules"]
    assert (root / "config" / "project_rules").is_dir()


def test_the_picker_adds_a_pdf_without_the_text_limits(qapp, tmp_path, monkeypatch):
    root = _project(tmp_path)
    tcm = _tcm(root, {"text_json": ["01_a.md"]})
    src = _source(tmp_path, "board.pdf", b"%PDF" + b"\xff" * (100 * 1024))
    filters = []
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *a, **k: filters.append(a[3]) or (str(src), "")
    )
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dlg = _dialog(root, _pm(root), tcm)

    dlg._on_add_document()

    assert "*.pdf" in filters[0]
    assert warned == []
    assert ("project/board.pdf", "board.pdf", "project · 101 KB", True) in _rows(dlg)
