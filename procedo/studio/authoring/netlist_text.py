"""Format an ODB++ board (the :func:`odb_inspect.load_board` dict) as compact,
LLM-readable netlist text.

Pure transform over the board mapping
``{components: [{refdes, side, pins: [{name, net}]}],
   nets: [{net, nodes: [{refdes, pin}]}], error: str}`` — no I/O, so it's
testable with synthetic dicts. The artifacts context source feeds it the board
loaded elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Mapping

# Match the generated chat transport budget; reject larger snapshots as a whole.
BOARD_CONTEXT_MAX_BYTES = 1024 * 1024

# Integrated-circuit reference designators (U1, IC3, ...). The prefix must be
# immediately followed by a digit so connectors like "USB1" don't match. This is
# the near-universal IPC refdes convention — NOT an EDA-specific field name — so
# only ICs carry part-number properties in the component-ids block (passives are
# skipped, which is the bulk of the property tokens).
_IC_REFDES_RE = re.compile(r"(?i)^(?:U|IC)\d")


def board_revision(path: str) -> str:
    """Cheap source identity shared by chat instructions and cached tool reads."""
    resolved = os.path.realpath(path)
    stat = os.stat(resolved)
    identity = (
        resolved,
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:20]


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
        f"{(p.get('name') or '?')}={(p.get('net') or '-')}" for p in pins if isinstance(p, Mapping)
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


def _format_part_numbers(board: Mapping[str, Any], include, header: str) -> str:
    """Render part-number / identity properties for the components whose refdes
    satisfies ``include(refdes)``, as a block SEPARATE from the connectivity
    netlist. Empty values are dropped; returns ``""`` when nothing matches.
    Shared by :func:`format_component_ids` (ICs) and
    :func:`format_other_component_ids` (everything else) so they can't drift."""
    components = board.get("components", []) or []
    lines = []
    for c in components:
        if not isinstance(c, Mapping):
            continue
        refdes = c.get("refdes") or "?"
        if not include(refdes):
            continue
        props = c.get("properties") or {}
        if not isinstance(props, Mapping):
            continue
        prop_strs = [f"{k}={v!r}" for k, v in props.items() if str(v).strip()]
        if prop_strs:
            lines.append(f"- {refdes}: {', '.join(prop_strs)}")
    if not lines:
        return ""
    return header + "\n" + "\n".join(lines)


def format_component_ids(board: Mapping[str, Any]) -> str:
    """Part numbers for the ICs (U*/IC* refdes) ONLY — the data a skill needs to
    identify power ICs — separate from the connectivity netlist so a wiring-only
    skill isn't charged for it. NON-IC components live in
    :func:`format_other_component_ids`. Empty values dropped; ``""`` if none.
    (Smarter, board-agnostic column projection is the agentic MCP mode's job.)"""
    return _format_part_numbers(board, _is_ic_refdes, "Component part numbers (ICs — U*, IC*):")


def format_other_component_ids(board: Mapping[str, Any]) -> str:
    """Part numbers for the NON-IC components — everything whose refdes is NOT
    U*/IC* (passives, connectors, relays, modules, ...). A SEPARATE artifact (and
    usually the larger one) so a skill opts into the non-IC set only when it needs
    it. Empty values dropped; ``""`` if none."""
    return _format_part_numbers(
        board, lambda r: not _is_ic_refdes(r), "Component part numbers (non-ICs):"
    )


_PROPERTY_ROLES = {
    "identity": (
        "manufacturerpartnumber",
        "manufacturerpn",
        "mpn",
        "manufacturerreference",
        "mfgpartnumber",
        "mfrpartnumber",
        "mfrpartno",
        "mfrpn",
        "mfgref",
        "vendorpartnumber",
        "vendorreference",
        "partnumber",
        "partno",
        "pn",
        "partname",
        "part",
        "componentname",
        "devicename",
        "name",
    ),
    "type": ("componenttype", "devicetype", "boardkind", "type", "category", "kind"),
    "function": ("componentfunction", "devicefunction", "function"),
    "value": ("componentvalue", "value"),
    "description": ("componentdescription", "description", "comment"),
    "package": ("footprint", "packagename", "package", "landpattern"),
    "test_access": (
        "testpoint",
        "istestpoint",
        "hastestpoint",
        "testpointtype",
        "testpointname",
        "testaccess",
        "istestaccess",
        "hastestaccess",
        "testaccessibility",
        "testaccessside",
        "testaccessmethod",
        "testpad",
        "istestpad",
        "probetype",
        "probeaccess",
        "probeaccessible",
    ),
}


def _property_role(key: str) -> tuple[str, int] | None:
    """Interpret field names only; arbitrary property values are not evidence."""
    parts = re.split(r"[.:/]+", str(key))
    candidates = [re.sub(r"[^a-z0-9]", "", part.casefold()) for part in (str(key), parts[-1])]
    # Namespace tolerance must not turn Supplier.Name into a component identity.
    if any(
        re.sub(r"[^a-z0-9]", "", part.casefold())
        in {"supplier", "distributor", "purchasing", "assembly", "document", "geometry"}
        for part in parts[:-1]
    ):
        return None
    for candidate in candidates:
        for role, names in _PROPERTY_ROLES.items():
            if candidate in names:
                if (
                    role == "identity"
                    and candidate == "name"
                    and (
                        candidates[0] in {"manufacturername", "vendorname"}
                        or any(part.casefold() in {"manufacturer", "vendor"} for part in parts[:-1])
                    )
                ):
                    return None
                return role, names.index(candidate)
    return None


def _selected_properties(components: list) -> tuple[str, int, int]:
    """Project the discovered schema without changing the cached board mapping."""
    fields = {key for comp in components for key in comp["properties"]}
    roles = {key: _property_role(key) for key in fields}
    selected_fields, lines = set(), []
    missing_identity = 0
    for comp in components:
        candidates = {}
        for key, value in comp["properties"].items():
            role = roles[key]
            if role is not None and value is not None and str(value).strip():
                candidates.setdefault(role[0], []).append((role[1], key))
        if "type" in candidates or "function" in candidates:
            candidates.pop("description", None)
        if len(comp["pins"]) != 1:
            candidates.pop("package", None)
        missing_identity += "identity" not in candidates
        selected = set()
        for role, choices in candidates.items():
            if role == "test_access":
                selected.update(key for _, key in choices)
            else:
                selected.add(min(choices, key=lambda item: (item[0], item[1]))[1])
        selected_fields.update(selected)
        values = [
            f"{key}={value!r}" for key, value in comp["properties"].items() if key in selected
        ]
        if values:
            lines.append(f"- {comp['refdes']}: {', '.join(values)}")
    schema = json.dumps(
        {"selected": sorted(selected_fields), "omitted": sorted(fields - selected_fields)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    header = (
        "Property fields: " + schema + "\n"
        "One populated field per identity/type/function/value role; descriptions "
        "are fallback only, packages are for single-pin candidates. Explicit "
        "test-access flags are preserved. Other fields are omitted, not absent. "
        "Components without selected metadata remain in the connectivity."
    )
    text = header + ("\nSelected component properties:\n" + "\n".join(lines) if lines else "")
    return text, len(lines), missing_identity


def format_board_context(board: Mapping[str, Any]) -> str:
    """Full connectivity and identities from ONE board snapshot, without caps.

    Validate before formatting: the older optional formatters intentionally skip
    malformed rows, which must not yield a complete-looking aggregate response.
    Select compact metadata locally from the board schema; the independent
    wizard identity formatters keep their original column semantics.
    """
    if not isinstance(board, Mapping) or board.get("error"):
        raise ValueError(f"Board data unavailable: {board}")
    components, nets = board.get("components"), board.get("nets")
    if not isinstance(components, list) or not isinstance(nets, list):
        raise ValueError("Board connectivity is incomplete (missing components or nets).")
    if not components and not nets:
        raise ValueError("No netlist available for this board.")
    pins = []
    for comp in components:
        if (
            not isinstance(comp, Mapping)
            or not comp.get("refdes")
            or not isinstance(comp.get("pins"), list)
            or not isinstance(comp.get("properties"), Mapping)
        ):
            raise ValueError("Board component connectivity or identities are incomplete.")
        for pin in comp["pins"]:
            if not isinstance(pin, Mapping) or "name" not in pin or "net" not in pin:
                raise ValueError("Board pin connectivity is incomplete.")
        pins.extend(comp["pins"])
    nodes = 0
    for net in nets:
        if (
            not isinstance(net, Mapping)
            or not net.get("net")
            or not isinstance(net.get("nodes"), list)
        ):
            raise ValueError("Board net connectivity is incomplete.")
        for node in net["nodes"]:
            if not isinstance(node, Mapping) or not node.get("refdes") or "pin" not in node:
                raise ValueError("Board net nodes are incomplete.")
        nodes += len(net["nodes"])
    unknown = sum(pin["net"] is None for pin in pins)
    properties, identities, missing_identity = _selected_properties(components)
    counts = (
        f"components={len(components)}, pins={len(pins)}, nets={len(nets)}, "
        f"nodes={nodes}, components_with_properties={identities}, "
        f"pins_with_unknown_net={unknown}, "
        f"components_without_selected_properties={len(components) - identities}, "
        f"components_without_selected_identity={missing_identity}"
    )
    header = (
        f"Board context: {counts}.\n"
        "Check these counts and the final BOARD_CONTEXT_END marker; a missing "
        "marker means truncated data. Do not infer absent connections from a "
        "partial result. Reuse this snapshot for follow-up questions.\n"
        "Properties preserve the board's own field names; missing identities "
        "or unknown nets are unknown, not proof of absence. A single-pin part "
        "alone is not proof of a test point. Shared nets are direct connections; "
        "paths through components are indirect."
    )
    sections = [header, format_netlist(board), properties, f"BOARD_CONTEXT_END: {counts}"]
    return "\n\n".join(section for section in sections if section)
