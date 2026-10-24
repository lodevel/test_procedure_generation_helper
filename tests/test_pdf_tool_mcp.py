"""End-to-end test of the _pdf_tool_mcp stdio MCP server via a real subprocess.

Speaks newline-delimited JSON-RPC over the server's stdin/stdout, exactly as
OpenCode would, and verifies: tools/list advertises read_pdf, a local crafted
PDF round-trips, and a path-traversal source is denied (not leaked).
"""
import os

import pytest

from tests.test_pdf_text import make_pdf

# The server module file, found via the installed package (path-independent).
import procedo.studio.authoring._pdf_tool_mcp as _mcp_mod
from tests.mcp_client import McpServer

SERVER_PATH = os.path.abspath(_mcp_mod.__file__)


@pytest.fixture
def server(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    # Launch from an unrelated cwd to prove the import bootstrap is cwd-independent.
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    srv = McpServer([SERVER_PATH, "--documents-dir", docs], other_cwd)
    yield srv, docs
    srv.close()


def _handshake(srv):
    init = srv.request(1, "initialize", {"protocolVersion": "2024-11-05"})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "pdf-tools"
    srv.notify("notifications/initialized")  # no response expected


def test_tools_list_advertises_read_pdf(server):
    srv, _docs = server
    _handshake(srv)
    resp = srv.request(2, "tools/list")
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "read_pdf" in names


def test_read_pdf_local_roundtrip(server):
    srv, docs = server
    _handshake(srv)
    make_pdf(docs / "ds.pdf", "BENCH SECRET ZX99")
    resp = srv.request(
        3, "tools/call", {"name": "read_pdf", "arguments": {"source": "ds.pdf"}}
    )
    text = resp["result"]["content"][0]["text"]
    assert "BENCH SECRET ZX99" in text


def test_read_pdf_path_traversal_denied(server):
    srv, docs = server
    _handshake(srv)
    # Plant a secret OUTSIDE the documents dir; the traversal must not reach it.
    secret = docs.parent / "secret.txt"
    secret.write_text("TOP SECRET OUTSIDE")
    resp = srv.request(
        4,
        "tools/call",
        {"name": "read_pdf", "arguments": {"source": "../secret.txt"}},
    )
    text = resp["result"]["content"][0]["text"]
    assert "TOP SECRET OUTSIDE" not in text
    assert "denied" in text.lower()


def test_read_pdf_absolute_outside_denied(server):
    srv, docs = server
    _handshake(srv)
    outside = docs.parent / "outside.pdf"
    make_pdf(outside, "OUTSIDE PDF CONTENT")
    resp = srv.request(
        5,
        "tools/call",
        {"name": "read_pdf", "arguments": {"source": str(outside)}},
    )
    text = resp["result"]["content"][0]["text"]
    assert "OUTSIDE PDF CONTENT" not in text
    assert "denied" in text.lower()


def test_read_pdf_missing_file_is_refused(server):
    srv, _docs = server
    _handshake(srv)
    resp = srv.request(
        6,
        "tools/call",
        {"name": "read_pdf", "arguments": {"source": "nope.pdf"}},
    )
    text = resp["result"]["content"][0]["text"]
    # Not a file list_documents shows → refused, like any other name.
    assert "denied" in text.lower() and "list_documents" in text


def test_ping(server):
    srv, _docs = server
    _handshake(srv)
    resp = srv.request(7, "ping")
    assert resp["result"] == {}


def test_save_pdf_advertised(server):
    srv, _docs = server
    _handshake(srv)
    resp = srv.request(8, "tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "save_pdf" in names and "read_pdf" in names


def test_safe_pdf_name_sandbox():
    from procedo.studio.authoring import _pdf_tool_mcp as m
    assert m._safe_pdf_name("../../etc/passwd") == "passwd.pdf"
    assert m._safe_pdf_name("a/b\\c") == "c.pdf"            # basename only -> no traversal
    assert m._safe_pdf_name("LT8609AJDDM#TRPBF") == "LT8609AJDDM_TRPBF.pdf"
    assert m._safe_pdf_name("U86.pdf") == "U86.pdf"         # .pdf not doubled
    assert m._safe_pdf_name("   ") is None
    assert m._safe_pdf_name("///") is None


def test_save_pdf_caches_and_guards(monkeypatch, tmp_path):
    from pathlib import Path
    from procedo.studio.authoring import _pdf_tool_mcp as m

    def fake_fetch(url, *, save_to=None, **k):
        if save_to is not None:
            Path(save_to).parent.mkdir(parents=True, exist_ok=True)
            Path(save_to).write_bytes(b"%PDF-1.4 fake")
        return "DATASHEET TEXT"

    monkeypatch.setattr(m, "fetch_and_extract", fake_fetch)
    res = m._handle_save_pdf(
        {"source": "http://x/d.pdf", "save_as": "U86"}, str(tmp_path))
    text = res["content"][0]["text"]
    assert "DATASHEET TEXT" in text
    assert "Saved to the project documents as U86.pdf" in text
    assert (tmp_path / "U86.pdf").exists()

    # guards: a save_as is required, and only http(s) sources are fetched.
    assert "save_as" in m._handle_save_pdf(
        {"source": "http://x/d.pdf"}, str(tmp_path))["content"][0]["text"]
    assert "http" in m._handle_save_pdf(
        {"source": "local.pdf", "save_as": "U"}, str(tmp_path))["content"][0]["text"]
