"""Real chat controls produce opt-in aggregate access without parsing a board."""

import json
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from procedo.studio.core.artifact_manager import ArtifactManager
from procedo.studio.core.project_manager import ProjectManager
from procedo.studio.core.task_config import TaskConfigManager
from procedo.studio.dock.chat_panel import ChatPanel
from procedo.studio.llm import LLMTask, OpenCodeBackend, OpenCodeConfig, TabContext


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    widget = ChatPanel(main_window=Mock())
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def make_context(root, tab_id="text_only"):
    return TabContext(
        tab_id=tab_id, backend_factory=None,
        project_manager=ProjectManager(project_root=root),
        artifact_manager=ArtifactManager(),
        task_config_manager=TaskConfigManager(root / "absent.json"),
    )


def request_body(context):
    request = context._build_request(LLMTask.AD_HOC_CHAT, user_message="Where can I probe reset?")
    body = OpenCodeBackend(OpenCodeConfig())._build_message_body("question", request)
    return request, body


def test_checkbox_gates_actual_request_without_loading_board(panel, tmp_path, monkeypatch):
    from procedo.studio.core import odb_inspect

    def forbidden(*args, **kwargs):
        pytest.fail("Enabling or building a request must not parse the board")

    monkeypatch.setattr(odb_inspect, "load_board", forbidden)
    (tmp_path / "board.tgz").write_bytes(b"board sentinel must not be sent")
    context = make_context(tmp_path)
    panel.switch_context(context)
    box = panel.board_data_checkbox
    assert not box.isHidden() and box.isEnabled() and not box.isChecked()
    _, off = request_body(context)
    assert off["tools"]["project_tools_board_context"] is False
    box.click()
    request, on = request_body(context)
    assert on["tools"]["project_tools_board_context"] is True
    assert all(not value for key, value in on["tools"].items()
               if key.startswith("project_tools_") and key != "project_tools_board_context")
    assert on["tools"]["pdf_tools_read_rule"] is True
    assert "board sentinel" not in str(request)
    assert "Reuse that result" in on["system"]
    box.click()
    _, off_again = request_body(context)
    assert off_again["tools"]["project_tools_board_context"] is False


def test_access_stays_with_text_tab_and_project(panel, tmp_path):
    (tmp_path / "board.tgz").write_bytes(b"board")
    text = make_context(tmp_path)
    panel.switch_context(text)
    panel.board_data_checkbox.click()
    other = make_context(tmp_path, "text_json")
    panel.switch_context(other)
    assert panel.board_data_checkbox.isHidden()
    assert request_body(other)[1]["tools"]["project_tools_board_context"] is False
    panel.switch_context(text)
    assert panel.board_data_checkbox.isChecked()
    new_project = tmp_path / "second"
    new_project.mkdir()
    (new_project / "board.tgz").write_bytes(b"other board")
    text.project_manager.project_root = new_project
    # Even before the panel is refreshed, a request cannot inherit permission.
    assert request_body(text)[1]["tools"]["project_tools_board_context"] is False
    panel.switch_context(text)
    assert not panel.board_data_checkbox.isChecked()
    panel.switch_context(None)
    assert panel.board_data_checkbox.isHidden()


def test_revision_changes_without_eager_payload_and_reset_allows_refetch(panel, tmp_path):
    archive = tmp_path / "board.tgz"
    archive.write_bytes(b"original")
    context = make_context(tmp_path)
    panel.switch_context(context)
    panel.board_data_checkbox.click()
    before = request_body(context)[1]["system"]
    assert request_body(context)[1]["system"] == before
    archive.write_bytes(b"different board version")
    after = request_body(context)[1]["system"]
    assert after != before
    assert "changed revision supersedes" in after
    context.reset_conversation()
    assert request_body(context)[1]["tools"]["project_tools_board_context"] is True
    archive.unlink()
    _, gone = request_body(context)
    assert gone["tools"]["project_tools_board_context"] is False
    assert "unavailable" in gone["system"]


def test_no_board_is_optional_and_configured_archive_is_shared(panel, tmp_path, monkeypatch):
    from procedo.studio.dialogs import settings_dialog

    context = make_context(tmp_path)
    panel.switch_context(context)
    assert not panel.board_data_checkbox.isEnabled()
    assert request_body(context)[1]["tools"]["project_tools_board_context"] is False
    monkeypatch.setattr(settings_dialog, "_python_win", lambda: "C:/Python/python.exe")
    empty_block = settings_dialog._build_project_tools_block(tmp_path)
    # Register the lazy server even before a board exists, so a later project
    # board selection can be used without restarting the editor.
    assert "--project-root" in empty_block["project_tools"]["command"]
    assert settings_dialog._build_project_tools_block(None) is None
    (tmp_path / "config").mkdir()
    configured = tmp_path / "boards" / "chosen.tgz"
    configured.parent.mkdir()
    configured.write_bytes(b"chosen")
    (tmp_path / "wrong.tgz").write_bytes(b"fallback must not win")
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"odb_archive": "boards/chosen.tgz"}), encoding="utf-8")
    block = settings_dialog._build_project_tools_block(tmp_path)
    command = block["project_tools"]["command"]
    assert command[command.index("--odb-tgz") + 1] == str(configured)
    assert command[command.index("--project-root") + 1] == str(tmp_path)
    panel.switch_context(context)
    assert panel.board_data_checkbox.isEnabled()
    panel.board_data_checkbox.click()
    from procedo.studio.authoring.netlist_text import board_revision
    assert board_revision(configured) in request_body(context)[1]["system"]


@pytest.mark.parametrize("configured_limit", [1024, 2 * 1024 * 1024])
def test_launch_transport_preserves_complete_admitted_board(tmp_path, monkeypatch, configured_limit):
    from procedo.studio.authoring.netlist_text import BOARD_CONTEXT_MAX_BYTES
    from procedo.studio.dialogs import settings_dialog as settings

    master = tmp_path / "master.json"
    master.write_text(json.dumps({
        "tool_output": {"max_bytes": configured_limit, "max_lines": configured_limit},
        "provider": {"example": {"name": "keep provider settings"}},
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "get_opencode_master_path", lambda: master)
    monkeypatch.setattr(settings, "_python_win", lambda: "C:/Python/python.exe")
    monkeypatch.setattr(settings, "_build_pdf_tools_block", lambda root: {})
    monkeypatch.setattr(settings, "_build_run_skill_block", lambda *args: None)
    monkeypatch.setattr(settings, "_build_skill_tools_blocks", lambda root: {})
    original = master.read_bytes()
    launch = settings.build_launch_config(tmp_path, launch_dir=tmp_path / "launch")
    config = json.loads((launch / "opencode.json").read_text(encoding="utf-8"))
    assert config["tool_output"] == {
        "max_bytes": max(configured_limit, BOARD_CONTEXT_MAX_BYTES),
        "max_lines": max(configured_limit, BOARD_CONTEXT_MAX_BYTES),
    }
    assert config["provider"]["example"]["name"] == "keep provider settings"
    assert master.read_bytes() == original
