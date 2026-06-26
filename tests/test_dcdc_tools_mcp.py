"""End-to-end test of _skill_tools_mcp driving the dcdc_bringup tool folder.

Speaks newline-delimited JSON-RPC over the server's stdin/stdout, exactly as the
editor would, and verifies the handshake, the single advertised tool + its
inputSchema, and that ``tools/call generate_dcdc_test`` with the benchmark
+MAIN_5V0 params returns the canonical procedure text (matching the deterministic
generator), plus a missing-required-field validation path.

Skipped when the parent repo's authoring_skills/dcdc_bringup folder is absent
(i.e. the submodule is tested in isolation without the parent repo installed).
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

# The generic tool-folder MCP server (replaces the deleted _dcdc_tools_mcp).
import workflow_editor.authoring._skill_tools_mcp as _mcp_mod
# The pure generator, to compute the EXPECTED text the tool must reproduce.
from workflow_editor.authoring import dcdc_test_generator as gen

SERVER_PATH = os.path.abspath(_mcp_mod.__file__)

# The dcdc_bringup tool folder lives in the PARENT repo:
# __file__ -> tests/ -> test_procedure_generation_helper/ -> external/ -> test_procedure_gui/
_DCDC_BRINGUP = Path(__file__).parents[3] / "authoring_skills" / "dcdc_bringup"


# The benchmark +MAIN_5V0 params (always-on enable, power-good present), as the
# LLM would assemble them from Stages 1-4 — board entry P4/P2, rail TP MAIN_5V0,
# PG TP PG_5V0, 28 V / 10 A input.
MAIN_5V0_ARGS = {
    "rail_name": "+MAIN_5V0",
    "ic_refdes": "U86",
    "ic_part": "LT8609AJDDM#TRPBF",
    "vout_nominal_v": 5.0,
    "rail_test_point": "MAIN_5V0",
    "psu": {
        "input_voltage_v": 28.0,
        "input_current_a": 10.0,
        "entry_pos": "P4",
        "entry_neg": "P2",
    },
    "enable": {"present": True, "always_on": True},
    "power_good": {"present": True, "test_point": "PG_5V0"},
}


class _Server:
    """Line-oriented JSON-RPC client over a server subprocess."""

    def __init__(self, tools_dir, cwd):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_PATH, "--tools-dir", str(tools_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd),
        )

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout=15):
        # readline() on a piped subprocess can block; guard with a watchdog
        # thread that kills the process so the test fails fast instead of hanging.
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
        return self.request(rid, "tools/call", params)

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


@pytest.fixture
def server(tmp_path):
    if not _DCDC_BRINGUP.is_dir():
        pytest.skip("parent repo dcdc skill not present")
    # Launch from an unrelated cwd to prove launch-location independence.
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    srv = _Server(_DCDC_BRINGUP, other_cwd)
    yield srv
    srv.close()


def _handshake(srv):
    init = srv.request(1, "initialize", {"protocolVersion": "2024-11-05"})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    # serverInfo.name is now "dcdc_tools" (SERVER_NAME in tools.py),
    # not the old hard-coded "dcdc-tools" from the deleted _dcdc_tools_mcp.py.
    assert init["result"]["serverInfo"]["name"] == "dcdc_tools"
    srv.notify("notifications/initialized")  # no response expected


def test_tools_list_advertises_generate_dcdc_test(server):
    _handshake(server)
    resp = server.request(2, "tools/list")
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"generate_dcdc_test"}
    tool = tools[0]
    assert tool.get("description", "").strip()
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    # The schema's required list = the generator's top-level required params.
    assert set(schema["required"]) == {
        "rail_name", "ic_refdes", "ic_part", "vout_nominal_v",
        "rail_test_point", "psu", "enable", "power_good",
    }
    # Nested objects carry their own required lists (psu's four entries).
    assert set(schema["properties"]["psu"]["required"]) == {
        "input_voltage_v", "input_current_a", "entry_pos", "entry_neg",
    }
    assert schema["properties"]["enable"]["required"] == ["present"]
    assert schema["properties"]["power_good"]["required"] == ["present"]


def test_generate_dcdc_test_returns_canonical_text(server):
    _handshake(server)
    resp = server.call(2, "generate_dcdc_test", MAIN_5V0_ARGS)
    text = _result_text(resp)

    # The tool's output must EQUAL the pure generator's output for these params
    # (the tool is a thin transport over the generator — no drift).
    params = gen.DcDcTestParams(
        rail_name="+MAIN_5V0",
        ic_refdes="U86",
        ic_part="LT8609AJDDM#TRPBF",
        vout_nominal_v=5.0,
        rail_test_point="MAIN_5V0",
        psu=gen.PsuParams(input_voltage_v=28.0, input_current_a=10.0,
                          entry_pos="P4", entry_neg="P2"),
        enable=gen.EnableParams(present=True, always_on=True),
        power_good=gen.PowerGoodParams(present=True, test_point="PG_5V0"),
    )
    assert text == gen.generate_dcdc_test(params)

    # Structural sanity matching the benchmark report_push.json +MAIN_5V0 block.
    assert text.startswith("## Equipment")
    assert "SCOPE1 : scope channels=[1, 2]" in text  # CH2 present (power-good)
    assert "Set PSU1 CH1 output = OFF." in text       # PSU-off-first wiring
    assert "Connect PSU1 CH1 + to P4, - to P2." in text
    assert "scale=2.0 V/div" in text                  # CH1 V/div from 5 V rail
    assert "Configure SCOPE1 timebase: position=0.0 s, scale=10.0 ms/div." in text
    assert "{3} = 5.0 V +/- 3.0 %" in text            # DC pass/fail
    assert "{4} = 3.3 V +/- 10.0 %" in text           # PG level pass/fail
    assert "{5} <= 100.0 mV" in text                  # ripple (2% of 5 V)
    # No PG-disable step (v0.13.0 forbids it).
    assert "de-assert" not in text.lower()


def test_generate_dcdc_test_missing_field_returns_validation_message(server):
    _handshake(server)
    bad = json.loads(json.dumps(MAIN_5V0_ARGS))
    del bad["rail_test_point"]  # drop a required field
    resp = server.call(2, "generate_dcdc_test", bad)
    text = _result_text(resp)
    assert "rail_test_point" in text
    assert "call generate_dcdc_test again" in text.lower()


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
