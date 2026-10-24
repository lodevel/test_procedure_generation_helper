"""Tests for the OpenCode launch-config derivation in
``procedo.studio.llm.launch_config``.

Verifies the master = providers-only blueprint + derived opencode.json with the
3 MCP blocks built FRESH at runtime:

- master.json carries providers/general only — NO model/small_model/mcp.
- build_launch_config(project_root) writes launch/opencode-<pid>.json with pdf_tools +
  dcdc_tools always present and project_tools present ONLY when the project has a
  ``*.tgz`` board archive (dropped otherwise).
- every MCP command path is computed at runtime (venv python via sys.executable,
  scripts via __file__, documents/rules/tgz under project_root).

The config dir is redirected to a tmp dir via monkeypatching so the test never
touches the user's real settings folder.
"""
import json
import sys
from pathlib import Path

import pytest

from procedo.studio.llm import launch_config as lc
from procedo.studio.llm import launch_files
from procedo.studio.llm.mcp_config import win_to_wsl_path


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Redirect the editor's OpenCode config dir to a tmp location."""
    cfg = tmp_path / "opencode"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lc, "get_opencode_config_dir", lambda: cfg)
    return cfg


def _config(launch_dir: Path) -> Path:
    """THIS Studio process's derived config in *launch_dir*."""
    return launch_files.launch_file_path(launch_dir, launch_files.CONFIG_FILE)


def _expected_python_win() -> str:
    return sys.executable.replace("pythonw.exe", "python.exe")


def _script_for(name: str) -> str:
    # Mirrors launch_config._mcp_script_win: authoring/<name> next to the
    # llm package's parent (procedo/studio/authoring/).
    return str(Path(lc.__file__).resolve().parents[1] / "authoring" / name)


def test_master_path_is_master_json(config_dir):
    assert lc.get_opencode_master_path() == config_dir / "master.json"


def test_ensure_master_strips_model_and_mcp(config_dir, tmp_path):
    seed = tmp_path / "opencode.json"
    seed.write_text(json.dumps({
        "provider": {"openai": {"models": {"gpt-5": {}}}},
        "model": "openai/gpt-5",
        "small_model": "openai/gpt-5-mini",
        "mcp": {"stale_tools": {"type": "local", "command": ["x"]}},
        "theme": "dark",
    }), encoding="utf-8")

    master = lc.ensure_master_config(seed_from=seed)
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
    master = lc.ensure_master_config(seed_from=seed)
    # User edits the master; a second ensure must NOT overwrite it.
    master.write_text(json.dumps({"provider": {"edited": {}}}), encoding="utf-8")
    lc.ensure_master_config(seed_from=seed)
    data = json.loads(master.read_text(encoding="utf-8"))
    assert data == {"provider": {"edited": {}}}


def test_build_launch_config_master_has_no_mcp(config_dir):
    # Master with providers only; no model/mcp.
    master = lc.get_opencode_master_path()
    master.write_text(json.dumps({
        "provider": {"openai": {"models": {"gpt-5": {}}}},
    }), encoding="utf-8")

    launch_dir = lc.build_launch_config(project_root=None)
    derived = json.loads(_config(launch_dir).read_text(encoding="utf-8"))

    # Master itself is unchanged (still no mcp).
    master_data = json.loads(master.read_text(encoding="utf-8"))
    assert "mcp" not in master_data
    # Providers carried through to the derived config.
    assert derived["provider"] == {"openai": {"models": {"gpt-5": {}}}}


def test_build_launch_config_no_project_drops_project_tools(config_dir):
    master = lc.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    launch_dir = lc.build_launch_config(project_root=None)
    derived = json.loads(_config(launch_dir).read_text(encoding="utf-8"))

    mcp = derived["mcp"]
    # pdf_tools always present; project_tools dropped (no project / no tgz).
    # Skill-tool blocks are discovered dynamically (BUILTIN tier) — not asserted
    # as a fixed set here since discovery depends on the installed environment.
    assert "pdf_tools" in mcp
    assert "project_tools" not in mcp


def test_build_launch_config_fresh_mcp_paths_with_project(config_dir, tmp_path):
    master = lc.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    project = tmp_path / "demo_project"
    project.mkdir()
    tgz = project / "board.tgz"
    tgz.write_bytes(b"fake-archive")
    (project / "bundle" / "rules").mkdir(parents=True)  # a loadable bundle with rules
    (project / "bundle" / "bundle.json").write_text(
        json.dumps({"id": "baseline", "version": "2.10.0"}), encoding="utf-8")
    (project / "bundle" / "defaults.json").write_text("{}", encoding="utf-8")

    # Create a deterministic BUNDLED tool folder so we can assert its command.
    myfix_dir = project / "bundle" / "authoring_skills" / "myfix"
    myfix_dir.mkdir(parents=True)
    (myfix_dir / "tools.json").write_text(
        json.dumps({"server": "myfix_tools", "tools": ["t"]}), encoding="utf-8")
    (myfix_dir / "tools.py").write_text(
        'SERVER_NAME = "myfix_tools"\nTOOLS = [{"name": "t", "description": "d",'
        ' "inputSchema": {"type": "object"}, "handler": lambda a: "ok"}]\n',
        encoding="utf-8",
    )

    launch_dir = lc.build_launch_config(project_root=project)
    derived = json.loads(_config(launch_dir).read_text(encoding="utf-8"))
    mcp = derived["mcp"]

    # pdf_tools + project_tools + at least myfix_tools (BUNDLED) must be present.
    assert "pdf_tools" in mcp
    assert "project_tools" in mcp
    assert "myfix_tools" in mcp

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
    assert pdf_cmd[6:] == ["--project-rules-dir", str(project / "config" / "project_rules")]
    # The documents dir is created so the server launches cleanly.
    assert (project / "documents").is_dir()

    # project_tools: fresh tgz path from the project's first *.tgz.
    proj_cmd = mcp["project_tools"]["command"]
    assert proj_cmd[0] == py_wsl
    assert proj_cmd[1] == _script_for("_project_tools_mcp.py")
    assert proj_cmd[2] == "--odb-tgz"
    assert proj_cmd[3] == str(tgz)

    # myfix_tools: generic _skill_tools_mcp.py with --tools-dir pointing at the
    # BUNDLED folder; replaces the old hard-wired dcdc_tools / _dcdc_tools_mcp.py.
    myfix_cmd = mcp["myfix_tools"]["command"]
    assert myfix_cmd[0] == py_wsl
    assert myfix_cmd[1] == _script_for("_skill_tools_mcp.py")
    assert myfix_cmd[2] == "--tools-dir"
    assert myfix_cmd[3] == str(myfix_dir)


def test_build_launch_config_atomic_write_in_launch_dir(config_dir):
    """The derived config is the only file left in launch/ — no temp turds."""
    master = lc.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    launch_dir = lc.build_launch_config(project_root=None)
    leftovers = [p.name for p in launch_dir.iterdir()]
    assert leftovers == [_config(launch_dir).name]


def test_build_launch_config_missing_master_still_writes(config_dir):
    """No master.json yet -> derive from an empty config (mcp still built)."""
    assert not lc.get_opencode_master_path().exists()
    launch_dir = lc.build_launch_config(project_root=None)
    derived = json.loads(_config(launch_dir).read_text(encoding="utf-8"))
    # pdf_tools is always present; skill-tool blocks discovered dynamically.
    assert "pdf_tools" in derived["mcp"]


def test_build_launch_config_strips_model_defensively(config_dir):
    """A user who edited model/small_model back into master.json must NOT have
    them leak into the derived launch config — they're stripped defensively so
    the blueprint stays project-agnostic (OpenCode auto-picks). (MINOR fix.)"""
    master = lc.get_opencode_master_path()
    master.write_text(json.dumps({
        "provider": {"a": {}},
        "model": "openai/gpt-5",
        "small_model": "openai/gpt-5-mini",
    }), encoding="utf-8")

    launch_dir = lc.build_launch_config(project_root=None)
    derived = json.loads(_config(launch_dir).read_text(encoding="utf-8"))
    assert "model" not in derived
    assert "small_model" not in derived


def test_build_launch_config_honours_launch_dir_override(config_dir):
    """Test Connection passes an explicit launch_dir (launch_test/) so its
    derived config + pid file never collide with the live launch/ dir.
    (BLOCKER B isolation.)"""
    master = lc.get_opencode_master_path()
    master.write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")

    test_dir = config_dir / "launch_test"
    out = lc.build_launch_config(project_root=None, launch_dir=test_dir)

    # Written into the override dir, NOT the live launch/ dir.
    assert out == test_dir
    assert _config(test_dir).exists()
    assert not _config(config_dir / "launch").exists()


def test_launch_test_dir_is_separate_from_live(config_dir):
    """The throwaway Test-Connection dir is a distinct sibling of the live one,
    so a probe's pid file can never overwrite/delete the live server's."""
    assert lc.get_opencode_launch_test_dir() != lc.get_opencode_launch_dir()
    assert lc.get_opencode_launch_test_dir().name == "launch_test"
    assert lc.get_opencode_launch_dir().name == "launch"


def test_each_studio_process_builds_its_own_launch_config(config_dir, monkeypatch, tmp_path):
    """Two Studios on two projects: the second one's build never overwrites the first one's
    config (its MCP paths), which the first one's next server relaunch reads."""
    import os

    lc.get_opencode_master_path().write_text(json.dumps({"provider": {"a": {}}}), encoding="utf-8")
    first_project, second_project = tmp_path / "first", tmp_path / "second"
    monkeypatch.setattr(os, "getpid", lambda: 1111)
    first = _config(lc.build_launch_config(project_root=first_project))
    built_first = first.read_bytes()
    monkeypatch.setattr(os, "getpid", lambda: 2222)
    second = _config(lc.build_launch_config(project_root=second_project))
    assert (first.name, second.name) == ("opencode-1111.json", "opencode-2222.json")
    assert first.read_bytes() == built_first
    second_in_json = json.dumps(str(second_project))[1:-1]  # its documents dir, its rules
    assert second_in_json in second.read_text(encoding="utf-8")
    assert second_in_json not in built_first.decode("utf-8")


def test_the_run_skill_universe_file_is_written_atomically(tmp_path, monkeypatch):
    """A child skill reading the gate universe never sees a half-written file."""
    written = []
    real = lc.atomic_write_text
    monkeypatch.setattr(lc, "atomic_write_text", lambda path, text: (written.append(path), real(path, text)))
    monkeypatch.setattr("procedo.studio.authoring.tool_folders.build_skill_tools_universe",
                        lambda project_root: {"srv": ["tool"]})
    lc._build_run_skill_block(tmp_path / "proj", tmp_path / "launch", "secret")
    universe = launch_files.launch_file_path(tmp_path / "launch", launch_files.UNIVERSE_FILE)
    assert written == [universe]
    assert json.loads(universe.read_text(encoding="utf-8")) == {"srv": ["tool"]}


def test_the_master_blueprint_is_written_atomically(config_dir, monkeypatch, tmp_path):
    """Another Studio reading master.json never sees a half-written blueprint."""
    seed = tmp_path / "opencode.json"
    seed.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    written = []
    real = lc.atomic_write_text
    monkeypatch.setattr(lc, "atomic_write_text", lambda path, text: (written.append(path), real(path, text)))
    master = lc.ensure_master_config(seed_from=seed)
    assert written == [master]
    assert json.loads(master.read_text(encoding="utf-8")) == {"theme": "dark"}
