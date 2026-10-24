"""Project rule documents reach the skills too (docs/plans/project_rules_scope.md §4, slice S4):
the skill chat's Rules list (and so the wizards' chats) lists the bundle rules AND the project's
own documents, keyed as in the Studio tabs (file name / ``project/<name>``), from
``ProjectManager.get_rule_docs``; a selected PDF rides the message as a file, as in the tabs.
The PDF-tools MCP's list_rules / read_rule see the project documents as ``project/<name>``,
confined to the two folders."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from procedo.studio.authoring import RulesSource, SkillChatSession, assemble
from procedo.studio.authoring import _pdf_tool_mcp as mcp
from procedo.studio.authoring.skill_menu import build_sources
from procedo.studio.core.project_manager import PROJECT_RULES_WIN, RuleDoc

from .test_authoring_skill_chat import _skill
from .test_main_window_characterization import qapp as qapp
from .test_pdf_text import make_pdf
from .test_project_rule_docs import _pm, _project


def _delete_now(widget):
    """Delete *widget* now: a parentless widget left to the GC gets events after its
    Python side is gone (its eventFilter then fails)."""
    from PySide6.QtCore import QCoreApplication, QEvent

    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _text(result):
    return result["content"][0]["text"]


def _docs(tmp_path):
    """A bundle rule and a project document of the same name, a project .txt and a project PDF."""
    (tmp_path / "b").mkdir()
    (tmp_path / "p").mkdir()
    (tmp_path / "b" / "a.md").write_text("bundle a", encoding="utf-8")
    (tmp_path / "p" / "a.md").write_text("project a", encoding="utf-8")
    (tmp_path / "p" / "c.txt").write_text("﻿project c", encoding="utf-8")
    (tmp_path / "p" / "d.pdf").write_bytes(b"%PDF-1.4 fake")
    return [RuleDoc("a.md", tmp_path / "b" / "a.md", "bundle"),
            RuleDoc("project/a.md", tmp_path / "p" / "a.md", "project"),
            RuleDoc("project/c.txt", tmp_path / "p" / "c.txt", "project"),
            RuleDoc("project/d.pdf", tmp_path / "p" / "d.pdf", "project")]


# --------------------------------------------------------------------------- #
# Skill chat Rules list                                                       #
# --------------------------------------------------------------------------- #

def test_the_rules_list_holds_the_bundle_rules_then_the_project_documents_labelled(tmp_path):
    src = RulesSource(lambda: _docs(tmp_path))
    items = src.list_items()
    assert [i.key for i in items] == ["a.md", "project/a.md", "project/c.txt", "project/d.pdf"]
    assert [i.label for i in items] == ["a.md", "project/a.md", "project/c.txt", "project/d.pdf"]
    assert [i.detail for i in items] == ["1 KB", "project · 1 KB", "project · 1 KB",
                                         "project · 1 KB"]


def test_the_project_documents_come_after_the_bundle_rules_under_the_project_wins_line(tmp_path):
    src = RulesSource(lambda: _docs(tmp_path), transform=str.upper)
    out = src.materialize(["project/c.txt", "project/a.md", "a.md"])  # any key order
    assert out.index("## a.md") < out.index(PROJECT_RULES_WIN) < out.index("## project/a.md")
    assert out.index("## project/a.md") < out.index("## project/c.txt")
    assert "BUNDLE A" in out and "PROJECT A" in out
    assert "PROJECT C" in out and "﻿" not in out  # a BOM is not text
    assert "d.pdf" not in out


def test_bundle_rules_alone_have_no_project_line(tmp_path):
    out = RulesSource(lambda: _docs(tmp_path)).materialize(["a.md"])
    assert out == "## a.md\n\nbundle a"


def test_a_selected_pdf_is_named_in_the_text_and_attached_as_a_file(tmp_path):
    docs = _docs(tmp_path)
    src = RulesSource(lambda: docs)
    out = src.materialize(["project/d.pdf"])
    assert "## project/d.pdf" in out and "attached to this message as a file" in out
    assert "%PDF" not in out
    assert src.files(["project/d.pdf", "project/a.md"]) == [docs[3].path]
    assert src.files(["project/a.md", "a.md"]) == []
    bundle = assemble([(src, ["a.md", "project/d.pdf"])])
    assert bundle.files == (docs[3].path,)
    assert assemble([(src, ["a.md"])]).files == ()


def test_the_skill_chat_lists_what_the_studio_tabs_send(tmp_path):
    """build_sources (the skill chat's and the wizards' Rules list) reads get_rule_docs."""
    root = _project(tmp_path, project_docs=("01_a.md", "notes.txt"))
    pm = _pm(root)
    sources, _ = build_sources(SimpleNamespace(project_manager=pm), root)
    rules = next(s for s in sources if s.source_id == "rules")
    assert [i.key for i in rules.list_items()] == [doc.key for doc in pm.get_rule_docs()] == [
        "01_a.md", "02_b.md", "project/01_a.md", "project/notes.txt"]
    out = rules.materialize(["project/notes.txt", "01_a.md"])
    assert "bundle 01_a.md" in out and "project notes.txt" in out


def test_the_session_sends_the_files_with_the_context_once_and_again_when_they_change(tmp_path):
    pdf = tmp_path / "d.pdf"
    s = SkillChatSession(_skill(), context_text="CTX", context_files=(pdf,))
    s.start_user_turn("q1")
    assert s.pending_files == [pdf]
    s.record_assistant("a1")
    s.start_user_turn("q2")
    assert s.pending_files == []                     # delivered: not attached again
    s.record_assistant("a2")
    s.set_context("CTX", ())                          # the PDF unticked, text the same
    assert s.start_user_turn("q3") == "CTX\n\nq3" and s.pending_files == []


def test_a_pdf_whose_contents_changed_is_sent_again(tmp_path):
    """Same selection, new contents: sent again (the contents digest the Studio tabs use)."""
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF v1")
    s = SkillChatSession(_skill(), context_text="CTX", context_files=(pdf,))
    s.start_user_turn("q1")
    s.record_assistant("a1")
    assert s.start_user_turn("q2") == "q2" and s.pending_files == []
    s.record_assistant("a2")
    pdf.write_bytes(b"%PDF v2")
    assert s.start_user_turn("q3") == "CTX\n\nq3" and s.pending_files == [pdf]


def test_a_failed_send_attaches_the_files_again(tmp_path):
    pdf = tmp_path / "d.pdf"
    s = SkillChatSession(_skill(), context_text="CTX", context_files=(pdf,))
    s.start_user_turn("q1")
    s.drop_last_user_turn()
    assert s.pending_files == []
    s.start_user_turn("q1")
    assert s.pending_files == [pdf]


def test_the_skill_chat_request_carries_the_selected_pdf(tmp_path, qapp, monkeypatch):
    """The picker's selected PDF rides the first request as an attachment (the same
    ``LLMRequest.attachments`` the Studio tabs fill); a later turn carries none."""
    from procedo.studio.dock import skill_chat_widget as scw

    monkeypatch.setattr("procedo.studio.core.studio_settings.load_settings", lambda: {})
    docs = _docs(tmp_path)
    sent = []

    class _Worker:
        def __init__(self, backend, request, parent=None):
            sent.append(request)
            self.text_chunk = self.thinking_chunk = self.finished = self.error = SimpleNamespace(
                connect=lambda slot: None)

        def start(self):
            pass

    monkeypatch.setattr(scw, "LLMWorker", _Worker)
    widget = scw.SkillChatWidget([_skill()], SimpleNamespace(), sources=[RulesSource(lambda: docs)])
    monkeypatch.setattr(widget, "_ensure_backend", lambda: object())
    monkeypatch.setattr(widget, "_ensure_session", lambda: None)
    widget._picker.set_checked("rules", ["a.md", "project/d.pdf"])
    widget.run_kickoff()
    assert sent[-1].attachments == [docs[3].path]
    assert "## project/d.pdf" in sent[-1].raw_prompt
    widget._worker = None
    widget._session.record_assistant("reply")
    widget.send_user_turn("more")
    assert sent[-1].attachments == [] and sent[-1].raw_prompt == "more"
    assert widget.resolved_context() == assemble(widget._picker.selections())  # text + files
    widget._worker = None  # the fake never finishes
    widget.shutdown()
    _delete_now(widget)


# --------------------------------------------------------------------------- #
# PDF-tools MCP: list_rules / read_rule                                       #
# --------------------------------------------------------------------------- #

def _mcp_dirs(tmp_path):
    rules = tmp_path / "bundle_rules"
    own = tmp_path / "project_rules"
    rules.mkdir()
    own.mkdir()
    (rules / "001_grammar.md").write_text("GRAMMAR", encoding="utf-8")
    (rules / "same.md").write_text("BUNDLE SAME", encoding="utf-8")
    (own / "same.md").write_text("PROJECT SAME", encoding="utf-8")
    (own / "notes.txt").write_text("﻿NOTES", encoding="utf-8")
    (own / "Thumbs.db").write_bytes(b"x")              # not a rule document
    (own / "sub").mkdir()
    (own / "sub" / "deep.md").write_text("DEEP", encoding="utf-8")  # not top-level
    (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")
    return str(rules), str(own)


def test_list_rules_lists_the_project_documents_after_the_bundle_rules(tmp_path):
    rules, own = _mcp_dirs(tmp_path)
    out = _text(mcp._handle_list_rules(rules, own))
    lines = [line[2:] for line in out.splitlines() if line.startswith("- ")]
    assert lines == ["001_grammar.md", "same.md", "project/notes.txt", "project/same.md"]
    assert "read_rule" in out


def test_list_rules_without_a_bundle_still_lists_the_project_documents(tmp_path):
    _, own = _mcp_dirs(tmp_path)
    out = _text(mcp._handle_list_rules(str(tmp_path / "absent"), own))
    assert "- project/same.md" in out
    assert "no rules folder" in _text(mcp._handle_list_rules(str(tmp_path / "absent"), None))


def test_read_rule_reads_a_project_document_by_its_key_and_a_bundle_rule_by_its_name(tmp_path):
    rules, own = _mcp_dirs(tmp_path)
    assert _text(mcp._handle_read_rule({"name": "project/same.md"}, rules, own)) == "PROJECT SAME"
    assert _text(mcp._handle_read_rule({"name": "same.md"}, rules, own)) == "BUNDLE SAME"
    assert _text(mcp._handle_read_rule({"name": "project/notes.txt"}, rules, own)) == "NOTES"


@pytest.mark.parametrize("name", [
    "project/../secret.md", "project/..\\secret.md", "project/sub/deep.md", "project/Thumbs.db",
    "project/", "project/../bundle_rules/same.md", "../secret.md", "project/ghost.md",
])
def test_read_rule_stays_inside_the_two_folders(tmp_path, name):
    rules, own = _mcp_dirs(tmp_path)
    out = _text(mcp._handle_read_rule({"name": name}, rules, own))
    assert "SECRET" not in out and "DEEP" not in out and "BUNDLE SAME" not in out
    assert "denied" in out.lower()


def test_read_rule_without_a_project_folder_refuses_project_keys(tmp_path):
    rules, _ = _mcp_dirs(tmp_path)
    out = _text(mcp._handle_read_rule({"name": "project/same.md"}, rules, None))
    assert "PROJECT SAME" not in out and "denied" in out.lower()


def test_read_rule_returns_a_project_pdf_as_its_extracted_text(tmp_path):
    rules, own = _mcp_dirs(tmp_path)
    make_pdf(Path(own) / "spec.pdf", "PIN TABLE U18")
    assert "- project/spec.pdf" in _text(mcp._handle_list_rules(rules, own))
    out = _text(mcp._handle_read_rule({"name": "project/spec.pdf"}, rules, own))
    assert "PIN TABLE U18" in out and "%PDF" not in out  # the text, not the file's bytes


def test_the_server_takes_the_project_rules_folder_from_its_command_line(tmp_path, monkeypatch):
    rules, own = _mcp_dirs(tmp_path)
    served = {}
    monkeypatch.setitem(__import__("sys").modules, "_mcp_serve", SimpleNamespace(
        serve=lambda info, tools, dispatch: served.update(dispatch)))
    mcp.main(["--documents-dir", str(tmp_path), "--rules-dir", rules,
              "--project-rules-dir", own])
    assert _text(served["read_rule"]({"name": "project/same.md"})) == "PROJECT SAME"
    assert "- project/notes.txt" in _text(served["list_rules"]({}))


# --------------------------------------------------------------------------- #
# The pdf_tools block passes both folders                                     #
# --------------------------------------------------------------------------- #

def test_the_pdf_tools_block_passes_the_bundle_rules_and_the_project_rules_folders(tmp_path):
    from procedo.studio.llm.launch_config import _build_pdf_tools_block

    root = _project(tmp_path)
    cmd = _build_pdf_tools_block(root)["pdf_tools"]["command"]
    assert cmd[cmd.index("--rules-dir") + 1] == str(root / "bundle" / "rules")
    assert cmd[cmd.index("--project-rules-dir") + 1] == str(root / "config" / "project_rules")
    assert not (root / "config" / "project_rules").exists()   # never created here


def test_the_pdf_tools_block_without_a_project_has_no_project_rules_folder(tmp_path, monkeypatch):
    from procedo.studio.llm import launch_config

    monkeypatch.setattr(launch_config, "get_opencode_config_dir", lambda: tmp_path)
    cmd = launch_config._build_pdf_tools_block(None)["pdf_tools"]["command"]
    assert "--project-rules-dir" not in cmd


def test_a_project_without_a_loadable_bundle_lists_no_bundle_rules(tmp_path):
    """The bundle rules folder is the one Studio resolves (bundle_registry.active_rules_dir):
    no loadable bundle, no bundle rules for the tools either."""
    from procedo.studio.llm.launch_config import _build_pdf_tools_block

    root = _project(tmp_path)
    (root / "bundle" / "bundle.json").write_text(json.dumps({}), encoding="utf-8")
    cmd = _build_pdf_tools_block(root)["pdf_tools"]["command"]
    assert cmd[cmd.index("--rules-dir") + 1] != str(root / "bundle" / "rules")


# --------------------------------------------------------------------------- #
# read_rule reads only what list_rules lists; the PDF text cap holds          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "same.md:evil", "same.md::$DATA", "./same.md", "SAME.MD ", "bundle_rules/same.md",
])
def test_read_rule_reads_only_a_bundle_file_list_rules_lists(tmp_path, name):
    """One confinement rule for both folders: the key must be an entry of list_rules. An NTFS
    alternate data stream ("same.md:evil") is not one."""
    rules, own = _mcp_dirs(tmp_path)
    try:
        with open(Path(rules) / "same.md:evil", "w", encoding="utf-8") as fh:
            fh.write("EVIL STREAM")
    except OSError:  # not NTFS: no alternate data streams to reach
        pass
    out = _text(mcp._handle_read_rule({"name": name}, rules, own))
    assert "EVIL" not in out and "BUNDLE SAME" not in out
    assert "denied" in out.lower()


def test_read_rule_reads_a_listed_bundle_file_in_a_subfolder(tmp_path):
    rules, own = _mcp_dirs(tmp_path)
    (Path(rules) / "extra").mkdir()
    (Path(rules) / "extra" / "x.md").write_text("EXTRA", encoding="utf-8")
    listed = [line[2:] for line in _text(mcp._handle_list_rules(rules, own)).splitlines()
              if line.startswith("- ")]
    key = next(k for k in listed if k.endswith("x.md"))
    assert _text(mcp._handle_read_rule({"name": key}, rules, own)) == "EXTRA"


def test_the_pdf_text_cap_holds_inside_one_long_page(tmp_path):
    """One page longer than the cap is cut to the cap, through read_rule and read_document."""
    from procedo.studio.authoring.pdf_text import DEFAULT_MAX_CHARS

    rules, own = _mcp_dirs(tmp_path)
    page = "ABCD " * 20_000  # 100k characters on one page
    make_pdf(Path(own) / "long.pdf", page)
    make_pdf(tmp_path / "long.pdf", page)
    for out in (_text(mcp._handle_read_rule({"name": "project/long.pdf"}, rules, own)),
                _text(mcp._handle_read_document({"name": "long.pdf"}, str(tmp_path)))):
        assert out.count("ABCD") * 5 <= DEFAULT_MAX_CHARS + 5
        assert out.count("ABCD") * 5 >= DEFAULT_MAX_CHARS - 10
        assert "truncated" in out


# --------------------------------------------------------------------------- #
# Every read tool reads only what its list tool shows                         #
# --------------------------------------------------------------------------- #

def _documents(tmp_path):
    docs = tmp_path / "documents"
    (docs / "sub").mkdir(parents=True)
    make_pdf(docs / "ds.pdf", "DATASHEET U18")
    make_pdf(docs / "sub" / "deep.pdf", "DEEP U18")
    make_pdf(tmp_path / "outside.pdf", "OUTSIDE U18")
    try:  # an NTFS alternate data stream holding a whole PDF
        make_pdf(Path(str(docs / "ds.pdf") + ":evil"), "STREAM U18")
    except OSError:  # not NTFS: no streams to reach
        pass
    return docs


def _read_tools(docs):
    return {
        "read_pdf": lambda n: mcp._handle_read_pdf({"source": n}, str(docs)),
        "read_document": lambda n: mcp._handle_read_document({"name": n}, str(docs)),
        "search_document": lambda n: mcp._handle_search_document(
            {"name": n, "query": "U18"}, str(docs)),
    }


@pytest.mark.parametrize("tool", ["read_pdf", "read_document", "search_document"])
def test_a_document_tool_reads_what_list_documents_shows(tmp_path, tool):
    docs = _documents(tmp_path)
    listed = [line[2:] for line in _text(mcp._handle_list_documents(str(docs))).splitlines()
              if line.startswith("- ")]
    assert listed == ["ds.pdf", "sub/deep.pdf"]
    read = _read_tools(docs)[tool]
    assert "DATASHEET" in _text(read("ds.pdf"))
    assert "DEEP" in _text(read("sub/deep.pdf"))


@pytest.mark.parametrize("tool", ["read_pdf", "read_document", "search_document"])
@pytest.mark.parametrize("name", [
    "ds.pdf:evil", "ds.pdf:evil:$DATA", "../outside.pdf", "sub/../../outside.pdf", "./ds.pdf",
    "sub\\deep.pdf", "DS.PDF", "nope.pdf", "sub",
])
def test_a_document_tool_refuses_anything_else(tmp_path, tool, name):
    docs = _documents(tmp_path)
    out = _text(_read_tools(docs)[tool](name))
    assert "STREAM" not in out and "OUTSIDE" not in out
    assert "DATASHEET" not in out and "DEEP" not in out
    assert "denied" in out.lower()


@pytest.mark.parametrize("tool", ["read_pdf", "read_document", "search_document"])
def test_a_document_tool_refuses_an_absolute_path(tmp_path, tool):
    docs = _documents(tmp_path)
    for path in (tmp_path / "outside.pdf", docs / "ds.pdf"):
        out = _text(_read_tools(docs)[tool](str(path)))
        assert "OUTSIDE" not in out and "DATASHEET" not in out and "denied" in out.lower()
