"""Fixture tool folder for test_skill_tools_mcp.py — the generic server test.

SERVER_NAME + TOOLS must stay in sync with tools.json beside this file.
"""
SERVER_NAME = "fixture_tools"


def _echo_handler(args):
    return "echo:" + str(args.get("x"))


TOOLS = [
    {
        "name": "echo",
        "description": "d",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        },
        "handler": _echo_handler,
    }
]
