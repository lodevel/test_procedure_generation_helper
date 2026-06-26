"""Discover *tool folders* — folders shipping MCP tools (``tools.py`` + ``tools.json``).

A tool folder is any immediate subfolder of a skill root that holds BOTH
``tools.py`` (the executable tools, loaded by ``_skill_tools_mcp.py``) and
``tools.json`` (the declared advert: ``{"server": <name>, "tools": [...]}``). This
uniformly covers a skill's own tools (``<skill>/tools.py``) and shared tools
(``common/tools.py``) — the scanner doesn't care whether the folder is a skill.

Pure logic, like :mod:`.registry`: :func:`discover_tool_folders` takes explicit
``(root, source)`` pairs; :func:`build_skill_tools_universe` is the convenience that
pulls the default roots from :mod:`.locations`. We read ``tools.json`` ONLY — never
import ``tools.py`` — so building the per-request tool-gate universe runs zero
skill code in the GUI process.

SECURITY (until the trust gate lands): only BUILTIN + BUNDLED tiers are scanned for
tool folders; LOCAL / PROJECT drop-ins are ignored (their ``tools.py`` is neither
read nor executed) so a project drop-in can't auto-run code nor shadow a trusted
``SERVER_NAME``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import locations
from .skill import SkillSource

log = logging.getLogger(__name__)

# A SERVER_NAME is the ONE key string: the opencode.json mcp-block key, the
# tool-gate universe key, AND (via OpenCode's <server>_<tool> namespacing) the
# per-request override key. Constrain it so it can't drift between those uses
# (a hyphen/space would mismatch the override key and fail OPEN).
_SERVER_RE = re.compile(r"^[a-z0-9_]+$")

# Host infrastructure servers — their tool keys are gated separately in
# _build_message_body (docs/rules always-on, read_pdf web-gated, project_tools
# checkbox). A tool folder must not claim one of these names (it would overwrite
# the infra block / inherit its always-on keys = privilege escalation).
_RESERVED_INFRA = frozenset({"pdf_tools", "project_tools"})

# Only these tiers are trusted to ship executable tools.
_TRUSTED_SOURCES = (SkillSource.BUILTIN, SkillSource.BUNDLED)


@dataclass(frozen=True)
class ToolFolder:
    """One discovered tool folder (its declared advert + where it lives)."""

    server: str
    tools: list[str]
    path: Path
    source: SkillSource


def _read_manifest(folder: Path) -> Optional[ToolFolder]:
    """Read + validate ``folder/tools.json`` (folder must also hold tools.py).

    Returns a :class:`ToolFolder` or ``None`` (logged) when the folder isn't a
    tool folder or its manifest is invalid/reserved/mis-charactered — fail-closed.
    """
    tools_py = folder / "tools.py"
    tools_json = folder / "tools.json"
    if not (tools_py.is_file() and tools_json.is_file()):
        return None
    try:
        manifest = json.loads(tools_json.read_text(encoding="utf-8"))
        server = manifest["server"]
        tools = list(manifest["tools"])
    except Exception as exc:  # noqa: BLE001 — one bad folder must not break discovery
        log.warning("skipping tool folder %s: bad tools.json (%s)", folder, exc)
        return None
    if not isinstance(server, str) or not _SERVER_RE.match(server):
        log.warning("skipping tool folder %s: server %r is not [a-z0-9_]+",
                    folder, server)
        return None
    if server in _RESERVED_INFRA:
        log.warning("skipping tool folder %s: server %r is a reserved infra name",
                    folder, server)
        return None
    if not all(isinstance(t, str) for t in tools):
        log.warning("skipping tool folder %s: tools must be strings", folder)
        return None
    return ToolFolder(server=server, tools=tools, path=folder, source=SkillSource.BUILTIN)


def discover_tool_folders(
    roots: Sequence[Tuple[Path, SkillSource]],
) -> list[ToolFolder]:
    """Discover tool folders under ``roots`` (BUILTIN/BUNDLED only) + resolve dups.

    Dedup mirrors skill precedence: by FOLDER NAME, higher :class:`SkillSource`
    wins (a same-source duplicate keeps the first seen). Then SERVER_NAME
    uniqueness is enforced across survivors — since SERVER_NAME is free-form, two
    different folders could declare the same name; a collision drops the later one
    (logged) rather than relying on nondeterministic dict-update order.
    """
    by_folder: dict[str, ToolFolder] = {}
    for root, source in roots:
        if source not in _TRUSTED_SOURCES:
            continue
        root = Path(root)
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir():
                continue
            tf = _read_manifest(folder)
            if tf is None:
                continue
            tf = ToolFolder(server=tf.server, tools=tf.tools, path=tf.path, source=source)
            existing = by_folder.get(folder.name)
            if existing is not None and source == existing.source:
                log.warning("duplicate tool folder %r in %s; keeping %s, ignoring %s",
                            folder.name, source.name, existing.path, tf.path)
                continue
            if existing is None or source > existing.source:
                by_folder[folder.name] = tf

    # Enforce SERVER_NAME uniqueness across the surviving folders.
    out: list[ToolFolder] = []
    seen: dict[str, ToolFolder] = {}
    for tf in by_folder.values():
        clash = seen.get(tf.server)
        if clash is not None:
            log.warning("SERVER_NAME collision %r: keeping %s, dropping %s",
                        tf.server, clash.path, tf.path)
            continue
        seen[tf.server] = tf
        out.append(tf)
    return sorted(out, key=lambda t: t.server)


def build_skill_tools_universe(project_root: Optional[Path] = None) -> dict[str, list[str]]:
    """``{server: [tool names]}`` for every discovered tool folder.

    This is the per-request tool-gate universe: the backend forces every tool of a
    server the active skill did NOT declare to an explicit ``False`` (OpenCode's
    tool override is additive, so an un-listed tool keeps its enabled default).
    Computed in-process (pure ``tools.json`` reads) so it can be refreshed cheaply
    on the same line that rebuilds the launch config.
    """
    return {
        tf.server: list(tf.tools)
        for tf in discover_tool_folders(locations.skill_roots(project_root))
    }
