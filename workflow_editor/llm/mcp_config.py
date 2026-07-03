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
    rules_dir_win: str,
) -> dict:
    """Return the ``mcp`` entry for the ``pdf_tools`` local server.

    ``command[0]`` is the python translated to its ``/mnt/c`` WSL path (OpenCode
    runs in WSL); the script, ``--documents-dir`` (datasheets) and ``--rules-dir``
    (procedure grammar) values stay as Windows paths (argv for the Windows
    python). Merge the returned dict into the master opencode.json's ``mcp`` object.
    """
    return {
        "pdf_tools": {
            "type": "local",
            "command": [
                win_to_wsl_path(venv_python_win),
                mcp_script_win,
                "--documents-dir",
                documents_dir_win,
                "--rules-dir",
                rules_dir_win,
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


def build_run_skill_mcp_block(
    venv_python_win: str,
    mcp_script_win: str,
    server_port_file_win: str,
    skill_roots_win: list,
    secret: str,
    universe_file_win: str = "",
    max_depth: int = 3,
) -> dict:
    """Return the ``mcp`` entry for the ``run_skill`` infra server (skill-invokes-skill).

    Mirrors :func:`build_project_tools_mcp_block` but carries the extra host state
    run_skill needs: ``--server-port-file`` (the launch pid-file — the server reads
    its own port from it at call time, because the port is OS-assigned AFTER this
    config is written), one ``--skill-root`` per root (ASCENDING precedence, to
    resolve a child ``skill_id``), ``--max-depth``, and the HMAC secret via the block
    ``environment`` (OpenCode delivers it as ``RUN_SKILL_SECRET`` — verified honored).
    A blank secret makes every call fail-closed. Merge into the master opencode.json's
    ``mcp`` object.
    """
    cmd = [
        win_to_wsl_path(venv_python_win),   # OpenCode runs in WSL
        mcp_script_win,                     # Windows argv for the Windows python
        "--server-port-file", server_port_file_win,
        "--max-depth", str(max_depth),
    ]
    if universe_file_win:
        cmd += ["--universe-file", universe_file_win]
    for root in skill_roots_win:
        cmd += ["--skill-root", root]
    return {
        "run_skill": {
            "type": "local",
            "command": cmd,
            "enabled": True,
            "environment": {"RUN_SKILL_SECRET": secret},
        }
    }


def build_skill_tools_mcp_block(
    server_name: str,
    venv_python_win: str,
    mcp_script_win: str,
    tools_dir_win: str,
) -> dict:
    """Return the ``mcp`` entry for ONE tool folder, served by the generic
    ``_skill_tools_mcp.py`` (``--tools-dir`` the folder).

    Replaces the per-tool block builders for skill-owned + common tools: one
    generic script backs every folder, varying only by ``--tools-dir``. Mirrors
    :func:`build_project_tools_mcp_block`: ``command[0]`` is the python translated
    to its ``/mnt/c`` WSL path (OpenCode runs in WSL); the script and the
    ``--tools-dir`` value stay Windows paths (argv for the Windows python).

    ``server_name`` is the block key — and is ALSO the per-request override
    namespace (OpenCode names the tool ``<server_name>_<tool>``) and the gate
    universe key, so block/override/universe can never drift. Merge the returned
    dict into the master opencode.json's ``mcp`` object.
    """
    return {
        server_name: {
            "type": "local",
            "command": [
                win_to_wsl_path(venv_python_win),
                mcp_script_win,
                "--tools-dir",
                tools_dir_win,
            ],
            "enabled": True,
        }
    }


def skill_tool_overrides(active_servers, universe) -> dict:
    """Per-request on/off for EVERY skill-owned tool in ``universe``.

    OpenCode's tool override is ADDITIVE — a tool NOT listed keeps its registered
    ``enabled`` default — so to scope tools to the active skill we must emit an
    EXPLICIT bool for every tool of every registered skill-owned server: ``True``
    for a server the active skill declared, ``False`` for all others (this is what
    stops one skill from seeing another skill's tools).

    ``universe`` is ``{server: [tool names]}`` (see
    :func:`workflow_editor.authoring.tool_folders.build_skill_tools_universe`);
    ``active_servers`` is the active skill's declared ``mcp_tools`` list. Keys are
    ``f"{server}_{tool}"`` to match OpenCode's ``<server>_<tool>`` namespacing.
    """
    active = set(active_servers or ())
    return {
        f"{server}_{tool}": (server in active)
        for server, tools in (universe or {}).items()
        for tool in tools
    }
