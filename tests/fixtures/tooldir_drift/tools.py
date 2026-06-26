"""Drift-guard fixture: tools.py has "echo" but tools.json lists "not_echo".
The generic server's drift guard must catch this and exit non-zero.
"""
SERVER_NAME = "fixture_tools"


def _echo_handler(args):
    return "echo:" + str(args.get("x"))


TOOLS = [
    {
        "name": "echo",
        "description": "d",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _echo_handler,
    }
]
