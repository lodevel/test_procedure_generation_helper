"""End-to-end test of the _project_tools_mcp stdio MCP server via a subprocess.

Speaks newline-delimited JSON-RPC over the server's stdin/stdout, exactly as the
editor would, and verifies the handshake, the advertised tool set, and every
tool's behavior against a SMALL synthetic board.

No real ODB++ .tgz is required: the server shells out to ``cli.py`` with
``sys.executable``, so we drop in a FAKE cli.py (a tiny LF script written to a
temp dir) and point the server at it via ``_PROJECT_TOOLS_CLI`` — an env-var
override the server honors purely for testing. The fake CLI reads a synthetic
board JSON file (handed to it as ``--odb-tgz``) and emits the same JSON shapes
the real CLI produces for ``--list-property-fields`` / ``--list`` (with
``--fields`` / ``--filter``) / ``--list-nets``.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

# The server module file, found via the installed package (path-independent).
import workflow_editor.authoring._project_tools_mcp as _mcp_mod

SERVER_PATH = os.path.abspath(_mcp_mod.__file__)


# --- synthetic board ----------------------------------------------------------
# Two multi-pin parts + two single-pin "test points" (board-agnostic: detected
# by pin count, NOT by any property value). Property field names are arbitrary
# on purpose — the schema/projection is the whole point.
SYNTH_BOARD = {
    "property_fields": ["MfgRef", "Package", "Type"],
    "components": [
        {
            "refdes": "U1",
            "side": "TOP",
            "pins": [
                {"name": "1", "net": "VCC"},
                {"name": "2", "net": "GND"},
            ],
            "properties": {"MfgRef": "TPS62840", "Package": "SOT-23", "Type": "IC"},
        },
        {
            "refdes": "R1",
            "side": "TOP",
            "pins": [
                {"name": "1", "net": "VCC"},
                {"name": "2", "net": "GND"},
            ],
            "properties": {"MfgRef": "RES-1K", "Package": "0402", "Type": "Resistor"},
        },
        {
            "refdes": "C1",
            "side": "TOP",
            "pins": [
                {"name": "1", "net": "VCC"},
                {"name": "2", "net": "GND"},
            ],
            "properties": {"MfgRef": "CAP-100N", "Package": "0603", "Type": "Capacitor"},
        },
        {
            "refdes": "TP_VCC",
            "side": "TOP",
            "pins": [{"name": "1", "net": "VCC"}],
            "properties": {"MfgRef": "", "Package": "TP_PAD", "Type": "Pad"},
        },
        {
            "refdes": "TP_GND",
            "side": "BOTTOM",
            "pins": [{"name": "1", "net": "GND"}],
            "properties": {"MfgRef": "", "Package": "TP_PAD", "Type": "Pad"},
        },
    ],
}

# A LF fake CLI: reads the synthetic board JSON file (passed as --odb-tgz) and
# reproduces the real cli.py JSON shapes for the inspection flags the server
# uses. Kept deliberately small but faithful to --fields / --filter semantics.
_FAKE_CLI_SRC = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import argparse, json, sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--odb-tgz", required=True)
    ap.add_argument("--list-property-fields", action="store_true")
    ap.add_argument("--list", dest="list_components", action="store_true")
    ap.add_argument("--list-nets", action="store_true")
    ap.add_argument("--fields", default=None)
    ap.add_argument("--filter", dest="filter_kv", default=None)
    ap.add_argument("--refdes-prefix", dest="refdes_prefix", default=None)
    args = ap.parse_args()

    with open(args.odb_tgz, "r", encoding="utf-8") as fh:
        board = json.load(fh)

    if args.list_property_fields:
        print(json.dumps(board["property_fields"]))
        sys.exit(0)

    if args.list_components:
        fields = None
        if args.fields:
            fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        filt = None
        if args.filter_kv:
            k, _, v = args.filter_kv.partition("=")
            filt = (k.strip(), v)
        prefixes = None
        if args.refdes_prefix:
            prefixes = tuple(
                p.strip().lower() for p in args.refdes_prefix.split(",") if p.strip()
            ) or None
        out = []
        for comp in board["components"]:
            props = comp["properties"]
            if prefixes is not None:
                rd = (comp["refdes"] or "").lower()
                if not any(
                    rd.startswith(p) and len(rd) > len(p) and rd[len(p)].isdigit()
                    for p in prefixes
                ):
                    continue
            if filt is not None:
                k, v = filt
                if k not in props or str(props[k]).strip().lower() != v.strip().lower():
                    continue
            if fields is not None:
                props_out = {k: props[k] for k in fields if k in props}
            else:
                props_out = props
            out.append({
                "refdes": comp["refdes"],
                "side": comp["side"],
                "pins": [p["name"] for p in comp["pins"]],
                "properties": props_out,
            })
        print(json.dumps(out))
        sys.exit(0)

    if args.list_nets:
        net_map = {}
        comps = []
        for comp in board["components"]:
            comps.append({
                "refdes": comp["refdes"],
                "side": comp["side"],
                "pins": comp["pins"],
                "properties": comp["properties"],
            })
            for pin in comp["pins"]:
                net = pin.get("net")
                if net:
                    net_map.setdefault(net, []).append(
                        {"refdes": comp["refdes"], "pin": pin["name"]})
        nets = [{"net": n, "nodes": nodes} for n, nodes in sorted(net_map.items())]
        print(json.dumps({"components": comps, "nets": nets}))
        sys.exit(0)

    print(json.dumps([]))
    '''
)


class _Server:
    """Line-oriented JSON-RPC client over a server subprocess."""

    def __init__(self, odb_tgz, cli_path, cwd):
        env = dict(os.environ)
        env["_PROJECT_TOOLS_CLI"] = str(cli_path)
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_PATH, "--odb-tgz", str(odb_tgz)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd),
            env=env,
        )

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout=15):
        # readline() on a piped subprocess can block; guard with a watchdog
        # thread that kills the process so the test fails fast instead of hanging.
        import threading

        result = {}

        def _reader():
            result["line"] = self.proc.stdout.readline()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.proc.kill()
            raise AssertionError("timed out waiting for MCP server response")
        line = result.get("line", "")
        if not line:
            err = self.proc.stderr.read()
            raise AssertionError(f"server closed stdout; stderr:\n{err}")
        return json.loads(line)

    def request(self, rid, method, params=None):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.read()

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def call(self, rid, name, arguments=None):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        resp = self.request(rid, "tools/call", params)
        return resp

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _result_text(resp):
    return resp["result"]["content"][0]["text"]


def _result_json(resp):
    return json.loads(_result_text(resp))


@pytest.fixture
def server(tmp_path):
    # Write the synthetic board + the fake CLI (LF) to a temp dir.
    board_file = tmp_path / "board.json"
    board_file.write_text(json.dumps(SYNTH_BOARD), encoding="utf-8")
    cli_file = tmp_path / "fake_cli.py"
    cli_file.write_text(_FAKE_CLI_SRC, encoding="utf-8", newline="\n")
    # Launch from an unrelated cwd to prove launch-location independence.
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    srv = _Server(board_file, cli_file, other_cwd)
    yield srv
    srv.close()


def _handshake(srv):
    init = srv.request(1, "initialize", {"protocolVersion": "2024-11-05"})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "project-tools"
    srv.notify("notifications/initialized")  # no response expected


def test_tools_list_advertises_all_tools(server):
    _handshake(server)
    resp = server.request(2, "tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {
        "list_property_fields",
        "list_components",
        "get_component",
        "query_net",
        "netlist",
        "get_bom",
        "list_test_points",
    }
    # Every tool must carry a non-empty description telling the model WHEN to use it.
    for tool in resp["result"]["tools"]:
        assert tool.get("description", "").strip()
        assert "inputSchema" in tool


def test_list_property_fields(server):
    _handshake(server)
    resp = server.call(2, "list_property_fields")
    fields = _result_json(resp)
    assert fields == ["MfgRef", "Package", "Type"]


def test_list_components_all(server):
    _handshake(server)
    resp = server.call(2, "list_components")
    comps = _result_json(resp)
    refdes = {c["refdes"] for c in comps}
    assert refdes == {"U1", "R1", "C1", "TP_VCC", "TP_GND"}
    # Full properties present by default.
    r1 = next(c for c in comps if c["refdes"] == "R1")
    assert r1["properties"]["MfgRef"] == "RES-1K"


def test_list_components_filtered(server):
    _handshake(server)
    resp = server.call(2, "list_components", {"filter": "Type=Pad"})
    comps = _result_json(resp)
    assert {c["refdes"] for c in comps} == {"TP_VCC", "TP_GND"}


def test_list_components_filter_case_insensitive(server):
    _handshake(server)
    resp = server.call(2, "list_components", {"filter": "Type=pad"})
    comps = _result_json(resp)
    assert {c["refdes"] for c in comps} == {"TP_VCC", "TP_GND"}


def test_list_components_projected_fields(server):
    _handshake(server)
    resp = server.call(2, "list_components", {"fields": ["Type"]})
    comps = _result_json(resp)
    r1 = next(c for c in comps if c["refdes"] == "R1")
    assert set(r1["properties"].keys()) == {"Type"}


def test_list_components_bad_filter(server):
    _handshake(server)
    resp = server.call(2, "list_components", {"filter": "NoEquals"})
    assert "filter" in _result_text(resp).lower()


def test_list_components_refdes_prefix_keeps_only_ics(server):
    # The targeted one-call IC pull: refdes_prefix='U,IC' + the part-number field.
    _handshake(server)
    resp = server.call(
        2, "list_components", {"refdes_prefix": "U,IC", "fields": ["MfgRef"]})
    comps = _result_json(resp)
    assert {c["refdes"] for c in comps} == {"U1"}
    # Only the requested column came back.
    assert comps[0]["properties"] == {"MfgRef": "TPS62840"}


def test_list_components_refdes_prefix_excludes_passives(server):
    _handshake(server)
    resp = server.call(2, "list_components", {"refdes_prefix": "U,IC"})
    comps = _result_json(resp)
    # R1/C1/TP_* are not U#/IC# and must be excluded.
    assert all(c["refdes"] == "U1" for c in comps)


def test_list_components_bad_refdes_prefix_type(server):
    _handshake(server)
    resp = server.call(2, "list_components", {"refdes_prefix": ["U", "IC"]})
    assert "refdes_prefix" in _result_text(resp).lower()


def test_get_component_found(server):
    _handshake(server)
    resp = server.call(2, "get_component", {"refdes": "C1"})
    comp = _result_json(resp)
    assert comp["refdes"] == "C1"
    assert comp["properties"]["Type"] == "Capacitor"


def test_get_component_projected(server):
    _handshake(server)
    resp = server.call(2, "get_component", {"refdes": "C1", "fields": ["Package"]})
    comp = _result_json(resp)
    assert set(comp["properties"].keys()) == {"Package"}


def test_get_component_not_found(server):
    _handshake(server)
    resp = server.call(2, "get_component", {"refdes": "NOPE"})
    assert "not found" in _result_text(resp).lower()


def test_get_component_missing_refdes(server):
    _handshake(server)
    resp = server.call(2, "get_component", {})
    assert "refdes" in _result_text(resp).lower()


def test_query_net_found(server):
    _handshake(server)
    resp = server.call(2, "query_net", {"name": "VCC"})
    net = _result_json(resp)
    assert net["net"] == "VCC"
    nodes = {(n["refdes"], n["pin"]) for n in net["nodes"]}
    assert ("R1", "1") in nodes
    assert ("TP_VCC", "1") in nodes


def test_query_net_not_found(server):
    _handshake(server)
    resp = server.call(2, "query_net", {"name": "DOES_NOT_EXIST"})
    assert "not found" in _result_text(resp).lower()


def test_netlist_full(server):
    _handshake(server)
    resp = server.call(2, "netlist")
    data = _result_json(resp)
    net_names = {e["net"] for e in data["nets"]}
    assert net_names == {"VCC", "GND"}
    # netlist() projects to just the nets graph (no bulky component echo).
    assert "components" not in data
    # Capped-overview envelope is present; the synthetic board is tiny so it is
    # NOT truncated (no 'note').
    assert data["total"] == 2
    assert data["returned"] == 2
    assert data["offset"] == 0
    assert "note" not in data


def test_netlist_is_capped_and_paginated(server):
    # A small limit truncates and advertises how to narrow instead of paging.
    _handshake(server)
    resp = server.call(2, "netlist", {"limit": 1})
    data = _result_json(resp)
    assert data["total"] == 2
    assert data["returned"] == 1
    assert len(data["nets"]) == 1
    assert "note" in data  # tells the model to narrow with query_net/name_contains
    assert "query_net" in data["note"]
    # Page 2 via offset returns the rest, no note.
    resp2 = server.call(3, "netlist", {"limit": 1, "offset": 1})
    data2 = _result_json(resp2)
    assert data2["returned"] == 1
    assert data2["offset"] == 1
    assert "note" not in data2
    assert {data["nets"][0]["net"], data2["nets"][0]["net"]} == {"VCC", "GND"}


def test_netlist_name_contains_filters_nets(server):
    _handshake(server)
    resp = server.call(2, "netlist", {"name_contains": "vc"})
    data = _result_json(resp)
    assert {e["net"] for e in data["nets"]} == {"VCC"}
    assert data["total"] == 1


def test_netlist_limit_cannot_exceed_cap(server):
    # Even an absurd limit is clamped to the server cap (never a full dump path).
    _handshake(server)
    resp = server.call(2, "netlist", {"limit": 100000})
    data = _result_json(resp)
    # Tiny board so all 2 nets fit, but the request did not bypass the cap.
    assert data["returned"] == 2


def test_list_test_points_board_agnostic(server):
    _handshake(server)
    resp = server.call(2, "list_test_points")
    points = _result_json(resp)
    # Single-pin components only — NOT filtered by any property value.
    refdes = {p["refdes"] for p in points}
    assert refdes == {"TP_VCC", "TP_GND"}
    by_ref = {p["refdes"]: p for p in points}
    assert by_ref["TP_VCC"]["net"] == "VCC"
    assert by_ref["TP_VCC"]["pin"] == "1"
    assert by_ref["TP_GND"]["net"] == "GND"


def test_get_bom_absent(server):
    _handshake(server)
    resp = server.call(2, "get_bom")
    assert "no bom" in _result_text(resp).lower()


def test_unknown_tool_errors(server):
    _handshake(server)
    resp = server.request(
        2, "tools/call", {"name": "does_not_exist", "arguments": {}}
    )
    assert resp["error"]["code"] == -32602
    assert "unknown tool" in resp["error"]["message"].lower()


def test_ping(server):
    _handshake(server)
    resp = server.request(2, "ping")
    assert resp["result"] == {}
