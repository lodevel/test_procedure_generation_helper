"""Pure helpers for the ``pdf_tools`` OpenCode MCP server block.

No I/O, no Qt — just the path translation and the config-dict shape, so the
wiring (settings_dialog → master opencode.json) is fully unit-testable.

The MCP server is a Windows-venv Python script, but OpenCode runs in WSL and
launches ``local`` servers via ``command``. So ``command[0]`` must be the
``/mnt/c/...python.exe`` interop path, while the script and its
``--documents-dir`` value stay as Windows ``C:\\...`` paths (they are argv that
the *Windows* python interprets).
"""
from __future__ import annotations

import re

# C:\foo  or  C:/foo  → capture drive letter + the rest.
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def win_to_wsl_path(win_path: str) -> str:
    """Convert a Windows path to its ``/mnt/<drive>`` WSL form.

    ``C:\\X\\Y`` → ``/mnt/c/X/Y`` (drive lowercased, backslashes → forward
    slashes). Already-POSIX / ``/mnt/`` paths are returned unchanged (idempotent).
    UNC paths (``\\\\server\\share``) are returned unchanged — out of scope.
    """
    if not win_path:
        return win_path
    # Already POSIX (absolute or /mnt/...) — leave it alone.
    if win_path.startswith("/"):
        return win_path
    # UNC — out of scope, pass through.
    if win_path.startswith("\\\\") or win_path.startswith("//"):
        return win_path
    m = _WIN_DRIVE_RE.match(win_path)
    if not m:
        return win_path
    drive, rest = m.group(1).lower(), m.group(2)
    rest = rest.replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def build_pdf_tools_mcp_block(
    venv_python_win: str,
    mcp_script_win: str,
    documents_dir_win: str,
) -> dict:
    """Return the ``mcp`` entry for the ``pdf_tools`` local server.

    ``command[0]`` is the python translated to its ``/mnt/c`` WSL path (OpenCode
    runs in WSL); the script and ``--documents-dir`` value stay as Windows paths
    (argv for the Windows python). Merge the returned dict into the master
    opencode.json's ``mcp`` object.
    """
    return {
        "pdf_tools": {
            "type": "local",
            "command": [
                win_to_wsl_path(venv_python_win),
                mcp_script_win,
                "--documents-dir",
                documents_dir_win,
            ],
            "enabled": True,
        }
    }


def build_project_tools_mcp_block(
    venv_python_win: str,
    mcp_script_win: str,
    odb_tgz_win: str,
) -> dict:
    """Return the ``mcp`` entry for the ``project_tools`` local server.

    Mirrors :func:`build_pdf_tools_mcp_block`: ``command[0]`` is the python
    translated to its ``/mnt/c`` WSL path (OpenCode runs in WSL); the script and
    ``--odb-tgz`` value stay as Windows paths (argv for the Windows python). The
    server exposes the project-data tools (netlist/BOM/components) the LLM PULLs.
    Merge the returned dict into the master opencode.json's ``mcp`` object.
    """
    return {
        "project_tools": {
            "type": "local",
            "command": [
                win_to_wsl_path(venv_python_win),
                mcp_script_win,
                "--odb-tgz",
                odb_tgz_win,
            ],
            "enabled": True,
        }
    }


def build_dcdc_tools_mcp_block(
    venv_python_win: str,
    mcp_script_win: str,
) -> dict:
    """Return the ``mcp`` entry for the ``dcdc_tools`` local server.

    Mirrors :func:`build_project_tools_mcp_block`: ``command[0]`` is the python
    translated to its ``/mnt/c`` WSL path (OpenCode runs in WSL); the script
    stays a Windows path (argv for the Windows python). The server exposes the
    single deterministic ``generate_dcdc_test`` tool the LLM CALLS with the
    params it extracted, so the procedure text is generated, not free-formed.
    Unlike project_tools/pdf_tools, it takes NO per-project argv (the generator
    is project-independent — it consumes only the params in each call). Merge the
    returned dict into the master opencode.json's ``mcp`` object.
    """
    return {
        "dcdc_tools": {
            "type": "local",
            "command": [
                win_to_wsl_path(venv_python_win),
                mcp_script_win,
            ],
            "enabled": True,
        }
    }
