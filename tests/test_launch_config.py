"""Tests for the OpenCode launch-config derivation in
``workflow_editor.dialogs.settings_dialog``.

Verifies the master = providers-only blueprint + derived opencode.json with the
3 MCP blocks built FRESH at runtime:

- master.json carries providers/general only — NO model/small_model/mcp.
- build_launch_config(project_root) writes launch/opencode.json with pdf_tools +
  dcdc_tools always present and project_tools present ONLY when the project has a
  ``*.tgz`` board archive (dropped otherwise).
- every MCP command path is computed at runtime (venv python via sys.executable,
  scripts via __file__, documents/rules/tgz under project_root).

The config dir is redirected to a tmp dir via monkeypatching so the test never
touches the user's real ~/.workflow_editor tree.
"""
import json
import sys
from pathlib import Path

import pytest

from workflow_editor.dialogs import settings_dialog as sd
from workflow_editor.llm.mcp_config import win_to_wsl_path


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Redirect the editor's OpenCode config dir to a tmp location."""
    cfg = tmp_path / "opencode"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sd, "get_opencode_config_dir", lambda: cfg)
    return cfg


def _expected_python_win() -> str:
    return sys.executable.replace("pythonw.exe", "python.exe")


def _script_for(name: str) -> str:
    # Mirrors settings_dialog._mcp_script_win: authoring/<name> next to the
    # dialogs package's parent (workflow_editor/authoring/).
    return str(Path(sd.__file__).resolve().parents[1] / "authoring" / name)


def test_master_path_is_master_json(config_dir):
    assert sd.get_opencode_master_path() == config_dir / "master.json"


def test_ensure_master_strips_model_and_mcp(config_dir, tmp_path):
    seed = tmp_path / "opencode.json"
    seed.write_text(json.dumps({
        "provider": {"openai": {"models": {"gpt-5": {}}}},
        "model": "openai/gpt-5",
        "small_model": "openai/gpt-5-mini",
        "mcp": {"stale_tools": {"type": "local", "command": ["x"]}},
        "theme": "dark",
    }), encoding="utf-8")

    master = sd.ensure_master_config(seed_from=seed)
    data = json.loads(master.read_text(encoding="utf-8"))

    # Providers + general carried; model/small_model/mcp stripped.
    assert "provider" in data
    assert data["theme"] == "dark"
    assert "model" not in data
    assert "small_model" not in data
    assert "mcp" not in data


def test_ensure_master_generated_once(config_dir, tmp_path):
    seed = tmp_path / "opencode.json"
    seed.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")
    master = sd.ensure_master_config(seed_from=seed)
    # User edits the master; a second ensure must NOT overwrite it.
    master.write_text(json.dumps({"provider": {"edited": {}}}), encoding="utf-8")
    sd.ensure_master_config(seed_from=seed)
    data = json.loads(master.read_text(encoding="utf-8"))
    assert data == {"provider": {"edited": {}}}


def test_build_launch_config_master_has_no_mcp(config_dir):
    # Master with providers only; no model/mcp.
    master = sd.get_opencode_master_path()
    master.write_text(json.dumps({
        "provider": {"openai": {"models": {"gpt-5": {}}}},
    }), encoding="utf-8")

    launch_dir = sd.build_launch_config(project_root=None)
    derived = json.loads((launch_dir / "opencode.json").read_text(encoding="utf-8"))

    # Master itself is unchanged (still no mcp).
    master_data = json.loads(master.read_text(encoding="utf-8"))
    assert "mcp" not in master_data
    # Providers carried through to the derived config.
    assert derived["provider"] == {"openai": {"models": {"gpt-5": {}}}}


def test_build_launch_config_no_project_drops_project_tools(config_dir):
    master = sd.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    launch_dir = sd.build_launch_config(project_root=None)
    derived = json.loads((launch_dir / "opencode.json").read_text(encoding="utf-8"))

    mcp = derived["mcp"]
    # pdf_tools + dcdc_tools always present; project_tools dropped (no tgz).
    assert set(mcp.keys()) == {"pdf_tools", "dcdc_tools"}
    assert "project_tools" not in mcp


def test_build_launch_config_fresh_mcp_paths_with_project(config_dir, tmp_path):
    master = sd.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    project = tmp_path / "demo_project"
    project.mkdir()
    tgz = project / "board.tgz"
    tgz.write_bytes(b"fake-archive")

    launch_dir = sd.build_launch_config(project_root=project)
    derived = json.loads((launch_dir / "opencode.json").read_text(encoding="utf-8"))
    mcp = derived["mcp"]

    # All three blocks present (project has a .tgz).
    assert set(mcp.keys()) == {"pdf_tools", "project_tools", "dcdc_tools"}

    py_win = _expected_python_win()
    py_wsl = win_to_wsl_path(py_win)

    # pdf_tools: command[0]=translated python, script + docs/rules dirs fresh.
    pdf_cmd = mcp["pdf_tools"]["command"]
    assert pdf_cmd[0] == py_wsl
    assert pdf_cmd[1] == _script_for("_pdf_tool_mcp.py")
    assert pdf_cmd[2] == "--documents-dir"
    assert pdf_cmd[3] == str(project / "documents")
    assert pdf_cmd[4] == "--rules-dir"
    assert pdf_cmd[5] == str(project / "bundle" / "rules")
    # The documents dir is created so the server launches cleanly.
    assert (project / "documents").is_dir()

    # project_tools: fresh tgz path from the project's first *.tgz.
    proj_cmd = mcp["project_tools"]["command"]
    assert proj_cmd[0] == py_wsl
    assert proj_cmd[1] == _script_for("_project_tools_mcp.py")
    assert proj_cmd[2] == "--odb-tgz"
    assert proj_cmd[3] == str(tgz)

    # dcdc_tools: project-independent, no per-project argv.
    dcdc_cmd = mcp["dcdc_tools"]["command"]
    assert dcdc_cmd[0] == py_wsl
    assert dcdc_cmd[1] == _script_for("_dcdc_tools_mcp.py")
    assert len(dcdc_cmd) == 2


def test_build_launch_config_atomic_write_in_launch_dir(config_dir):
    """The derived config is the only file left in launch/ — no temp turds."""
    master = sd.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    launch_dir = sd.build_launch_config(project_root=None)
    leftovers = [p.name for p in launch_dir.iterdir()]
    assert leftovers == ["opencode.json"]


def test_build_launch_config_missing_master_still_writes(config_dir):
    """No master.json yet -> derive from an empty config (mcp still built)."""
    assert not sd.get_opencode_master_path().exists()
    launch_dir = sd.build_launch_config(project_root=None)
    derived = json.loads((launch_dir / "opencode.json").read_text(encoding="utf-8"))
    assert set(derived["mcp"].keys()) == {"pdf_tools", "dcdc_tools"}


def test_build_launch_config_strips_model_defensively(config_dir):
    """A user who edited model/small_model back into master.json must NOT have
    them leak into the derived launch config — they're stripped defensively so
    the blueprint stays project-agnostic (OpenCode auto-picks). (MINOR fix.)"""
    master = sd.get_opencode_master_path()
    master.write_text(json.dumps({
        "provider": {"a": {}},
        "model": "openai/gpt-5",
        "small_model": "openai/gpt-5-mini",
    }), encoding="utf-8")

    launch_dir = sd.build_launch_config(project_root=None)
    derived = json.loads((launch_dir / "opencode.json").read_text(encoding="utf-8"))
    assert "model" not in derived
    assert "small_model" not in derived


def test_build_launch_config_honours_launch_dir_override(config_dir):
    """Test Connection passes an explicit launch_dir (launch_test/) so its
    derived config + pid file never collide with the live launch/ dir.
    (BLOCKER B isolation.)"""
    master = sd.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    test_dir = config_dir / "launch_test"
    out = sd.build_launch_config(project_root=None, launch_dir=test_dir)

    # Written into the override dir, NOT the live launch/ dir.
    assert out == test_dir
    assert (test_dir / "opencode.json").exists()
    live = config_dir / "launch" / "opencode.json"
    assert not live.exists()


def test_launch_test_dir_is_separate_from_live(config_dir):
    """The throwaway Test-Connection dir is a distinct sibling of the live one,
    so a probe's pid file can never overwrite/delete the live server's."""
    assert sd.get_opencode_launch_test_dir() != sd.get_opencode_launch_dir()
    assert sd.get_opencode_launch_test_dir().name == "launch_test"
    assert sd.get_opencode_launch_dir().name == "launch"
