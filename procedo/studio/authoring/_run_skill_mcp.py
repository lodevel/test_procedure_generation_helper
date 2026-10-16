#!/usr/bin/env python3
"""Dedicated stdio MCP server exposing ONE tool: ``run_skill`` (skill-invokes-skill).

Peer of ``_project_tools_mcp`` / ``_pdf_tool_mcp``: host INFRASTRUCTURE (a reserved
server name, gated by a single per-request bool), NOT a user tool folder — it needs
launch-time state a generic ``--tools-dir`` server can't carry: the skill roots (to
resolve a child ``skill_id``), the launch pid-file (to find the server's own port at
call time), the recursion ceiling, and the HMAC secret (via ``RUN_SKILL_SECRET`` env,
which OpenCode delivers from the block's ``environment`` — verified honored).

When the model calls ``run_skill(skill_id, prompt, chain_token)``: verify the token
(HMAC), apply the recursion guard (depth + cycle), then spawn a LOCKED-DOWN child
OpenCode session for the skill on the SAME server and return its prose. All logic is
in :mod:`rs_core`; this file is thin argv + serve wiring.

Bootstraps ``sys.path`` from its own location so it imports ``_mcp_serve`` / ``rs_core``
as bare siblings regardless of launch cwd (same as the sibling servers).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rs_core        # noqa: E402  (bare sibling — see module docstring)
import _mcp_serve     # noqa: E402


def _handle_run_skill(arguments, cfg):
    """Verify -> guard -> spawn a child skill session. Returns text; never raises
    for an expected failure (a refusal / spawn error is normal output)."""
    skill_id = (arguments or {}).get("skill_id", "")
    prompt = (arguments or {}).get("prompt", "")
    token = (arguments or {}).get("chain_token", "")

    if not skill_id:
        return "run_skill REFUSED: skill_id is required."
    payload = rs_core.verify(token, cfg["secret"])
    if payload is None:
        return ("run_skill REFUSED: missing or invalid chain_token (recursion is "
                "only available to a skill that declares `mcp_tools: [run_skill]`).")
    status, res = rs_core.guard(payload, skill_id, cfg["max_depth"])
    if status == "refused":
        return f"run_skill REFUSED: {res}"
    child_payload = res
    child_token = rs_core.sign(child_payload, cfg["secret"])

    try:
        server_url = rs_core.read_server_url(cfg["port_file"])
    except Exception as exc:  # noqa: BLE001
        return f"run_skill ERROR: cannot locate the OpenCode server ({exc})."
    try:
        text, child_sid = rs_core.run_child_skill(
            server_url, cfg["roots"], skill_id, prompt,
            child_payload, child_token, cfg["max_depth"],
            universe=cfg["universe"], model=cfg["model"])
    except ValueError as exc:  # skill not found / empty body
        return f"run_skill REFUSED: {exc}"
    except Exception as exc:  # noqa: BLE001 — surface as text, keep the loop alive
        return f"run_skill ERROR spawning child '{skill_id}': {exc}"
    return (f"[child skill '{skill_id}' ran at depth {child_payload['depth']}, "
            f"session {child_sid}]\n{text}")


_TOOL = {
    "name": "run_skill",
    "description": ("Delegate to another authoring skill: run `skill_id` as a child "
                    "LLM session with `prompt` and return its result. You MUST pass "
                    "the chain_token given to you by [HOST]."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "the skill folder name to run"},
            "prompt": {"type": "string", "description": "what to ask that skill"},
            "chain_token": {"type": "string", "description": "the exact token from [HOST]"},
        },
        "required": ["skill_id", "prompt", "chain_token"],
    },
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="run_skill MCP server")
    ap.add_argument("--server-port-file", required=True,
                    help="launch pid-file holding the OpenCode server port")
    ap.add_argument("--skill-root", action="append", default=[],
                    help="a skill root (repeatable, ASCENDING precedence)")
    ap.add_argument("--universe-file", default="",
                    help="JSON {server:[tools]} of skill-owned tools, to scope the child")
    ap.add_argument("--max-depth", type=int, default=rs_core.DEFAULT_MAX_DEPTH)
    ap.add_argument("--model", default="",
                    help="optional provider/model for the child (blank = auto-pick)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = {
        "port_file": a.server_port_file,
        "roots": list(a.skill_root),
        "universe": rs_core.load_universe(a.universe_file),
        "max_depth": a.max_depth,
        "model": a.model,
        "secret": os.environ.get("RUN_SKILL_SECRET", "").encode(),
    }
    _mcp_serve.serve(
        {"name": "run_skill", "version": "1.0.0"},
        [_TOOL],
        {"run_skill": lambda args: _handle_run_skill(args, cfg)},
    )


if __name__ == "__main__":
    main()
