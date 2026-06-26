"""Tests for workflow_editor.llm.mcp_config (pure path/dict helpers)."""
import json
from pathlib import Path

import pytest

from workflow_editor.llm import mcp_config
from workflow_editor.authoring.skill import SkillSource
from workflow_editor.authoring.tool_folders import discover_tool_folders


def test_win_to_wsl_path_basic():
    assert mcp_config.win_to_wsl_path(r"C:\A\B") == "/mnt/c/A/B"


def test_win_to_wsl_path_lowercases_drive():
    assert mcp_config.win_to_wsl_path(r"D:\Foo\Bar.py") == "/mnt/d/Foo/Bar.py"


def test_win_to_wsl_path_forward_slash_input():
    assert mcp_config.win_to_wsl_path("C:/A/B") == "/mnt/c/A/B"


def test_win_to_wsl_path_idempotent_on_posix():
    assert mcp_config.win_to_wsl_path("/mnt/c/A/B") == "/mnt/c/A/B"
    assert mcp_config.win_to_wsl_path("/usr/bin/python") == "/usr/bin/python"


def test_win_to_wsl_path_unc_passthrough():
    assert mcp_config.win_to_wsl_path(r"\\server\share\x") == r"\\server\share\x"


def test_build_pdf_tools_mcp_block_shape():
    block = mcp_config.build_pdf_tools_mcp_block(
        venv_python_win=r"C:\Workspace\.venv\Scripts\python.exe",
        mcp_script_win=r"C:\Workspace\editor\_pdf_tool_mcp.py",
        documents_dir_win=r"C:\Projects\demo\documents",
        rules_dir_win=r"C:\Projects\demo\bundle\rules",
    )
    assert set(block.keys()) == {"pdf_tools"}
    entry = block["pdf_tools"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["command"] == [
        "/mnt/c/Workspace/.venv/Scripts/python.exe",  # command[0] translated
        r"C:\Workspace\editor\_pdf_tool_mcp.py",       # script stays Windows
        "--documents-dir",
        r"C:\Projects\demo\documents",                 # docs dir stays Windows
        "--rules-dir",
        r"C:\Projects\demo\bundle\rules",              # rules dir stays Windows
    ]


def test_build_project_tools_mcp_block_shape():
    block = mcp_config.build_project_tools_mcp_block(
        venv_python_win=r"C:\Workspace\.venv\Scripts\python.exe",
        mcp_script_win=r"C:\Workspace\editor\_project_tools_mcp.py",
        odb_tgz_win=r"C:\Projects\demo\board.tgz",
    )
    assert set(block.keys()) == {"project_tools"}
    entry = block["project_tools"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["command"] == [
        "/mnt/c/Workspace/.venv/Scripts/python.exe",   # command[0] translated
        r"C:\Workspace\editor\_project_tools_mcp.py",   # script stays Windows
        "--odb-tgz",
        r"C:\Projects\demo\board.tgz",                  # tgz stays Windows
    ]


# ---------------------------------------------------------------------------
# build_skill_tools_mcp_block
# ---------------------------------------------------------------------------

def test_build_skill_tools_mcp_block_shape():
    block = mcp_config.build_skill_tools_mcp_block(
        server_name="my_tools",
        venv_python_win=r"C:\Workspace\.venv\Scripts\python.exe",
        mcp_script_win=r"C:\Workspace\editor\_skill_tools_mcp.py",
        tools_dir_win=r"C:\Projects\skills\my_skill",
    )
    # Block key == server_name.
    assert set(block.keys()) == {"my_tools"}
    entry = block["my_tools"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    cmd = entry["command"]
    # command[0] is the WSL-translated python path.
    assert cmd[0] == "/mnt/c/Workspace/.venv/Scripts/python.exe"
    # Script stays as given (Windows python receives it as an argv).
    assert cmd[1] == r"C:\Workspace\editor\_skill_tools_mcp.py"
    # --tools-dir followed by the tools folder path.
    assert cmd[2] == "--tools-dir"
    assert cmd[3] == r"C:\Projects\skills\my_skill"


# ---------------------------------------------------------------------------
# skill_tool_overrides
# ---------------------------------------------------------------------------

def test_skill_tool_overrides_active_server_true_others_false():
    universe = {"srv_a": ["t1", "t2"], "srv_b": ["t3"]}
    overrides = mcp_config.skill_tool_overrides(["srv_a"], universe)
    assert overrides == {
        "srv_a_t1": True,
        "srv_a_t2": True,
        "srv_b_t3": False,
    }


def test_skill_tool_overrides_empty_active_all_false():
    universe = {"srv_a": ["t1"], "srv_b": ["t2"]}
    overrides = mcp_config.skill_tool_overrides([], universe)
    assert overrides == {"srv_a_t1": False, "srv_b_t2": False}


def test_skill_tool_overrides_empty_universe_empty_result():
    assert mcp_config.skill_tool_overrides(["any"], {}) == {}


# ---------------------------------------------------------------------------
# discover_tool_folders
# ---------------------------------------------------------------------------

def _make_folder(parent: Path, name: str, server: str, tools: list) -> Path:
    """Create a valid tool folder under ``parent/name``."""
    folder = parent / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tools.json").write_text(
        json.dumps({"server": server, "tools": tools}), encoding="utf-8")
    (folder / "tools.py").write_text(
        f'SERVER_NAME = "{server}"\nTOOLS = []\n', encoding="utf-8")
    return folder


def test_discover_tool_folders_finds_good_folder(tmp_path):
    root = tmp_path / "skills"
    _make_folder(root, "myfolder", "my_tools", ["t"])
    results = discover_tool_folders([(root, SkillSource.BUILTIN)])
    assert len(results) == 1
    assert results[0].server == "my_tools"
    assert results[0].tools == ["t"]
    assert results[0].source == SkillSource.BUILTIN


def test_discover_tool_folders_drops_bad_charset(tmp_path):
    root = tmp_path / "skills"
    _make_folder(root, "bad_folder", "BAD-NAME", ["t"])  # hyphen not allowed
    results = discover_tool_folders([(root, SkillSource.BUILTIN)])
    assert results == []


def test_discover_tool_folders_drops_reserved_name(tmp_path):
    root = tmp_path / "skills"
    _make_folder(root, "infra_folder", "project_tools", ["t"])
    _make_folder(root, "pdf_folder", "pdf_tools", ["t"])
    results = discover_tool_folders([(root, SkillSource.BUILTIN)])
    assert results == []


def test_discover_tool_folders_excludes_project_tier(tmp_path):
    root = tmp_path / "skills"
    _make_folder(root, "proj_folder", "proj_tools", ["t"])
    # PROJECT tier is excluded (only BUILTIN/BUNDLED are trusted).
    results = discover_tool_folders([(root, SkillSource.PROJECT)])
    assert results == []


def test_discover_tool_folders_bundled_wins_over_builtin_same_folder(tmp_path):
    builtin_root = tmp_path / "builtin"
    bundled_root = tmp_path / "bundled"
    _make_folder(builtin_root, "shared", "builtin_server", ["t"])
    _make_folder(bundled_root, "shared", "bundled_server", ["t"])
    results = discover_tool_folders([
        (builtin_root, SkillSource.BUILTIN),
        (bundled_root, SkillSource.BUNDLED),
    ])
    # Same folder name "shared" — BUNDLED (higher value) wins.
    assert len(results) == 1
    assert results[0].server == "bundled_server"
    assert results[0].source == SkillSource.BUNDLED
