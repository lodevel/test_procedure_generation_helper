#!/usr/bin/env python3
"""Generic stdio MCP server — serves ONE tool folder's ``tools.py`` over JSON-RPC.

A *tool folder* is a directory holding ``tools.py`` (the executable tools) and a
co-located ``tools.json`` (the declared advert the host trusts WITHOUT importing
the code). ``tools.py`` exposes a module-level ``SERVER_NAME`` and ``TOOLS`` — a
list of ``{"name", "description", "inputSchema", "handler"}`` descriptors, each
``handler(arguments: dict) -> str``. This server loads that folder (``--tools-dir``)
and serves its tools; it holds ZERO per-tool knowledge, so the same script backs
every skill-owned and common tool folder.

MCP stdio transport = newline-delimited JSON-RPC 2.0 (one object per line). The
protocol loop is the verified ``_pdf_tool_mcp`` / ``_project_tools_mcp`` skeleton:
``initialize`` echoes the client ``protocolVersion``; notifications (no ``id``)
get no response; ``tools/list``; ``tools/call`` returns ``{"content":[{"type":
"text",...}]}``; ``ping``. A handler that returns a plain string is wrapped as a
text result; a handler owns its OWN error→text mapping and must not raise for
expected/validation failures (only truly-unexpected exceptions hit the -32603
catch-all, so one bad request never kills the loop).

Like the sibling servers it bootstraps ``sys.path`` from its own location before
loading ``tools.py`` — so a tool folder's ``tools.py`` can ``import
workflow_editor.authoring.*`` regardless of where the
folder physically lives or the launch cwd.

DRIFT GUARD (fail-closed): the served ``TOOLS`` names and ``SERVER_NAME`` MUST
match the declared ``tools.json`` (which is what the host's per-request tool-gate
universe is built from). A mismatch exits non-zero rather than serving tools the
host can't gate.
"""
import argparse
import importlib.util
import json
import os
import sys


# --- bootstrap import path from THIS file -------------------------------------
# <repo>/external/test_procedure_generation_helper/workflow_editor/authoring/
# Two dirs up from authoring/ lands on the package root holding workflow_editor/,
# so a loaded tools.py can `import workflow_editor.authoring.*` from any cwd.
_AUTHORING_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(os.path.dirname(_AUTHORING_DIR))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


def _die(message):
    sys.stderr.write("_skill_tools_mcp: " + message + "\n")
    sys.exit(2)


def _load_tool_folder(tools_dir):
    """Load ``<tools_dir>/tools.py`` and reconcile it against ``tools.json``.

    Returns ``(server_name, advert, dispatch)`` where ``advert`` is the
    tools/list payload (name/description/inputSchema, no handler) and
    ``dispatch`` maps tool name -> handler. Exits non-zero (fail-closed) on any
    structural problem — never serves a tool the declared manifest doesn't cover.
    """
    manifest_path = os.path.join(tools_dir, "tools.json")
    tools_py = os.path.join(tools_dir, "tools.py")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        declared_server = manifest["server"]
        declared_tools = set(manifest["tools"])
    except Exception as exc:  # noqa: BLE001
        _die(f"cannot read {manifest_path}: {exc}")

    try:
        spec = importlib.util.spec_from_file_location(
            f"skilltools_{declared_server}", tools_py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        _die(f"cannot import {tools_py}: {exc}")

    server_name = getattr(mod, "SERVER_NAME", None) or declared_server
    tools = list(getattr(mod, "TOOLS", []))
    names = {t["name"] for t in tools}

    # Drift guard: the host built its tool-gate universe from tools.json; if the
    # code serves a different server/tool set those tools can't be gated.
    if server_name != declared_server:
        _die(f"SERVER_NAME {server_name!r} != tools.json server {declared_server!r}")
    if names != declared_tools:
        _die(f"TOOLS {sorted(names)} != tools.json tools {sorted(declared_tools)}")

    advert = [
        {k: t[k] for k in ("name", "description", "inputSchema")} for t in tools
    ]
    dispatch = {t["name"]: t["handler"] for t in tools}
    return server_name, advert, dispatch


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generic tool-folder MCP server")
    parser.add_argument("--tools-dir", required=True,
                        help="folder holding tools.py + tools.json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    server_name, tools, dispatch = _load_tool_folder(args.tools_dir)
    server_info = {"name": server_name, "version": "1.0.0"}

    import _mcp_serve  # sibling module (authoring/ is sys.path[0] for the
    # launched script); avoids triggering the heavy authoring/__init__ chain.
    _mcp_serve.serve(server_info, tools, dispatch)


if __name__ == "__main__":
    main()
