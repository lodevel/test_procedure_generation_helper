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

Concurrency: ``tools/call`` runs on a DAEMON WORKER THREAD so the read loop keeps
servicing new requests while a handler is still running. This is REQUIRED for
``run_skill``: a handler that spawns a child session blocks on that child's reply,
and the child may itself issue another ``tools/call`` to THIS same server — a
sequential loop would deadlock (the second call is never read). JSON-RPC ids
correlate the (now possibly out-of-order) responses; ``send`` holds a lock so
whole-line writes never interleave. The sibling servers' handlers are quick and
side-effect-light, so concurrent dispatch is safe for them too.
"""
import json
import sys
import threading

# Serialize whole-line stdout writes across worker threads.
_send_lock = threading.Lock()


def send(msg):
    line = json.dumps(msg) + "\n"
    with _send_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def text_result(text):
    """Wrap a plain string as an MCP text content result."""
    return {"content": [{"type": "text", "text": text}]}


def _dispatch_call(rid, params, dispatch):
    """Run ONE ``tools/call`` (on a worker thread) and send its response."""
    name = params.get("name")
    handler = dispatch.get(name)
    if handler is None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32602, "message": f"unknown tool: {name}"}})
        return
    try:
        result = handler(params.get("arguments") or {})
        if isinstance(result, str):
            result = text_result(result)
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    except Exception as exc:  # noqa: BLE001 — never let one request kill the server
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32603, "message": f"internal error: {exc}"}})


def serve(server_info, tools, dispatch):
    """Run the stdio JSON-RPC loop until stdin closes.

    ``server_info`` → ``initialize``'s ``serverInfo``; ``tools`` → the
    ``tools/list`` advert (name/description/inputSchema, no handler); ``dispatch``
    → ``{tool name: handler(arguments) -> str | dict}``. A handler returning a
    plain ``str`` is wrapped via :func:`text_result`; one returning a ready MCP
    content dict/list is passed through. A handler must NOT raise for an expected
    failure (return text instead) — only unexpected exceptions hit the -32603
    catch-all. ``tools/call`` is dispatched on a daemon thread (see module docs).
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
                # Worker thread so the loop keeps reading while this runs (a
                # run_skill handler blocks on a child that may re-enter here).
                threading.Thread(
                    target=_dispatch_call,
                    args=(rid, req.get("params") or {}, dispatch),
                    daemon=True,
                ).start()
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
