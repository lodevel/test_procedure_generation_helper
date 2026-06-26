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
        "name": "save_pdf",
        "description": (
            "Fetch a datasheet PDF from a WEB URL, SAVE it into the project's "
            "documents folder for reuse, and return its text. Use this (when "
            "available) instead of read_pdf for a datasheet you want to KEEP: a "
            "future test then reads it locally with list_documents / read_document, "
            "no re-download. 'source' is an http(s) PDF URL; 'save_as' is the name "
            "to store it under — use the part's EXACT MPN."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "An http(s) URL to the datasheet PDF.",
                },
                "save_as": {
                    "type": "string",
                    "description": "Filename to cache it under (the part's exact "
                    "MPN). Stored as a sandboxed <name>.pdf in the documents folder.",
                },
            },
            "required": ["source", "save_as"],
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
    {
        "name": "list_rules",
        "description": (
            "List the procedure GRAMMAR / rule files available for this project so "
            "you can see what's there. No arguments. Pair with read_rule. Local "
            "only, no network — always available."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_rule",
        "description": (
            "Read a GRAMMAR / rule file (returns its text) so you can write VALID "
            "procedure text. 'name' is a filename from list_rules. Sandboxed to the "
            "rules folder; no network — always available. Do NOT re-read a rule that "
            "is already present in your context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename of a rule file (see list_rules).",
                }
            },
            "required": ["name"],
        },
    },
]

_UNREADABLE_MSG = (
    "Could not read the PDF (no text layer, unreachable, or not a PDF)."
)


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


def _safe_pdf_name(save_as):
    """Sandbox a model-supplied cache name to a bare ``<name>.pdf`` filename.

    basename only (strips any path -> no traversal), a safe charset, and a forced
    ``.pdf`` suffix, so the cached file can only land directly in the documents
    folder. Returns None when nothing usable remains (then we do not cache)."""
    base = os.path.basename(str(save_as or "").strip())
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in base).strip("._")
    return (safe + ".pdf") if safe else None


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


def _handle_save_pdf(arguments, documents_dir):
    """Fetch a web PDF, CACHE it into the documents folder, and return its text.

    The cached datasheet then survives for future tests (read it locally via
    list_documents / read_document, no web). Gated by the 'Save datasheets'
    per-chat toggle; only http(s) sources are cached, under a sandboxed name."""
    arguments = arguments or {}
    source = arguments.get("source")
    if not isinstance(source, str) or not source.strip():
        return _text_result("save_pdf requires a non-empty 'source' URL.")
    source = source.strip()
    if not (source.startswith("http://") or source.startswith("https://")):
        return _text_result(
            "save_pdf only fetches http(s) URLs; for a local file use read_document."
        )
    save_name = _safe_pdf_name(arguments.get("save_as"))
    if not save_name:
        return _text_result(
            "save_pdf requires a 'save_as' name (use the part's exact MPN)."
        )
    save_to = os.path.join(documents_dir, save_name)
    text = fetch_and_extract(source, save_to=save_to)
    if text is None:
        return _text_result(_UNREADABLE_MSG)
    if os.path.exists(save_to):
        text += (
            f"\n\n(Saved to the project documents as {save_name} — future tests "
            f"can read it locally via list_documents / read_document, no web.)"
        )
    return _text_result(text)


def _list_dir(directory, missing_msg, empty_msg, header):
    """List files under a sandboxed folder (relative paths, capped). Shared by
    list_documents and list_rules so they can't drift."""
    base = os.path.realpath(directory)
    if not os.path.isdir(base):
        return _text_result(missing_msg)
    names = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            names.append(os.path.relpath(os.path.join(root, fn), base))
            if len(names) >= 500:
                break
        if len(names) >= 500:
            break
    if not names:
        return _text_result(empty_msg)
    names.sort()
    return _text_result(header + "\n" + "\n".join("- " + n for n in names))


def _handle_list_documents(documents_dir):
    return _list_dir(
        documents_dir, "(no documents folder for this project)",
        "(documents folder is empty)",
        "Documents available (read one with read_document):")


def _handle_list_rules(rules_dir):
    return _list_dir(
        rules_dir, "(no rules folder for this project)",
        "(rules folder is empty)",
        "Grammar/rule files available (read one with read_rule):")


def _handle_read_rule(arguments, rules_dir):
    """Read a LOCAL rule/grammar file as TEXT (not PDF), sandboxed to the rules
    folder — no network."""
    name = (arguments or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return _text_result("read_rule requires a non-empty 'name' string.")
    resolved = _resolve_local(name.strip(), rules_dir)
    if resolved is None:
        return _text_result("Access denied: that path is outside the rules folder.")
    try:
        with open(resolved, encoding="utf-8", errors="replace") as fh:
            return _text_result(fh.read())
    except OSError as exc:
        return _text_result(f"Could not read the rule file: {exc}")


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


def _parse_dir_arg(argv, flag, default):
    """Tiny manual argv parse for ``<flag> <path>`` or ``<flag>=<path>``."""
    i = 0
    while i < len(argv):
        if argv[i] == flag and i + 1 < len(argv):
            return argv[i + 1]
        if argv[i].startswith(flag + "="):
            return argv[i].split("=", 1)[1]
        i += 1
    return default


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    documents_dir = _parse_dir_arg(argv, "--documents-dir", os.getcwd())
    rules_dir = _parse_dir_arg(argv, "--rules-dir", os.getcwd())

    import _mcp_serve  # sibling module (authoring/ is sys.path[0] for the
    # launched script); avoids triggering the heavy authoring/__init__ chain.
    dispatch = {
        "read_pdf": lambda a: _handle_read_pdf(a, documents_dir),
        "save_pdf": lambda a: _handle_save_pdf(a, documents_dir),
        "read_document": lambda a: _handle_read_document(a, documents_dir),
        "list_documents": lambda a: _handle_list_documents(documents_dir),
        "list_rules": lambda a: _handle_list_rules(rules_dir),
        "read_rule": lambda a: _handle_read_rule(a, rules_dir),
    }
    _mcp_serve.serve(SERVER_INFO, TOOLS, dispatch)


if __name__ == "__main__":
    main()
