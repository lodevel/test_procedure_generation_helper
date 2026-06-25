"""list_documents + read_document — the always-on, sandboxed local-document tools
of the pdf-tools MCP server (no network; usable without the web toggle)."""
import os

from workflow_editor.authoring import _pdf_tool_mcp as m


def _text(result):
    return result["content"][0]["text"]


def test_list_documents_lists_files_recursively(tmp_path):
    (tmp_path / "U86_LT8609A.pdf").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "U34_TDN1.pdf").write_text("y")
    out = _text(m._handle_list_documents(str(tmp_path)))
    assert "U86_LT8609A.pdf" in out
    assert os.path.join("sub", "U34_TDN1.pdf") in out


def test_list_documents_empty(tmp_path):
    assert "empty" in _text(m._handle_list_documents(str(tmp_path))).lower()


def test_list_documents_missing_folder(tmp_path):
    out = _text(m._handle_list_documents(str(tmp_path / "nope")))
    assert "no documents folder" in out.lower()


def test_read_document_denies_escape(tmp_path):
    # The sandbox refuses anything outside the documents folder.
    out = _text(m._handle_read_document({"name": "../../etc/passwd"}, str(tmp_path)))
    assert "denied" in out.lower()


def test_read_document_requires_name(tmp_path):
    assert "requires" in _text(m._handle_read_document({}, str(tmp_path))).lower()


def test_tools_list_advertises_all_three():
    names = {t["name"] for t in m.TOOLS}
    assert names == {"read_pdf", "read_document", "list_documents"}
