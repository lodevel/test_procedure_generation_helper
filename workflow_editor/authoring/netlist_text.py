"""Format an ODB++ board (the :func:`odb_inspect.load_board` dict) as compact,
LLM-readable netlist text.

Pure transform over the board mapping
``{components: [{refdes, side, pins: [{name, net}]}],
   nets: [{net, nodes: [{refdes, pin}]}], error: str}`` — no I/O, so it's
testable with synthetic dicts. The artifacts context source feeds it the board
loaded elsewhere.
"""
from __future__ import annotations

from typing import Any, Mapping


def _component_line(comp: Mapping[str, Any]) -> str:
    refdes = comp.get("refdes", "") or "?"
    side = comp.get("side", "") or ""
    pins = comp.get("pins", []) or ()
    pin_strs = [
        f"{(p.get('name') or '?')}={(p.get('net') or '-')}"
        for p in pins
        if isinstance(p, Mapping)
    ]
    head = f"- {refdes}" + (f" [{side}]" if side else "")
    return f"{head}: {', '.join(pin_strs)}" if pin_strs else head


def _node_str(node: Mapping[str, Any]) -> str:
    # ``pin`` may legitimately be 0/"0", so test for None rather than falsiness.
    pin = node.get("pin")
    return f"{(node.get('refdes') or '?')}.{'?' if pin is None else pin}"


def _net_line(net: Mapping[str, Any]) -> str:
    name = net.get("net", "") or "?"
    nodes = net.get("nodes", []) or ()
    node_strs = [_node_str(n) for n in nodes if isinstance(n, Mapping)]
    return f"- {name}: {', '.join(node_strs)}" if node_strs else f"- {name}"


def format_netlist(board: Mapping[str, Any]) -> str:
    """Render ``board`` as text. Returns an explanatory line (never raises) when
    the board carries an error or is empty."""
    components = board.get("components", []) or []
    nets = board.get("nets", []) or []
    error = board.get("error", "") or ""
    if not components and not nets:
        return f"(no netlist available: {error})" if error else "(no netlist available)"

    sections = [
        f"Components ({len(components)}):",
        *[_component_line(c) for c in components if isinstance(c, Mapping)],
        "",
        f"Nets ({len(nets)}):",
        *[_net_line(n) for n in nets if isinstance(n, Mapping)],
    ]
    return "\n".join(sections)
