#!/usr/bin/env python3
"""Dependency-free stdio MCP server exposing one tool: ``read_pdf``.

MCP stdio transport = newline-delimited JSON-RPC 2.0 (one JSON object per line).
This server is launched by OpenCode as a ``local`` MCP server (see
``workflow_editor/llm/mcp_config.build_pdf_tools_mcp_block``). It reads a PDF
from a URL or a local path scoped to a ``--documents-dir`` folder and returns
its extracted text, reusing :mod:`workflow_editor.authoring.pdf_text`.

Protocol handling mirrors the verified probe skeleton: ``initialize`` echoes the
client's ``protocolVersion``; notifications (no ``id``) get no response;
``tools/list``; ``tools/call`` returns ``{"content":[{"type":"text",...}]}``;
``ping``.

Runs as a standalone script (launched by absolute path from an arbitrary cwd),
so it bootstraps its own import path before importing the package.
"""
import json
import os
import sys


# --- path-independent import bootstrap -------------------------------------
# This file lives at <root>/workflow_editor/authoring/_pdf_tool_mcp.py. Inserting
# <root> (two dirs up from this file) onto sys.path lets us import the package as
# `workflow_editor.authoring.pdf_text` no matter the launch cwd or how the
# absolute path was spelled (WSL or Windows interop).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from workflow_editor.authoring.pdf_text import (  # noqa: E402
    extract_pdf_text,
    fetch_and_extract,
)


SERVER_INFO = {"name": "pdf-tools", "version": "1.0.0"}

TOOLS = [
    {
        "name": "read_pdf",
        "description": (
            "Fetch and read a PDF, returning its extracted text. USE THIS FOR "
            "ANY PDF, datasheets especially: webfetch and websearch CANNOT read "
            "PDF content (they only handle HTML pages), so whenever you have a "
            "PDF URL or a PDF in the documents folder, call read_pdf — never "
            "webfetch on a .pdf. 'source' is an http(s) URL to a PDF, or the "
            "name of a PDF file in the project's documents folder (local lookups "
            "are sandboxed to that folder). Returns the text layer (pin tables, "
            "body text); scanned/image-only PDFs have no text layer and cannot "
            "be read this way."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "An http(s) URL to a PDF, or a filename/relative "
                    "path inside the documents folder.",
                }
            },
            "required": ["source"],
        },
    },
    {
        "name": "list_documents",
        "description": (
            "List the files in the project's documents folder (datasheets, notes, "
            "etc.) so you can see what is available to read. No arguments. Pair with "
            "read_document to read one. Local only, no network — always available."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_document",
        "description": (
            "Read a LOCAL document PDF from the project's documents folder and "
            "return its text layer. 'name' is a filename/relative path inside the "
            "documents folder (use list_documents to discover names). Sandboxed to "
            "that folder; no network, so it works WITHOUT web access. For a PDF at "
            "an http(s) URL, use read_pdf instead (that one needs web)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename or relative path inside the documents folder.",
                }
            },
            "required": ["name"],
        },
    },
]

_UNREADABLE_MSG = (
    "Could not read the PDF (no text layer, unreachable, or not a PDF)."
)


def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _text_result(text):
    return {"content": [{"type": "text", "text": text}]}


def _resolve_local(source: str, documents_dir: str):
    """Resolve ``source`` to a real path INSIDE ``documents_dir``, or ``None``.

    Accepts a bare filename, a relative path, or an absolute path — as long as
    the resolved real path is contained within the real documents dir. Anything
    that escapes (``..`` traversal, an absolute path elsewhere, a symlink out)
    returns ``None`` so the caller can deny access.
    """
    base = os.path.realpath(documents_dir)
    if os.path.isabs(source):
        candidate = source
    else:
        candidate = os.path.join(base, source)
    real = os.path.realpath(candidate)
    # Containment check: real must be base itself or a descendant of base.
    if real == base:
        return None  # the dir itself is not a file
    base_prefix = base + os.sep
    if not real.startswith(base_prefix):
        return None
    return real


def _handle_read_pdf(arguments, documents_dir):
    source = (arguments or {}).get("source")
    if not isinstance(source, str) or not source.strip():
        return _text_result("read_pdf requires a non-empty 'source' string.")
    source = source.strip()

    if source.startswith("http://") or source.startswith("https://"):
        text = fetch_and_extract(source)
        return _text_result(text if text is not None else _UNREADABLE_MSG)

    resolved = _resolve_local(source, documents_dir)
    if resolved is None:
        return _text_result(
            "Access denied: that path is outside the documents folder."
        )
    text = extract_pdf_text(resolved)
    return _text_result(text if text is not None else _UNREADABLE_MSG)


def _handle_list_documents(documents_dir):
    """List the files under the (sandboxed) documents folder, relative paths."""
    base = os.path.realpath(documents_dir)
    if not os.path.isdir(base):
        return _text_result("(no documents folder for this project)")
    names = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            names.append(os.path.relpath(os.path.join(root, fn), base))
            if len(names) >= 500:
                break
        if len(names) >= 500:
            break
    if not names:
        return _text_result("(documents folder is empty)")
    names.sort()
    return _text_result(
        "Documents available (read one with read_document):\n"
        + "\n".join("- " + n for n in names)
    )


def _handle_read_document(arguments, documents_dir):
    """Read a LOCAL document from the sandboxed folder — no network branch."""
    name = (arguments or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return _text_result("read_document requires a non-empty 'name' string.")
    resolved = _resolve_local(name.strip(), documents_dir)
    if resolved is None:
        return _text_result(
            "Access denied: that path is outside the documents folder."
        )
    text = extract_pdf_text(resolved)
    return _text_result(text if text is not None else _UNREADABLE_MSG)


def _parse_documents_dir(argv):
    """Tiny manual argv parse for ``--documents-dir <path>`` (default cwd)."""
    docs = os.getcwd()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--documents-dir":
            if i + 1 < len(argv):
                docs = argv[i + 1]
                i += 2
                continue
            i += 1
        elif arg.startswith("--documents-dir="):
            docs = arg.split("=", 1)[1]
            i += 1
        else:
            i += 1
    return docs


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    documents_dir = _parse_documents_dir(argv)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        method = req.get("method")
        # Notifications (no id) -> no response.
        if rid is None:
            continue
        try:
            if method == "initialize":
                client_ver = (req.get("params") or {}).get(
                    "protocolVersion", "2024-11-05"
                )
                _send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": client_ver,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                })
            elif method == "tools/list":
                _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = req.get("params") or {}
                name = params.get("name")
                args = params.get("arguments")
                if name == "read_pdf":
                    result = _handle_read_pdf(args, documents_dir)
                elif name == "read_document":
                    result = _handle_read_document(args, documents_dir)
                elif name == "list_documents":
                    result = _handle_list_documents(documents_dir)
                else:
                    _send({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": -32602,
                            "message": f"unknown tool: {name}",
                        },
                    })
                    continue
                _send({"jsonrpc": "2.0", "id": rid, "result": result})
            elif method == "ping":
                _send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                _send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {
                        "code": -32601,
                        "message": f"method not found: {method}",
                    },
                })
        except Exception as exc:  # noqa: BLE001 — never let one request kill the loop
            _send({
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            })


if __name__ == "__main__":
    main()
