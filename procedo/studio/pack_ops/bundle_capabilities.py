"""What the active bundle declares, read from its ``defaults.json`` without
importing any pack: equipment profiles and per-type capabilities, and the
gating of an action on the equipment a procedure uses. (Bench fields are
read by the host's ``procedo.core.bundle_registry``.)"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from procedo.core import bundle_registry, studio_launch_env
from procedo.engine_client.engine_session import ParserUnavailable



def load_bundle_equipment_profiles(
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
