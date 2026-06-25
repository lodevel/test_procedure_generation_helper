"""Tests for workflow_editor.llm.mcp_config (pure path/dict helpers)."""
from workflow_editor.llm import mcp_config


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
