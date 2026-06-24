"""Tests for the OpenCode server-health value objects + free-port selection."""
import socket
from unittest.mock import MagicMock, patch

from workflow_editor.llm.backend_base import NoneBackend
from workflow_editor.llm.server_manager import OpenCodeServerManager


def _fake_resp(status, content_type, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = {"Content-Type": content_type}
    r.json.return_value = body
    return r


def test_external_check_rejects_html_200():
    # The OpenCode web UI (or any stray server) answers 200 with HTML — must NOT
    # be treated as the API server (that caused a chat hang).
    mgr = OpenCodeServerManager()
    with patch("workflow_editor.llm.server_manager.requests.get",
               return_value=_fake_resp(200, "text/html", None)):
        assert mgr._check_external_server() is False


def test_external_check_accepts_opencode_json():
    mgr = OpenCodeServerManager()
    body = {"$schema": "https://opencode.ai/config.json", "model": "x"}
    with patch("workflow_editor.llm.server_manager.requests.get",
               return_value=_fake_resp(200, "application/json", body)):
        assert mgr._check_external_server() is True
from workflow_editor.llm.server_health import (
    ServerError,
    ServerStatus,
    classify_install,
    find_free_port,
    is_port_conflict,
)


# --------------------------------------------------------------------------- #
# ServerStatus / messages                                                      #
# --------------------------------------------------------------------------- #

def test_healthy_status_message():
    s = ServerStatus.healthy()
    assert s.ok is True
    assert "ready" in s.message.lower()


def test_failure_message_includes_reason():
    s = ServerStatus.failure(ServerError.OPENCODE_MISSING)
    assert s.ok is False
    assert "OpenCode was not found" in s.message


def test_failure_message_appends_detail():
    s = ServerStatus.failure(ServerError.START_TIMEOUT, detail="boom traceback")
    assert "boom traceback" in s.message
    assert s.message.startswith("The OpenCode server did not become ready")


def test_failure_message_blank_detail_is_clean():
    s = ServerStatus.failure(ServerError.WSL_MISSING, detail="   ")
    assert s.message == ServerStatus.failure(ServerError.WSL_MISSING).message


# --------------------------------------------------------------------------- #
# classify_install                                                             #
# --------------------------------------------------------------------------- #

def test_classify_install_wsl_missing():
    assert classify_install(False, False) is ServerError.WSL_MISSING
    assert classify_install(False, True) is ServerError.WSL_MISSING


def test_classify_install_opencode_missing():
    assert classify_install(True, False) is ServerError.OPENCODE_MISSING


def test_classify_install_ok():
    assert classify_install(True, True) is ServerError.NONE


def test_is_port_conflict_detects_common_messages():
    assert is_port_conflict("Error: listen EADDRINUSE: address already in use 127.0.0.1:4096")
    assert is_port_conflict("bind: Address already in use")
    assert not is_port_conflict("some unrelated startup error")
    assert not is_port_conflict("")


# --------------------------------------------------------------------------- #
# find_free_port                                                               #
# --------------------------------------------------------------------------- #

def test_find_free_port_returns_preferred_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    # socket closed → the port is free again, so it should be returned as-is.
    assert find_free_port(free) == free


def test_find_free_port_avoids_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        chosen = find_free_port(taken)
        assert chosen != taken
        # the chosen port must actually be bindable.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", chosen))   # must not raise


# --------------------------------------------------------------------------- #
# NoneBackend carries the classified reason                                    #
# --------------------------------------------------------------------------- #

def test_none_backend_surfaces_reason():
    from workflow_editor.llm.backend_base import LLMRequest, LLMTask
    reason = "OpenCode was not found in the WSL PATH."
    backend = NoneBackend(reason=reason)
    resp = backend.send_request(LLMRequest(task=LLMTask.AD_HOC_CHAT))
    assert resp.success is False
    assert resp.assistant_message == reason
    assert resp.error_message == reason


def test_none_backend_default_reason():
    from workflow_editor.llm.backend_base import LLMRequest, LLMTask
    resp = NoneBackend().send_request(LLMRequest(task=LLMTask.AD_HOC_CHAT))
    assert "disabled" in resp.assistant_message.lower()
