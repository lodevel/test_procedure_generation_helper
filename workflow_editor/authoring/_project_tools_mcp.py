#!/usr/bin/env python3
"""Dependency-free stdio MCP server exposing project-data tools the LLM PULLs.

MCP stdio transport = newline-delimited JSON-RPC 2.0 (one JSON object per line).
This server is launched by the editor as a ``local`` MCP server, given the
project's ODB++ archive via ``--odb-tgz <path>``. Rather than push the entire
~124k-token netlist + every component property at the model up front, it lets
the model PULL exactly what it needs: discover the property-field schema, then
project/filter only the columns and rows it wants.

Every tool is a thin wrapper around the ODB++ CLI
(``external/odb_image_generator/cli.py``) inspection modes
(``--list-property-fields`` / ``--list`` ``--fields`` ``--filter`` /
``--list-nets``), invoked with the SAME interpreter running this server
(``sys.executable``) and its JSON stdout parsed. The parsed board (components +
nets) is cached per process because the tgz parse takes seconds.

Protocol handling mirrors the verified ``_pdf_tool_mcp`` skeleton: ``initialize``
echoes the client's ``protocolVersion``; notifications (no ``id``) get no
response; ``tools/list``; ``tools/call`` returns
``{"content":[{"type":"text",...}]}``; ``ping``.

Runs as a standalone script (launched by absolute path from an arbitrary cwd),
so it resolves the CLI path relative to its own location, not the launch cwd.
Board-agnostic by design: NO property field name is ever hardcoded — discovery
and projection are the whole point.
"""
import importlib.util
import json
import os
import subprocess
import sys


# --- locate the ODB++ CLI relative to THIS file --------------------------------
# This file lives at:
#   <repo>/external/test_procedure_generation_helper/workflow_editor/authoring/_project_tools_mcp.py
# Four dirs up from authoring/ lands on <repo>/external/; the CLI is its sibling
# package odb_image_generator/cli.py. Resolving from __file__ (not cwd) keeps the
# server launch-location-independent, exactly like _pdf_tool_mcp's import boot.
_AUTHORING_DIR = os.path.dirname(os.path.abspath(__file__))
_EXTERNAL_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(_AUTHORING_DIR))
)
# Default: the sibling odb_image_generator package's CLI. ``_PROJECT_TOOLS_CLI``
# overrides it (tests point this at a fake CLI so no real .tgz is needed); the
# default is board-agnostic and used in production.
_CLI_PATH = os.environ.get("_PROJECT_TOOLS_CLI") or os.path.join(
    _EXTERNAL_DIR, "odb_image_generator", "cli.py"
)


def _load_netlist_text():
    """Load the sibling ``netlist_text.py`` as a standalone module.

    This server runs as a bare script (no ``workflow_editor`` package on the
    path — see ``mcp_config.build_project_tools_mcp_block``), so neither a
    relative (``from .netlist_text``) nor an absolute package import works.
    ``netlist_text`` is pure stdlib (``re`` / ``typing``), so loading it by file
    path from this file's own directory is safe and launch-location-independent,
    mirroring how ``_CLI_PATH`` is resolved from ``__file__``. This is the SAME
    formatter the editor's PUSH context uses, so the ``netlist`` tool returns
    connectivity identical to the pushed netlist.
    """
    path = os.path.join(_AUTHORING_DIR, "netlist_text.py")
    spec = importlib.util.spec_from_file_location("_project_netlist_text", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_netlist_text = _load_netlist_text()


SERVER_INFO = {"name": "project-tools", "version": "1.0.0"}

TOOLS = [
    {
        "name": "list_property_fields",
        "description": (
            "Discover the board's component-property SCHEMA: the sorted list of "
            "every property field name present across all components (e.g. a "
            "part-number field, a package field, a type field — names are "
            "board/EDA/PLM-specific, NOT fixed). CALL THIS FIRST, before "
            "list_components, so you know which columns exist; then pull only the "
            "ones you need via list_components(fields=...) and narrow rows via "
            "list_components(filter=FIELD=VALUE). Avoids dumping the whole "
            "property table at you."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_components",
        "description": (
            "List the board's components, optionally narrowed to a refdes FAMILY "
            "(refdes_prefix), FILTERED to a property value, and PROJECTED to "
            "chosen property fields. "
            "TO FIND THE ICs AND THEIR PART NUMBERS IN ONE CALL: pass "
            "refdes_prefix='U,IC' together with the part-number field in fields "
            "(discover that field name first via list_property_fields — it is "
            "board-specific, e.g. a Manufacturer_Reference / part-number column). "
            "Do NOT dump the whole netlist to find ICs. "
            "refdes_prefix keeps only components whose refdes starts with one of "
            "the given prefixes followed by a digit (U,IC -> U1, IC3, ...; not "
            "UART). filter keeps only rows whose property KEY equals VALUE "
            "(case-insensitive; field from list_property_fields, NEVER guessed). "
            "To READ a property (e.g. the part number) for EVERY matched IC, put "
            "it in fields — NOT filter: filter NARROWS to a single value and HIDES "
            "all other ICs (filter='Manufacturer_Reference=TPS16630' returns only "
            "that one part, not the board's ICs). fields keeps only the columns you ask for "
            "(fields=['Type','Package'] keeps output small). Omit all three to "
            "get every component with all properties (large). Each result is "
            "{refdes, side, pins, properties}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refdes_prefix": {
                    "type": "string",
                    "description": "Keep only components whose refdes starts with "
                    "one of these comma-separated, case-insensitive prefixes "
                    "FOLLOWED BY A DIGIT (e.g. 'U,IC' to get just the ICs). "
                    "Composes with filter and fields.",
                },
                "filter": {
                    "type": "string",
                    "description": "Keep only components whose property KEY equals "
                    "VALUE, as 'KEY=VALUE' (case-insensitive value match). KEY "
                    "must be a real field from list_property_fields.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project each component's properties to ONLY "
                    "these field names (order preserved). Omit for all "
                    "properties.",
                },
            },
        },
    },
    {
        "name": "get_component",
        "description": (
            "Fetch ONE component by exact refdes (e.g. 'C45', 'R1', a test-point "
            "name). Use when you already know the refdes and want its details "
            "rather than scanning the whole list. Optionally project its "
            "properties to chosen fields. Returns {refdes, side, pins, "
            "properties} or a 'not found' message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refdes": {
                    "type": "string",
                    "description": "The exact reference designator of the "
                    "component to fetch.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project the component's properties to ONLY "
                    "these field names. Omit for all properties.",
                },
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "query_net",
        "description": (
            "Get the connectivity of ONE net by exact name: every {refdes, pin} "
            "node electrically tied to it. Use to answer 'what is connected to "
            "net X', to trace a signal, or to find which component pins share a "
            "rail. Prefer this over netlist() when you only need one net. "
            "Returns {net, nodes:[{refdes, pin}]} or a 'not found' message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The exact net name to query.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "netlist",
        "description": (
            "The FULL board netlist as compact text — every component's "
            "pins->nets AND every net's nodes (the same connectivity the "
            "editor's push context sends). Use query_net(name) for a single net, "
            "list_components for the BOM/part numbers."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_bom",
        "description": (
            "Return the project's Bill of Materials if a BOM file is present in "
            "the project, else a clear 'no BOM available' message. Best-effort: "
            "use it to cross-reference part numbers / quantities, but do not rely "
            "on it existing — component properties from list_components are the "
            "authoritative per-component data."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_test_points",
        "description": (
            "List the board's likely TEST POINTS with the net each one probes. "
            "Board-agnostic heuristic: single-pin components (the strongest "
            "board-independent test-point signal), each paired with the net on "
            "its lone pin. Use to find where to measure a signal. NOTE: boards "
            "label test points differently (a Type/Package property value, a "
            "naming convention, etc.) — if this heuristic is too broad or too "
            "narrow, discover the real marker with list_property_fields and "
            "refine via list_components(filter=FIELD=VALUE). Returns "
            "[{refdes, side, pin, net}]."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- JSON-RPC framing helpers (mirror _pdf_tool_mcp) ---------------------------

def _text_result(payload):
    """Wrap *payload* as an MCP text content result.

    Dicts/lists are JSON-encoded (the model gets structured data as text); plain
    strings pass through (for human-readable 'not found' / 'no BOM' messages).
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _components_result(rows):
    """``list_components`` result hardened against the tool-output SIZE CAP.

    Two changes vs a plain ``indent=2`` dump: (1) a loud ``count=`` header goes
    FIRST so it survives even when the body is cut, telling the model how many
    rows to expect; (2) the body is COMPACT JSON (no indent) so far more rows fit
    before the cap truncates. The model compares the rows it actually received
    against ``count`` to detect a dropped tail (the caller's completeness
    check) and re-pulls in refdes chunks if short.
    """
    note = (
        f"count={len(rows)} components. If FEWER component rows follow than this "
        f"count, the reply was TRUNCATED by the tool-output size cap — re-pull in "
        f"refdes chunks (refdes_prefix='U1', then 'U2', ...) or with fewer fields "
        f"until you have all {len(rows)}. Never classify from a partial list."
    )
    body = json.dumps({"components": rows}, separators=(",", ":"))
    return _text_result(note + "\n" + body)


# --- CLI shelling + per-process board cache -----------------------------------

class _CliError(Exception):
    """The ODB++ CLI failed (non-zero exit, missing file, or bad JSON)."""


def _run_cli(odb_tgz, extra_args):
    """Invoke ``cli.py`` with ``--odb-tgz`` + *extra_args*, return parsed JSON.

    Uses ``sys.executable`` (the same interpreter running this server) so the
    CLI gets the editor's venv. Raises :class:`_CliError` on non-zero exit or
    unparseable stdout — the caller turns that into a graceful tool message.
    """
    cmd = [sys.executable, _CLI_PATH, "--odb-tgz", odb_tgz] + list(extra_args)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise _CliError(f"could not launch ODB++ CLI: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or "(no stderr)"
        raise _CliError(f"ODB++ CLI exited {proc.returncode}: {err}")
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise _CliError(f"ODB++ CLI returned non-JSON output: {exc}") from exc


class _Board:
    """Per-process cache of the parsed board (the tgz parse is slow).

    Each distinct artifact (``property_fields`` / ``netlist``) is fetched from
    the CLI at most once and memoized. ``list_components`` is NOT cached because
    its filter/fields shape the CLI output; only the schema + full netlist are
    expensive whole-board reads worth holding.
    """

    def __init__(self, odb_tgz):
        self.odb_tgz = odb_tgz
        self._property_fields = None
        self._netlist = None

    def property_fields(self):
        if self._property_fields is None:
            self._property_fields = _run_cli(
                self.odb_tgz, ["--list-property-fields"]
            )
        return self._property_fields

    def netlist(self):
        if self._netlist is None:
            self._netlist = _run_cli(self.odb_tgz, ["--list-nets"])
        return self._netlist

    def components(self, fields=None, filter_kv=None, refdes_prefix=None):
        """Component list, narrowed by *fields* / *filter_kv* / *refdes_prefix*
        (not cached — each narrowing shapes the CLI output)."""
        args = ["--list"]
        if filter_kv:
            args += ["--filter", filter_kv]
        if fields:
            args += ["--fields", ",".join(fields)]
        if refdes_prefix:
            args += ["--refdes-prefix", refdes_prefix]
        return _run_cli(self.odb_tgz, args)


# --- tool implementations ------------------------------------------------------
# Each returns an MCP text-content result. CLI failures surface as a readable
# message (never an exception that kills the request loop).

def _err_result(exc):
    return _text_result(f"Could not read the project board: {exc}")


def _tool_list_property_fields(board, _args):
    try:
        return _text_result(board.property_fields())
    except _CliError as exc:
        return _err_result(exc)


def _tool_list_components(board, args):
    filter_kv = (args or {}).get("filter") or None  # "" means no filter, not malformed
    fields = (args or {}).get("fields")
    refdes_prefix = (args or {}).get("refdes_prefix")
    if filter_kv is not None and not isinstance(filter_kv, str):
        return _text_result("list_components: 'filter' must be a 'KEY=VALUE' string.")
    if filter_kv is not None and "=" not in filter_kv:
        return _text_result(
            "list_components: 'filter' must be 'KEY=VALUE' (missing '='), e.g. "
            "'Type=Pad'. The KEY must be a field from list_property_fields."
        )
    if fields is not None and not isinstance(fields, list):
        return _text_result("list_components: 'fields' must be a list of strings.")
    if refdes_prefix is not None and not isinstance(refdes_prefix, str):
        return _text_result(
            "list_components: 'refdes_prefix' must be a comma-separated string "
            "like 'U,IC'."
        )
    try:
        rows = board.components(
            fields=fields, filter_kv=filter_kv, refdes_prefix=refdes_prefix)
        return _components_result(rows)
    except _CliError as exc:
        return _err_result(exc)


def _tool_get_component(board, args):
    refdes = (args or {}).get("refdes")
    if not isinstance(refdes, str) or not refdes.strip():
        return _text_result("get_component requires a non-empty 'refdes' string.")
    refdes = refdes.strip()
    fields = (args or {}).get("fields")
    if fields is not None and not isinstance(fields, list):
        return _text_result("get_component: 'fields' must be a list of strings.")
    try:
        components = board.components(fields=fields)
    except _CliError as exc:
        return _err_result(exc)
    for comp in components:
        if comp.get("refdes") == refdes:
            return _text_result(comp)
    return _text_result(f"Component not found: {refdes}")


def _tool_query_net(board, args):
    name = (args or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return _text_result("query_net requires a non-empty 'name' string.")
    name = name.strip()
    try:
        data = board.netlist()
    except _CliError as exc:
        return _err_result(exc)
    for entry in data.get("nets", []):
        if entry.get("net") == name:
            return _text_result(entry)
    return _text_result(f"Net not found: {name}")


def _tool_netlist(board, _args):
    """The FULL board netlist as compact connectivity TEXT — every component's
    pins->nets AND every net's nodes — identical to what the editor's PUSH
    context sends (``format_netlist`` over the same ``--list-nets`` board). No
    cap / filter / paging: any subset breaks the connectivity graph, and the
    full compact text is small (~27k tokens on a real board). For ONE net use
    query_net(name); for the BOM / part numbers use list_components."""
    try:
        data = board.netlist()
    except _CliError as exc:
        return _err_result(exc)
    return _text_result(_netlist_text.format_netlist(data))


def _tool_get_bom(board, _args):
    """Best-effort BOM lookup beside the project's ODB archive.

    No standardized BOM location exists, so we probe a few common spellings in
    the archive's directory. Absent → a clear message (this tool is optional).
    """
    base = os.path.dirname(os.path.abspath(board.odb_tgz))
    candidates = [
        "bom.csv", "BOM.csv", "bom.txt", "BOM.txt",
        "bom.json", "BOM.json", "bom.xlsx", "BOM.xlsx",
    ]
    for name in candidates:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    return _text_result(fh.read())
            except OSError as exc:
                return _text_result(f"Found BOM {name} but could not read it: {exc}")
    return _text_result(
        "No BOM available for this project (no bom/BOM file beside the ODB "
        "archive). Use list_components for per-component part data instead."
    )


def _tool_list_test_points(board, _args):
    """Single-pin components + the net on their lone pin (board-agnostic).

    Single-pin placements are the strongest board-independent test-point signal;
    the per-pin net comes from the same ``--list-nets`` connectivity data.
    """
    try:
        data = board.netlist()
    except _CliError as exc:
        return _err_result(exc)
    points = []
    for comp in data.get("components", []):
        pins = comp.get("pins") or []
        if len(pins) != 1:
            continue
        pin = pins[0]
        points.append({
            "refdes": comp.get("refdes"),
            "side": comp.get("side"),
            "pin": pin.get("name"),
            "net": pin.get("net"),
        })
    return _text_result(points)


_DISPATCH = {
    "list_property_fields": _tool_list_property_fields,
    "list_components": _tool_list_components,
    "get_component": _tool_get_component,
    "query_net": _tool_query_net,
    "netlist": _tool_netlist,
    "get_bom": _tool_get_bom,
    "list_test_points": _tool_list_test_points,
}


def _parse_odb_tgz(argv):
    """Tiny manual argv parse for ``--odb-tgz <path>`` (default: empty)."""
    odb = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--odb-tgz":
            if i + 1 < len(argv):
                odb = argv[i + 1]
                i += 2
                continue
            i += 1
        elif arg.startswith("--odb-tgz="):
            odb = arg.split("=", 1)[1]
            i += 1
        else:
            i += 1
    return odb


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    odb_tgz = _parse_odb_tgz(argv)
    board = _Board(odb_tgz)  # lazy: nothing parsed until a tool is called

    import _mcp_serve  # sibling module (authoring/ is sys.path[0] for the
    # launched script); avoids triggering the heavy authoring/__init__ chain.
    dispatch = {name: (lambda a, h=h: h(board, a)) for name, h in _DISPATCH.items()}
    _mcp_serve.serve(SERVER_INFO, TOOLS, dispatch)


if __name__ == "__main__":
    main()
