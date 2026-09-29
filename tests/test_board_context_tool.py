"""Exercise the real MCP dispatcher; fake only the expensive ODB CLI boundary."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from workflow_editor.authoring import _mcp_serve, _project_tools_mcp as server
from workflow_editor.authoring.netlist_text import board_revision


def _board_data():
    components = [
        {"refdes": "MCU_A", "side": "TOP", "pins": [
            {"name": "24", "net": "RESET"}, {"name": "25", "net": "GPIO"}],
         "properties": {"Vendor reference": "STM32_TEST", "EDA.category": "MCU"}},
        {"refdes": "R7", "side": "TOP", "pins": [
            {"name": "1", "net": "GPIO"}, {"name": "2", "net": "PROBE"}],
         "properties": {"VALUE": "100 ohm", "EDA.category": "Resistor"}},
        {"refdes": "TP7", "side": "BOTTOM", "pins": [
            {"name": "1", "net": "RESET"}],
         "properties": {"arbitrary.TestAccess": "yes", "Package": "TEST_PAD"}},
        {"refdes": "PAD9", "side": "TOP", "pins": [
            {"name": "1", "net": "PROBE"}],
         "properties": {"arbitrary.TestAccess": "yes", "Package": "TEST_PAD"}},
        {"refdes": "J1", "side": "TOP", "pins": [
            {"name": "1", "net": None}], "properties": {}},
    ]
    nets = {}
    for comp in components:
        for pin in comp["pins"]:
            if pin["net"] is not None:
                nets.setdefault(pin["net"], []).append(
                    {"refdes": comp["refdes"], "pin": pin["name"]})
    return {"components": components,
            "nets": [{"net": name, "nodes": nodes} for name, nodes in nets.items()]}


@pytest.fixture
def board(tmp_path, monkeypatch):
    archive = tmp_path / "board.tgz"
    archive.write_bytes(b"synthetic archive version one")
    calls = []
    data = _board_data()

    def cli(path, args):
        assert path == str(archive)
        calls.append(args)
        assert args == ["--list-nets"]
        return deepcopy(data)

    monkeypatch.setattr(server, "_run_cli", cli)
    return server._Board(str(archive)), archive, data, calls


def _call(board, monkeypatch):
    replies = []
    monkeypatch.setattr(_mcp_serve, "send", replies.append)
    dispatch = {name: lambda args, handler=handler: handler(board, args)
                for name, handler in server._DISPATCH.items()}
    _mcp_serve._dispatch_call(17, {"name": "board_context", "arguments": {}}, dispatch)
    assert len(replies) == 1
    assert replies[0]["id"] == 17
    assert "error" not in replies[0], replies[0]
    return replies[0]["result"]


def _text(result):
    return result["content"][0]["text"]


def test_constructing_and_advertising_board_tool_does_not_read_archive(board, monkeypatch):
    _, _, _, calls = board
    monkeypatch.setattr(server._netlist_text, "board_revision",
                        lambda _: pytest.fail("enabling must not read board"))
    server._Board("missing.tgz")
    tool = next(item for item in server.TOOLS if item["name"] == "board_context")
    assert tool["inputSchema"]["properties"] == {}
    assert calls == []


def test_one_request_contains_all_wiring_identities_and_completion_marker(board, monkeypatch):
    cached, archive, _, calls = board
    result = _call(cached, monkeypatch)
    assert not result.get("isError")
    text = _text(result)
    assert text.startswith(f"Board revision: {board_revision(str(archive))}\n")
    counts = ("components=5, pins=7, nets=3, nodes=6, "
              "components_with_properties=4, pins_with_unknown_net=1")
    assert f"Board context: {counts}" in text
    assert text.endswith(f"BOARD_CONTEXT_END: {counts}")
    for line in ("- MCU_A [TOP]: 24=RESET, 25=GPIO", "- R7 [TOP]: 1=GPIO, 2=PROBE",
                 "- RESET: MCU_A.24, TP7.1", "- GPIO: MCU_A.25, R7.1",
                 "- PROBE: R7.2, PAD9.1", "- J1 [TOP]: 1=-",
                 "Vendor reference='STM32_TEST'", "EDA.category='Resistor'",
                 "arbitrary.TestAccess='yes'", "Package='TEST_PAD'"):
        assert line in text
    assert calls == [["--list-nets"]]


def test_repeated_requests_use_one_cached_parse(board, monkeypatch):
    cached, _, _, calls = board
    first = _call(cached, monkeypatch)
    assert _call(cached, monkeypatch) == first
    assert calls == [["--list-nets"]]


def test_large_board_keeps_final_component_properties_and_net(board, monkeypatch):
    cached, _, data, _ = board
    for index in range(2100):
        refdes, net = f"P{index}", f"SIGNAL_{index}"
        data["components"].append({"refdes": refdes, "side": "TOP",
                                   "pins": [{"name": "1", "net": net}],
                                   "properties": {"BoardKind": f"TEST_ACCESS_{index}"}})
        data["nets"].append({"net": net, "nodes": [{"refdes": refdes, "pin": "1"}]})
    text = _text(_call(cached, monkeypatch))
    assert "components=2105, pins=2107, nets=2103, nodes=2106" in text
    assert "- P2099 [TOP]: 1=SIGNAL_2099" in text
    assert "- SIGNAL_2099: P2099.1" in text
    assert "- P2099: BoardKind='TEST_ACCESS_2099'" in text
    assert text.splitlines()[-1].startswith("BOARD_CONTEXT_END:")


@pytest.mark.parametrize("spare_bytes", [-1, 0, 1])
def test_board_context_byte_limit_includes_revision_and_exact_boundary(
        board, monkeypatch, spare_bytes):
    cached, _, _, calls = board
    complete = _text(_call(cached, monkeypatch))
    size = len(complete.encode("utf-8"))
    limit = size + spare_bytes
    monkeypatch.setattr(server._netlist_text, "BOARD_CONTEXT_MAX_BYTES", limit)
    result = _call(cached, monkeypatch)
    if spare_bytes >= 0:
        assert not result.get("isError")
        assert _text(result) == complete
    else:
        assert result["isError"]
        assert f"{size} UTF-8 bytes" in _text(result)
        assert f"limit is {limit} bytes" in _text(result)
        assert "BOARD_CONTEXT_END" not in _text(result)
        assert "- RESET:" not in _text(result)
    assert calls == [["--list-nets"]]


def test_oversized_unicode_board_is_rejected_whole_by_bytes_not_characters(board, monkeypatch):
    cached, archive, data, calls = board
    data["components"][0]["properties"]["Description"] = "界" * 400_000
    complete = (f"Board revision: {board_revision(str(archive))}\n"
                + server._netlist_text.format_board_context(data))
    limit = server._netlist_text.BOARD_CONTEXT_MAX_BYTES
    size = len(complete.encode("utf-8"))
    assert len(complete) < limit < size
    for _ in range(2):
        result = _call(cached, monkeypatch)
        assert result["isError"]
        text = _text(result)
        assert f"{size} UTF-8 bytes" in text
        assert f"limit is {limit} bytes" in text
        assert "complete board cannot fit in one response" in text
        assert "BOARD_CONTEXT_END" not in text and "界" not in text
        assert len(text.encode("utf-8")) < 1024
    assert calls == [["--list-nets"]]


def test_changed_archive_refreshes_properties_and_wiring_together(board, monkeypatch):
    cached, archive, data, calls = board
    old = _text(_call(cached, monkeypatch))
    archive.write_bytes(b"different archive with new component data")
    data["components"][0]["properties"]["Vendor reference"] = "NEW_MCU"
    data["components"][0]["pins"][0]["net"] = "RESET_NEW"
    data["components"][2]["pins"][0]["net"] = "RESET_NEW"
    data["nets"][0]["net"] = "RESET_NEW"
    new = _text(_call(cached, monkeypatch))
    assert new.splitlines()[0] != old.splitlines()[0]
    assert "NEW_MCU" in new and "STM32_TEST" not in new
    assert "24=RESET_NEW" in new and "- RESET_NEW: MCU_A.24, TP7.1" in new
    assert calls == [["--list-nets"], ["--list-nets"]]


def test_removed_archive_does_not_return_cached_data(board, monkeypatch):
    cached, archive, _, calls = board
    _call(cached, monkeypatch)
    archive.unlink()
    result = _call(cached, monkeypatch)
    assert result["isError"]
    assert "unavailable" in _text(result)
    assert "BOARD_CONTEXT_END" not in _text(result)
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["", "missing.tgz"])
def test_missing_archive_returns_clear_error_without_cli(path, monkeypatch):
    monkeypatch.setattr(server, "_run_cli", lambda *_: pytest.fail("no archive"))
    result = _call(server._Board(path), monkeypatch)
    assert result["isError"]
    assert "archive unavailable" in _text(result)
    assert "BOARD_CONTEXT_END" not in _text(result)


@pytest.mark.parametrize("problem", ["cli", "identities", "nets", "error", "empty"])
def test_failed_or_incomplete_board_has_no_success_marker(board, monkeypatch, problem):
    cached, _, data, _ = board
    if problem == "cli":
        def fail(*_):
            raise server._CliError("synthetic CLI failed")
        monkeypatch.setattr(server, "_run_cli", fail)
    elif problem == "identities":
        data["components"][0].pop("properties")
    elif problem == "nets":
        data.pop("nets")
    elif problem == "error":
        data["error"] = "ODB parse incomplete"
    else:
        data.update(components=[], nets=[])
    result = _call(cached, monkeypatch)
    assert result["isError"]
    assert "Could not read the project board" in _text(result)
    assert "BOARD_CONTEXT_END" not in _text(result)


def test_archive_changed_during_parse_is_not_reported_as_complete(board, monkeypatch):
    cached, archive, data, _ = board

    def changing_cli(*_):
        archive.write_bytes(b"changed during parse")
        return deepcopy(data)

    monkeypatch.setattr(server, "_run_cli", changing_cli)
    result = _call(cached, monkeypatch)
    assert result["isError"]
    assert "changed while being read" in _text(result)
    assert "BOARD_CONTEXT_END" not in _text(result)


def test_concurrent_requests_share_one_parse(board, monkeypatch):
    cached, _, data, calls = board
    entered, release = threading.Event(), threading.Event()

    def cli(*_):
        calls.append("parse")
        entered.set()
        assert release.wait(5)
        return deepcopy(data)

    monkeypatch.setattr(server, "_run_cli", cli)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cached.snapshot()))
               for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(5)
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2 and results[0] == results[1]
    assert calls == ["parse"]


@pytest.fixture
def stdio_board_server(tmp_path):
    """Real standalone server/framing with the ODB CLI replaced at its boundary.

    Avoid the environment's unsupported nested interpreter spawn while still
    exercising threaded JSON-RPC calls and the standalone formatter import.
    """
    project = tmp_path / "project"
    project.mkdir()
    # A stale startup binding must never substitute for a missing current board.
    old_archive = tmp_path / "old_board.json"
    old_archive.write_text(json.dumps(_board_data()), encoding="utf-8")
    reads = tmp_path / "cli_reads.txt"
    launcher = tmp_path / "server.py"
    authoring = str(Path(server.__file__).parent)
    launcher.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {authoring!r})\n"
        "import _project_tools_mcp as server\n"
        "def cli(path, args):\n"
        "    assert args == ['--list-nets']\n"
        f"    with open({str(reads)!r}, 'a') as log: log.write('read\\n')\n"
        "    return json.loads(Path(path).read_text(encoding='utf-8'))\n"
        "server._run_cli = cli\n"
        "server.main()\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(launcher), "--odb-tgz", str(old_archive),
         "--project-root", str(project)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(10, proc.kill)
    watchdog.start()
    def request(rid):
        message = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": "board_context", "arguments": {}}}
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, "standalone MCP server closed or timed out"
        reply = json.loads(line)
        assert reply["id"] == rid and "error" not in reply
        return reply["result"]

    try:
        yield project, reads, request
    finally:
        watchdog.cancel()
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


def test_stdio_board_context_reads_once_for_two_requests(stdio_board_server):
    project, reads, request = stdio_board_server
    archive = project / "board.tgz"
    archive.write_text(json.dumps(_board_data()), encoding="utf-8")
    replies = [request(1), request(2)]
    for result in replies:
        text = _text(result)
        assert "- RESET: MCU_A.24, TP7.1" in text
        assert "Vendor reference='STM32_TEST'" in text
        assert text.splitlines()[-1].startswith("BOARD_CONTEXT_END:")
    assert replies[0] == replies[1]
    assert reads.read_text().splitlines() == ["read"]


def test_running_stdio_server_follows_changed_project_board_selection(stdio_board_server):
    project, reads, request = stdio_board_server
    assets = project / "assets"
    assets.mkdir()
    first, second = assets / "a.tgz", assets / "b.tgz"
    first.write_text(json.dumps(_board_data()), encoding="utf-8")
    data = _board_data()
    data["components"][0]["properties"]["Vendor reference"] = "SECOND_BOARD_MCU"
    second.write_text(json.dumps(data), encoding="utf-8")
    (project / "config").mkdir()
    config = project / "config/config.json"
    config.write_text(json.dumps({"odb_archive": "assets/a.tgz"}), encoding="utf-8")
    old = _text(request(1))
    config.write_text(json.dumps({"odb_archive": "assets/b.tgz"}), encoding="utf-8")
    new = _text(request(2))
    assert old.startswith(f"Board revision: {board_revision(str(first))}\n")
    assert new.startswith(f"Board revision: {board_revision(str(second))}\n")
    assert "SECOND_BOARD_MCU" in new and "STM32_TEST" not in new
    assert reads.read_text().splitlines() == ["read", "read"]


def test_running_stdio_server_handles_added_and_removed_board(stdio_board_server):
    project, reads, request = stdio_board_server
    unavailable = request(1)
    assert unavailable["isError"] and "unavailable" in _text(unavailable)
    assert not reads.exists()
    archive = project / "added.tgz"
    archive.write_text(json.dumps(_board_data()), encoding="utf-8")
    available = request(2)
    assert not available.get("isError")
    assert "BOARD_CONTEXT_END" in _text(available)
    archive.unlink()
    unavailable_again = request(3)
    assert unavailable_again["isError"]
    assert "BOARD_CONTEXT_END" not in _text(unavailable_again)
    assert reads.read_text().splitlines() == ["read"]
