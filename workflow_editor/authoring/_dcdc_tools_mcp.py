#!/usr/bin/env python3
"""Dependency-free stdio MCP server exposing the deterministic DC-DC generator.

MCP stdio transport = newline-delimited JSON-RPC 2.0 (one JSON object per line).
This server exposes ONE tool, ``generate_dcdc_test``, whose ``inputSchema`` is the
DC-DC bring-up param schema. The LLM's job (per the ``dcdc_bringup`` skill) is to
EXTRACT those params from the netlist/datasheet; this tool turns them into the
canonical ``## Equipment`` / ``## Steps`` / ``## Expected`` procedure text via the
:mod:`workflow_editor.authoring.dcdc_test_generator` pure function. The output is
DETERMINISTIC, removing the variance the benchmark showed in free-formed tests.

Protocol handling mirrors the verified ``_project_tools_mcp`` / ``_pdf_tool_mcp``
skeleton: ``initialize`` echoes the client's ``protocolVersion``; notifications
(no ``id``) get no response; ``tools/list``; ``tools/call`` returns
``{"content":[{"type":"text",...}]}``; ``ping``.

Runs as a standalone script launched by absolute path from an arbitrary cwd, so
it bootstraps ``sys.path`` from its own location before importing the generator —
exactly like the sibling MCP servers resolve their helpers from ``__file__``.

A missing or invalid param surfaces as a readable tool result (the validation
error lists exactly which field is absent) — never an exception that kills the
request loop.
"""
import json
import os
import sys


# --- bootstrap import path from THIS file -------------------------------------
# This file lives at:
#   <repo>/external/test_procedure_generation_helper/workflow_editor/authoring/_dcdc_tools_mcp.py
# Three dirs up from authoring/ lands on the package root
# (<repo>/external/test_procedure_generation_helper), which must be importable so
# `from workflow_editor.authoring import dcdc_test_generator` resolves even when
# the server is launched by absolute path from an unrelated cwd.
_AUTHORING_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(os.path.dirname(_AUTHORING_DIR))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from workflow_editor.authoring import dcdc_test_generator as gen  # noqa: E402


SERVER_INFO = {"name": "dcdc-tools", "version": "1.0.0"}


# --- inputSchema derived from the generator's PARAM_SCHEMA --------------------
# We translate the generator's machine-readable PARAM_SCHEMA (the single source of
# truth for the param set) into a JSON-Schema ``inputSchema`` so the tool advert
# and the generator never drift. The PARAM_SCHEMA's lightweight type strings
# ("float", "str", "bool", "int", "float|null", "str|null", "object") map onto
# JSON-Schema types; ``required:true`` entries become the object's ``required``
# list. Conditional requireds (enable.control_target / power_good.test_point) are
# NOT marked required at the schema level — the generator enforces them — but
# their "meaning" text states the condition, so the model knows to supply them.

_TYPE_MAP = {
    "str": "string",
    "float": "number",
    "int": "integer",
    "bool": "boolean",
    "float|null": "number",
    "str|null": "string",
}


def _field_to_json_schema(spec: dict) -> dict:
    """Translate ONE PARAM_SCHEMA field spec to a JSON-Schema property dict."""
    if spec.get("type") == "object":
        props = {}
        required = []
        for name, sub in spec.get("fields", {}).items():
            props[name] = _field_to_json_schema(sub)
            if sub.get("required"):
                required.append(name)
        obj = {"type": "object", "properties": props}
        if required:
            obj["required"] = required
        if spec.get("meaning"):
            obj["description"] = spec["meaning"]
        return obj
    out = {"type": _TYPE_MAP.get(spec.get("type"), "string")}
    if spec.get("meaning"):
        out["description"] = spec["meaning"]
    if "default" in spec and spec["default"] is not None:
        out["default"] = spec["default"]
    return out


def _build_input_schema() -> dict:
    """Build the generate_dcdc_test inputSchema from gen.PARAM_SCHEMA."""
    props = {}
    required = []
    for name, spec in gen.PARAM_SCHEMA.items():
        props[name] = _field_to_json_schema(spec)
        if spec.get("required"):
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {
        "name": "generate_dcdc_test",
        "description": (
            "Turn the EXTRACTED power-IC bring-up params into the canonical "
            "scope-based power-supply test (## Equipment / ## Steps / ## "
            "Expected), deterministically. Call this INSTEAD of hand-writing the "
            "procedure steps: you supply the structural + numeric params (rail "
            "name, IC refdes/part, nominal Vout, rail test point, PSU set-points "
            "and board entry, whether an enable / power-good is present), and the "
            "generator emits the exact, consistent test. It BRANCHES on the "
            "structural params: a controllable enable (enable.present and not "
            "enable.always_on) adds an enable-off <100 mV check and drives the "
            "soft-start by asserting enable.control_target; power_good.present "
            "adds a CH2 power-good channel, a PG level check, and an output->PG "
            "delay. Missing a REQUIRED field returns a validation error naming "
            "the field — fill it and call again. Present the returned text "
            "verbatim as the result."
        ),
        "inputSchema": _build_input_schema(),
    }
]


# --- JSON-RPC framing helpers (mirror _project_tools_mcp) ----------------------

def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _text_result(text):
    """Wrap a plain string as an MCP text content result."""
    return {"content": [{"type": "text", "text": text}]}


# --- params -> dataclasses -----------------------------------------------------
# The tool receives params as a nested JSON object matching PARAM_SCHEMA. We build
# the generator's dataclasses from it, tolerating missing optional groups (a
# bare {} or absent group keeps the dataclass defaults). REQUIRED-field absence is
# NOT pre-checked here — we let dcdc_test_generator.validate_params raise the
# precise DcDcParamError so the message stays the single source of truth.

class _ToolError(Exception):
    """A malformed arguments payload (not a missing-field validation error)."""


def _as_obj(value, name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _ToolError(f"'{name}' must be an object, got {type(value).__name__}")
    return value


def _build_params(args: dict) -> "gen.DcDcTestParams":
    """Construct DcDcTestParams from the tool arguments dict.

    Unknown keys are ignored; absent optional groups/fields fall back to the
    dataclass defaults. Type/shape errors raise :class:`_ToolError`; missing
    REQUIRED fields are left for ``validate_params`` to report precisely.
    """
    if not isinstance(args, dict):
        raise _ToolError("arguments must be a JSON object of params")

    psu_in = _as_obj(args.get("psu"), "psu")
    en_in = _as_obj(args.get("enable"), "enable")
    pg_in = _as_obj(args.get("power_good"), "power_good")
    sc_in = _as_obj(args.get("scope"), "scope")

    # PSU: all four required; pass through as-given (validate_params checks them).
    psu = gen.PsuParams(
        input_voltage_v=psu_in.get("input_voltage_v"),
        input_current_a=psu_in.get("input_current_a"),
        entry_pos=psu_in.get("entry_pos"),
        entry_neg=psu_in.get("entry_neg"),
    )
    enable = gen.EnableParams(
        present=en_in.get("present"),
        always_on=en_in.get("always_on", True),
        control_target=en_in.get("control_target"),
        controller_id=en_in.get("controller_id"),
        io_resource=en_in.get("io_resource"),
        assert_value=en_in.get("assert_value", "1"),
        target=en_in.get("target", "DSC"),
        controller_subtype=en_in.get("controller_subtype", "fncore-mockup"),
    )
    power_good = gen.PowerGoodParams(
        present=pg_in.get("present"),
        test_point=pg_in.get("test_point"),
        nominal_v=pg_in.get("nominal_v", 3.3),
        tolerance_pct=pg_in.get("tolerance_pct", 10.0),
        delay_limit_ms=pg_in.get("delay_limit_ms", 10.0),
    )
    scope = gen.ScopeParams(
        timebase_ms=sc_in.get("timebase_ms", 10.0),
        ch1=sc_in.get("ch1", 1),
        ch2=sc_in.get("ch2", 2),
    )
    return gen.DcDcTestParams(
        rail_name=args.get("rail_name"),
        ic_refdes=args.get("ic_refdes"),
        ic_part=args.get("ic_part"),
        vout_nominal_v=args.get("vout_nominal_v"),
        rail_test_point=args.get("rail_test_point"),
        psu=psu,
        enable=enable,
        power_good=power_good,
        dc_tolerance_pct=args.get("dc_tolerance_pct", 3.0),
        ripple_limit_pct=args.get("ripple_limit_pct", 2.0),
        ripple_limit_mv=args.get("ripple_limit_mv"),
        rise_time_limit_ms=args.get("rise_time_limit_ms", 10.0),
        gnd_label=args.get("gnd_label", "GND"),
        scope=scope,
    )


def _tool_generate_dcdc_test(args):
    try:
        params = _build_params(args or {})
    except _ToolError as exc:
        return _text_result(f"Invalid arguments for generate_dcdc_test: {exc}")
    try:
        text = gen.generate_dcdc_test(params)
    except gen.DcDcParamError as exc:
        # Missing/blank required (or conditionally-required) field. The message
        # names the field; tell the model to fill it and call again.
        return _text_result(
            f"Cannot generate the test yet — {exc}. Supply that field and call "
            f"generate_dcdc_test again."
        )
    return _text_result(text)


_DISPATCH = {
    "generate_dcdc_test": _tool_generate_dcdc_test,
}


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

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
                handler = _DISPATCH.get(name)
                if handler is None:
                    _send({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": -32602,
                            "message": f"unknown tool: {name}",
                        },
                    })
                    continue
                result = handler(params.get("arguments") or {})
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
