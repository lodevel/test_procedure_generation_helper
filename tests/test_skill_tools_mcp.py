"""Self-contained end-to-end tests for _skill_tools_mcp.py (the generic server).

Does NOT depend on the parent repo: all fixtures live under tests/fixtures/.
Drives the server over newline-delimited JSON-RPC and verifies:

- initialize handshake (serverInfo.name from SERVER_NAME in tools.py)
- tools/list exposes the declared tool
- tools/call returns the expected text result
- ping returns {}
- unknown tool returns -32602
- drift guard: tools.json listing a tool NOT in tools.py makes the server exit
  non-zero (fail-closed) before it serves any request.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import procedo.studio.authoring._skill_tools_mcp as _mcp_mod
from tests.mcp_client import McpServer

SERVER_PATH = os.path.abspath(_mcp_mod.__file__)

# Fixture tool dir: SERVER_NAME="fixture_tools", tool "echo" -> "echo:<x>"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tooldir"

# Drift fixture: tools.py has "echo" but tools.json lists "not_echo" -> exit 2
DRIFT_DIR = Path(__file__).parent / "fixtures" / "tooldir_drift"


def _result_text(resp):
    return resp["result"]["content"][0]["text"]


@pytest.fixture
def server(tmp_path):
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    srv = McpServer([SERVER_PATH, "--tools-dir", FIXTURE_DIR], other_cwd)
    yield srv
    srv.close()


def _handshake(srv):
    init = srv.request(1, "initialize", {"protocolVersion": "2024-11-05"})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "fixture_tools"
    srv.notify("notifications/initialized")


def test_initialize_returns_fixture_server_name(server):
    _handshake(server)


def test_tools_list_has_echo(server):
    _handshake(server)
    resp = server.request(2, "tools/list")
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"echo"}


def test_tools_call_echo_returns_text_result(server):
    _handshake(server)
    resp = server.call(2, "echo", {"x": "hi"})
    assert _result_text(resp) == "echo:hi"


def test_ping(server):
    _handshake(server)
    resp = server.request(2, "ping")
    assert resp["result"] == {}


def test_unknown_tool_returns_minus_32602(server):
    _handshake(server)
    resp = server.request(2, "tools/call", {"name": "no_such", "arguments": {}})
    assert resp["error"]["code"] == -32602


def test_drift_guard_exits_nonzero():
    """tools.json listing a tool absent from tools.py -> server exits non-zero."""
    proc = subprocess.Popen(
        [sys.executable, SERVER_PATH, "--tools-dir", str(DRIFT_DIR)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    assert proc.returncode != 0


def test_loading_a_tool_folder_leaves_no_bytecode_in_it(tmp_path, monkeypatch):
    """The project's bundle tool folder gets no __pycache__ from the server's load."""
    import shutil

    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    tools_dir = tmp_path / "tooldir"
    shutil.copytree(FIXTURE_DIR, tools_dir, ignore=shutil.ignore_patterns("__pycache__"))
    _mcp_mod._load_tool_folder(str(tools_dir))
    assert not (tools_dir / "__pycache__").exists()
    assert sys.dont_write_bytecode is False
