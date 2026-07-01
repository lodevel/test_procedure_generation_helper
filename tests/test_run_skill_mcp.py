"""Tests for run_skill (skill-invokes-skill): pure rs_core logic, the concurrent
MCP transport (deadlock regression), and the dedicated server incl. a hermetic
child-spawn that proves the child is scoped to its OWN tools + read-only data."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import workflow_editor.authoring.rs_core as rs_core

SECRET = b"unit-test-secret"
AUTH_DIR = os.path.dirname(rs_core.__file__)
SERVER_PATH = os.path.join(AUTH_DIR, "_run_skill_mcp.py")


# --------------------------------------------------------------------------- #
# pure rs_core                                                                #
# --------------------------------------------------------------------------- #
def test_sign_verify_roundtrip():
    tok = rs_core.sign({"depth": 1, "visited": ["a"]}, SECRET)
    assert rs_core.verify(tok, SECRET) == {"depth": 1, "visited": ["a"]}


def test_verify_rejects_forged_blank_and_wrong_secret():
    tok = rs_core.sign({"depth": 0, "visited": []}, SECRET)
    assert rs_core.verify(tok, b"other-secret") is None
    assert rs_core.verify("garbage.deadbeef", SECRET) is None
    assert rs_core.verify("", SECRET) is None
    assert rs_core.verify(tok, b"") is None
    assert rs_core.verify(tok[:-1] + ("0" if tok[-1] != "0" else "1"), SECRET) is None


def test_guard_depth_cap_and_cycle():
    ok, child = rs_core.guard({"depth": 0, "visited": ["a"]}, "b", max_depth=3)
    assert ok == "ok" and child == {"depth": 1, "visited": ["a", "b"]}
    refused, reason = rs_core.guard({"depth": 3, "visited": ["a", "b", "c"]}, "d", 3)
    assert refused == "refused" and "depth" in reason
    refused, reason = rs_core.guard({"depth": 1, "visited": ["a", "b"]}, "b", 3)
    assert refused == "refused" and "cycle" in reason


def test_resolve_skill_precedence_body_and_declared(tmp_path):
    builtin, project = tmp_path / "builtin", tmp_path / "project"
    for root, marker, tools in ((builtin, "BUILTIN", "[a]"), (project, "PROJECT", "[b]")):
        d = root / "greeter"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: greeter\nmcp_tools: {tools}\n---\n{marker} body\n", encoding="utf-8")
    # ascending precedence -> project (last / higher) wins; declared parsed.
    body, declared = rs_core.resolve_skill("greeter", [str(builtin), str(project)])
    assert body == "PROJECT body" and declared == ["b"]
    with pytest.raises(ValueError):
        rs_core.resolve_skill("missing", [str(builtin)])


def test_child_tools_scopes_to_declared_plus_readonly():
    universe = {"greeter_tools": ["do"], "other_tools": ["x"]}
    tools = rs_core.child_tools(declared=["greeter_tools"], universe=universe,
                                run_skill_enabled=True)
    # built-ins hard off
    for t in ("bash", "edit", "write", "read", "task"):
        assert tools[t] is False
    # the child's OWN declared tool on; another skill's tool off
    assert tools["greeter_tools_do"] is True
    assert tools["other_tools_x"] is False
    # read-only project/datasheet data on; network off
    assert tools["project_tools_netlist"] is True
    assert tools["pdf_tools_read_document"] is True
    assert tools["webfetch"] is False and tools["pdf_tools_read_pdf"] is False
    # recursion gate
    assert tools["run_skill_run_skill"] is True
    assert rs_core.child_tools([], {}, run_skill_enabled=False)["run_skill_run_skill"] is False


# --------------------------------------------------------------------------- #
# concurrent transport (deadlock regression for _mcp_serve)                    #
# --------------------------------------------------------------------------- #
_CONCURRENT_SERVER = f'''
import sys, os, time
sys.path.insert(0, {AUTH_DIR!r})
import _mcp_serve
TOOLS = [{{"name": "slow", "description": "d", "inputSchema": {{"type": "object"}}}},
         {{"name": "fast", "description": "d", "inputSchema": {{"type": "object"}}}}]
def slow(a):
    time.sleep(1.5); return "SLOW"
def fast(a):
    return "FAST"
_mcp_serve.serve({{"name": "t", "version": "1"}}, TOOLS, {{"slow": slow, "fast": fast}})
'''


def test_mcp_serve_dispatches_calls_concurrently(tmp_path):
    """A slow tools/call must NOT block a later fast one (the depth>=2 deadlock fix)."""
    script = tmp_path / "concurrent_server.py"
    script.write_text(_CONCURRENT_SERVER, encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}}) + "\n")
        p.stdin.flush()
        p.stdout.readline()  # initialize reply
        # fire slow (id 10) then fast (id 11) back to back
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                  "params": {"name": "slow", "arguments": {}}}) + "\n")
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                                  "params": {"name": "fast", "arguments": {}}}) + "\n")
        p.stdin.flush()
        first = json.loads(p.stdout.readline())
        second = json.loads(p.stdout.readline())
        # concurrency: fast (11) overtakes slow (10) -> it must arrive FIRST.
        assert first["id"] == 11 and first["result"]["content"][0]["text"] == "FAST"
        assert second["id"] == 10 and second["result"]["content"][0]["text"] == "SLOW"
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


# --------------------------------------------------------------------------- #
# run_skill MCP server (subprocess)                                           #
# --------------------------------------------------------------------------- #
class _Server:
    def __init__(self, extra_args, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        self.p = subprocess.Popen(
            [sys.executable, SERVER_PATH, *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)

    def rpc(self, obj, timeout=20):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()
        out = {"line": None}
        t = threading.Thread(target=lambda: out.__setitem__("line", self.p.stdout.readline()),
                             daemon=True)
        t.start()
        t.join(timeout)
        assert out["line"], "no response (timeout / server died)"
        return json.loads(out["line"])

    def notify(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def _handshake(srv):
    r = srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}})
    assert r["result"]["serverInfo"]["name"] == "run_skill"
    assert r["result"]["protocolVersion"] == "2025-06-18"
    srv.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _call(srv, args, rid=9):
    r = srv.rpc({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                 "params": {"name": "run_skill", "arguments": args}})
    return r["result"]["content"][0]["text"]


def test_protocol_handshake_list_ping_unknown(tmp_path):
    srv = _Server(["--server-port-file", str(tmp_path / "opencode.pid"),
                   "--skill-root", str(tmp_path)], {"RUN_SKILL_SECRET": SECRET.decode()})
    try:
        _handshake(srv)
        r = srv.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in r["result"]["tools"]] == ["run_skill"]
        assert r["result"]["tools"][0]["inputSchema"]["required"] == [
            "skill_id", "prompt", "chain_token"]
        assert srv.rpc({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {}
        r = srv.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "nope", "arguments": {}}})
        assert r["error"]["code"] == -32602
    finally:
        srv.close()


def test_run_skill_refuses_forged_token(tmp_path):
    srv = _Server(["--server-port-file", str(tmp_path / "opencode.pid"),
                   "--skill-root", str(tmp_path)], {"RUN_SKILL_SECRET": SECRET.decode()})
    try:
        _handshake(srv)
        out = _call(srv, {"skill_id": "greeter", "prompt": "hi",
                          "chain_token": "forged.deadbeef"})
        assert "REFUSED" in out and "chain_token" in out
    finally:
        srv.close()


class _FakeOpencode(BaseHTTPRequestHandler):
    captured = []
    deleted = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        payload = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/session":
            self._send({"id": "child-1"})
        elif self.path.endswith("/message"):
            _FakeOpencode.captured.append(body)
            self._send({"info": {"role": "assistant"},
                        "parts": [{"type": "text", "text": "FAKE CHILD REPLY"}]})
        else:
            self._send({})

    def do_GET(self):
        self._send([{"info": {"role": "assistant"},
                     "parts": [{"type": "text", "text": "FAKE CHILD REPLY"}]}])

    def do_DELETE(self):
        _FakeOpencode.deleted.append(self.path)
        self._send({})


def test_run_skill_spawns_scoped_child(tmp_path):
    d = tmp_path / "roots" / "greeter"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: greeter\nmcp_tools: [greeter_tools]\n---\nSay hi.\n", encoding="utf-8")
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps({"greeter_tools": ["do"], "other_tools": ["x"]}),
                        encoding="utf-8")

    _FakeOpencode.captured.clear()
    _FakeOpencode.deleted.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpencode)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    pidfile = tmp_path / "opencode.pid"
    pidfile.write_text(json.dumps({"pid": 1, "port": port}), encoding="utf-8")

    srv = _Server(["--server-port-file", str(pidfile),
                   "--skill-root", str(tmp_path / "roots"),
                   "--universe-file", str(universe), "--max-depth", "3"],
                  {"RUN_SKILL_SECRET": SECRET.decode()})
    try:
        _handshake(srv)
        token = rs_core.sign({"depth": 0, "visited": ["orchestrator"]}, SECRET)
        out = _call(srv, {"skill_id": "greeter", "prompt": "greet", "chain_token": token})
        assert "FAKE CHILD REPLY" in out and "depth 1" in out
        tools = _FakeOpencode.captured[-1]["tools"]
        for t in ("bash", "edit", "write", "read", "task"):   # built-ins hard off
            assert tools[t] is False
        assert tools["greeter_tools_do"] is True              # child's own tool
        assert tools["other_tools_x"] is False                # not another skill's
        assert tools["project_tools_netlist"] is True         # read-only data on
        assert tools["webfetch"] is False                     # network off
        assert tools["run_skill_run_skill"] is True           # depth 1 < 3
        assert _FakeOpencode.deleted, "child session not cleaned up"
    finally:
        srv.close()
        httpd.shutdown()
