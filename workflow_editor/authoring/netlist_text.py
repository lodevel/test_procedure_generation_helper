"""Format an ODB++ board (the :func:`odb_inspect.load_board` dict) as compact,
LLM-readable netlist text.

Pure transform over the board mapping
``{components: [{refdes, side, pins: [{name, net}]}],
   nets: [{net, nodes: [{refdes, pin}]}], error: str}`` — no I/O, so it's
testable with synthetic dicts. The artifacts context source feeds it the board
loaded elsewhere.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# Integrated-circuit reference designators (U1, IC3, ...). The prefix must be
# immediately followed by a digit so connectors like "USB1" don't match. This is
# the near-universal IPC refdes convention — NOT an EDA-specific field name — so
# only ICs carry part-number properties in the component-ids block (passives are
# skipped, which is the bulk of the property tokens).
_IC_REFDES_RE = re.compile(r"(?i)^(?:U|IC)\d")


def _is_ic_refdes(refdes: str) -> bool:
    return bool(_IC_REFDES_RE.match(refdes or ""))


def _component_line(comp: Mapping[str, Any]) -> str:
    """Connectivity only (refdes + pins -> nets). Part-number properties live in
    :func:`format_component_ids`, a separate context item, so a skill that only
    needs wiring isn't charged the property tokens."""
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


def format_component_ids(board: Mapping[str, Any]) -> str:
    """Render part-number / identity properties for the ICs (U*/IC* refdes) — the
    data a skill needs to identify power ICs — as a block SEPARATE from the
    connectivity netlist, so a skill that only needs wiring isn't charged for it.

    Passives are skipped by refdes convention (the bulk of the property tokens);
    empty values are dropped. Returns ``""`` when no IC properties are present,
    so the artifact simply contributes nothing. (Smarter, board-agnostic
    column projection is the agentic MCP mode's job — see the project tasks.)
    """
    components = board.get("components", []) or []
    lines = []
    for c in components:
        if not isinstance(c, Mapping):
            continue
        refdes = c.get("refdes") or "?"
        if not _is_ic_refdes(refdes):
            continue
        props = c.get("properties") or {}
        if not isinstance(props, Mapping):
            continue
        prop_strs = [f"{k}={v!r}" for k, v in props.items() if str(v).strip()]
        if prop_strs:
            lines.append(f"- {refdes}: {', '.join(prop_strs)}")
    if not lines:
        return ""
    return "Component part numbers (ICs):\n" + "\n".join(lines)
