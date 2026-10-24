"""What the active bundle declares, read from its ``defaults.json`` without
importing any pack: equipment profiles and per-type capabilities, and the
gating of an action on the equipment a procedure uses. (Bench fields are
read by the host's ``procedo.core.bundle_registry``.)"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from procedo.core import bundle_registry, section_ownership, studio_launch_env
from procedo.engine_client.engine_session import ParserUnavailable



def _load_bundle_equipment_profiles(
    project_root: Optional[Path] = None,
) -> list[dict]:
    """Read ``equipment_profiles`` from the bundle's defaults.json.

    Task #39: resolved project-root-first via :func:`defaults_path`
    (``<project_root>/bundle/defaults.json`` when a project is open, so
    a bundle imported mid-session is seen immediately), falling back to
    ``PROCEDO_BUNDLE_DEFAULTS_PATH`` — the same convention as
    ``task_config_defaults.load_pack_workflow_defaults``.
    Returns ``[]`` when no bundle resolves or the file is missing /
    malformed (no bundle context). A readable bundle defaults.json
    WITHOUT the ``equipment_profiles`` key is a pre-bundle/2 artifact —
    that is a hard error (D4), never a silent ``[]``.
    """
    data = bundle_registry.read_defaults(defaults_path(project_root))
    if data is None:
        return []
    profiles = data.get("equipment_profiles")
    if not isinstance(profiles, list):
        raise ParserUnavailable(
            "bundle predates equipment_profiles (bundle/1) — rebuild the bundle"
        )
    return [p for p in profiles if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# Per-capability gating (parse / emit / schema / codegen) — read from the
# bundle's pack_dispatch.capabilities, so the editor can enable/disable actions
# per the equipment a procedure actually uses, without importing any pack.
# ---------------------------------------------------------------------------

# All four concerns a pack may provide for an equipment type.
CAPABILITIES = ("parse", "emit", "schema", "codegen")

_EQUIP_LINE_RE = None    # lazily compiled
_HEADING_LINE_RE = None  # lazily compiled


def defaults_path(project_root: Optional[Path]) -> Optional[Path]:
    """Resolve the bundle defaults.json: a project's ``<root>/bundle/defaults.json``
    when a project is open, else ``PROCEDO_BUNDLE_DEFAULTS_PATH`` (editor mode)."""
    if project_root is not None:
        return bundle_registry.defaults_path(Path(project_root))
    env = os.environ.get(studio_launch_env.BUNDLE_DEFAULTS_PATH)
    return Path(env) if env else None


def pack_capabilities(project_root: Optional[Path] = None) -> dict[str, dict]:
    """Return the bundle's per-equipment-type capability map
    ``{etype: {parse, schema, emit, codegen, pack}}``. Empty dict when no bundle
    / no capabilities block (pre-capability bundle) — callers treat 'absent' as
    'unknown', see :func:`can_for_types`."""
    data = bundle_registry.read_defaults(defaults_path(project_root))
    if data is None:
        return {}
    caps = (data.get("pack_dispatch") or {}).get("capabilities")
    return caps if isinstance(caps, dict) else {}


def _heading_name(line: str, known_sections: frozenset[str]) -> Optional[str]:
    """Section name if *line* is a ``##`` section heading, else None.

    Local mirror of the wheel parser's heading FORM semantics (the editor must
    not import the wheel at module level): ``##`` + OPTIONAL space/tab + name —
    ``## Equipment`` and ``##Equipment`` both name ``Equipment``; ``###...``
    and a bare ``##`` are content, never headings. The NO-SPACE form is a
    heading only for a name in *known_sections* (lowercased), so prose like
    ``##note`` stays content. Unlike the wheel, the known-name check is
    case-insensitive (the ownership-map keys are lowercase; labels are
    display-only)."""
    global _HEADING_LINE_RE
    if _HEADING_LINE_RE is None:
        _HEADING_LINE_RE = re.compile(r"^##(?!#)[ \t]*(\S.*?)[ \t]*$")
    m = _HEADING_LINE_RE.match(line)
    if m is None:
        return None
    name = m.group(1)
    if line[2:3] in (" ", "\t"):        # spaced form: heading for any name
        return name
    return name if name.lower() in known_sections else None


def _section_scan_names(
    project_root: Optional[Path] = None,
) -> tuple[str, frozenset[str]]:
    """Resolve ``(equipment_section_name, known_section_names)`` for the
    heading scan in :func:`equipment_types_in`; both lowercased.

    The section universe is bundle-driven — the ownership-map keys from
    :func:`pack_parsers.get_section_ownership` when loadable — falling back to the
    canonical v2.0.x names. The equipment section is the universe entry named
    ``equipment`` when declared, else the same literal: the ONE place to
    change if a bundle ever renames the section."""
    from . import pack_parsers  # its ownership map comes from the engine
    names: tuple[str, ...] = ()
    try:
        names = tuple(
            str(n).strip().lower() for n in pack_parsers.get_section_ownership(project_root))
    except Exception:  # noqa: BLE001 — no wheel/bundle reachable: fallback
        pass
    known = frozenset(n for n in names if n) or section_ownership.CANONICAL_SECTIONS
    # test_id has no '## ' heading form (it is the '# <id>' line) — it must
    # never gate the NO-SPACE heading match, or a literal '##test_id' body
    # line would terminate a section (wheel treats it as content).
    known = frozenset(n for n in known if n != "test_id")
    equipment = next((n for n in known if n == "equipment"), "equipment")
    return equipment, known


def equipment_types_in(
    procedure_text: str,
    project_root: Optional[Path] = None,
) -> list[str]:
    """Extract the declared equipment types from a procedure's ``## Equipment``
    block (lines ``<ID> : <type> [params]``). Order-preserving, de-duplicated.
    Used to decide which packs an action needs.

    Heading matching is lenient, mirroring the wheel (:func:`_heading_name`):
    ``## Equipment`` and ``##Equipment`` both open the block, case-insensitive;
    ``###...`` is content. The section name comes from the bundle's declared
    universe when available (:func:`_section_scan_names`)."""
    global _EQUIP_LINE_RE
    if _EQUIP_LINE_RE is None:
        _EQUIP_LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]*\s*:\s*(\S+)")
    target, known = _section_scan_names(project_root)
    types: list[str] = []
    in_block = False
    for raw in (procedure_text or "").splitlines():
        line = raw.strip()
        heading = _heading_name(line, known)
        if heading is not None:
            in_block = heading.strip().lower() == target
            continue
        if in_block:
            m = _EQUIP_LINE_RE.match(line)
            if m and m.group(1) not in types:
                types.append(m.group(1))
    return types


# Equipment types the base engine handles natively (no pack needed).
_BASE_NATIVE_TYPES = {"dmm"}


def can_for_types(
    capability: str,
    equipment_types,
    project_root: Optional[Path] = None,
) -> tuple[bool, list[tuple[str, str]]]:
    """Whether *capability* is available for EVERY one of *equipment_types* (from
    :func:`equipment_types_in`, or a JSON procedure's ``equipment[*].type``).
    Returns ``(ok, missing)`` where ``missing`` is a list of
    ``(equipment_type, pack_or_reason)`` pairs lacking it.

    Rules:
    - Base-native types (dmm) always satisfy every capability.
    - When the bundle declares NO capabilities block (pre-capability bundle),
      assume everything is available (back-compat — preserves today's behavior).
    - An equipment type absent from the capabilities map → no pack provides it
      → missing as ``(etype, "no pack")``.
    - A type present but with the capability flag false → missing as
      ``(etype, pack_name)``.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}; expected one of {CAPABILITIES}")
    caps = pack_capabilities(project_root)
    if not caps:
        return True, []  # pre-capability bundle: don't gate
    missing: list[tuple[str, str]] = []
    for etype in equipment_types:
        if etype in _BASE_NATIVE_TYPES:
            continue
        entry = caps.get(etype)
        if entry is None:
            missing.append((etype, "no pack"))
        elif not entry.get(capability, False):
            missing.append((etype, entry.get("pack", "?")))
    return (not missing), missing
