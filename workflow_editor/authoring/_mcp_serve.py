"""Shared stdio MCP JSON-RPC loop for the authoring tool servers.

The transport — newline-delimited JSON-RPC 2.0 (one object per line),
``initialize`` echoing the client ``protocolVersion``, id-less notifications
dropped, ``tools/list``, ``tools/call``, ``ping``, ``-32601``/``-32602`` errors,
and a ``-32603`` catch-all so one bad request never kills the loop — is IDENTICAL
across the generic tool-folder server and the infra servers (``pdf``/``project``).
Only the tool set, the server name, and how a tool is dispatched differ, so each
server builds its advert + a ``dispatch`` map and calls :func:`serve`.

This is plain transport DRY (the loop was copy-pasted); it does NOT merge the
servers — provenance, parameterisation and gating keep them separate modules.
"""
import json
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def text_result(text):
    """Wrap a plain string as an MCP text content result."""
    return {"content": [{"type": "text", "text": text}]}


def serve(server_info, tools, dispatch):
    """Run the stdio JSON-RPC loop until stdin closes.

    ``server_info`` → ``initialize``'s ``serverInfo``; ``tools`` → the
    ``tools/list`` advert (name/description/inputSchema, no handler); ``dispatch``
    → ``{tool name: handler(arguments) -> str | dict}``. A handler returning a
    plain ``str`` is wrapped via :func:`text_result`; one returning a ready MCP
    content dict/list is passed through. A handler must NOT raise for an expected
    failure (return text instead) — only unexpected exceptions hit the -32603
    catch-all.
    """
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
                    "protocolVersion", "2024-11-05")
                send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": client_ver,
                        "capabilities": {"tools": {}},
                        "serverInfo": server_info,
                    },
                })
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
            elif method == "tools/call":
                params = req.get("params") or {}
                name = params.get("name")
                handler = dispatch.get(name)
                if handler is None:
                    send({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32602, "message": f"unknown tool: {name}"},
                    })
                    continue
                result = handler(params.get("arguments") or {})
                if isinstance(result, str):
                    result = text_result(result)
                send({"jsonrpc": "2.0", "id": rid, "result": result})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                })
        except Exception as exc:  # noqa: BLE001 — never let one request kill the loop
            send({
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            })
