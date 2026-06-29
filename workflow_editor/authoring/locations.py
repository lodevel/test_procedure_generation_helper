"""Resolve the directories skills and wizards are discovered from — the four
location tiers (builtin / bundled / local / project).

Thin wiring over the repo-root path helpers in ``project_services`` (the single
source for those dirs — not re-derived here). The discovery LOGIC lives in
:mod:`.registry` and takes roots explicitly, so it stays testable without any of
these app/project paths.

Layout: app-level artifacts live under ``packages/{builtin,local}/{skills,wizards}``;
the project-level tiers keep ``authoring_skills/`` + ``authoring_wizards/`` (in the
project root and inside an applied bundle). The two naming schemes are kept
DECOUPLED on purpose — collapsing them onto one shared constant would silently
empty one set of tiers.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .skill import SkillSource

log = logging.getLogger(__name__)

# packages/{builtin,local}/<subdir> — the consolidated app-level layout.
_SKILLS_SUBDIR = "skills"
_WIZARDS_SUBDIR = "wizards"
# Project + bundled tiers keep the original naming — do NOT unify with the above.
_PROJECT_SKILLS_SUBDIR = "authoring_skills"
_PROJECT_WIZARDS_SUBDIR = "authoring_wizards"


def _local_packages_dir() -> Optional[Path]:
    """The gitignored drop-in root ``packages/local`` (None when project_services
    can't be imported, e.g. the editor run fully standalone)."""
    try:
        from project_services.config_manager import get_local_packages_dir
    except Exception:
        log.debug("project_services.config_manager unavailable; no local packages dir")
        return None
    return get_local_packages_dir()


def builtin_skills_dir() -> Optional[Path]:
    """``packages/builtin/skills`` — committed, ships with the app."""
    try:
        from project_services.config_manager import get_builtin_skills_dir
    except Exception:
        log.debug("project_services.config_manager unavailable; no builtin skills dir")
        return None
    return get_builtin_skills_dir()


def builtin_wizards_dir() -> Optional[Path]:
    """``packages/builtin/wizards`` — committed, ships with the app."""
    try:
        from project_services.config_manager import get_builtin_wizards_dir
    except Exception:
        log.debug("project_services.config_manager unavailable; no builtin wizards dir")
        return None
    return get_builtin_wizards_dir()


def builtin_tools_dir() -> Optional[Path]:
    """``packages/builtin/tools`` — shared author-tool servers (committed)."""
    try:
        from project_services.config_manager import get_builtin_tools_dir
    except Exception:
        log.debug("project_services.config_manager unavailable; no builtin tools dir")
        return None
    return get_builtin_tools_dir()


def local_skills_dir() -> Optional[Path]:
    """``packages/local/skills`` — gitignored drop-in (all projects on this install)."""
    base = _local_packages_dir()
    return base / _SKILLS_SUBDIR if base else None


def local_wizards_dir() -> Optional[Path]:
    """``packages/local/wizards`` — gitignored drop-in."""
    base = _local_packages_dir()
    return base / _WIZARDS_SUBDIR if base else None


def project_skills_dir(project_root: Optional[Path]) -> Optional[Path]:
    """``<project>/authoring_skills`` — skills scoped to one project."""
    if not project_root:
        return None
    return Path(project_root) / _PROJECT_SKILLS_SUBDIR


def project_wizards_dir(project_root: Optional[Path]) -> Optional[Path]:
    """``<project>/authoring_wizards`` — wizards scoped to one project."""
    if not project_root:
        return None
    return Path(project_root) / _PROJECT_WIZARDS_SUBDIR


def bundled_skills_dir(project_root: Optional[Path]) -> Optional[Path]:
    """``<project>/bundle/authoring_skills`` — skills shipped inside an applied bundle."""
    if not project_root:
        return None
    return Path(project_root) / "bundle" / _PROJECT_SKILLS_SUBDIR


def bundled_wizards_dir(project_root: Optional[Path]) -> Optional[Path]:
    """``<project>/bundle/authoring_wizards`` — wizards shipped inside an applied bundle."""
    if not project_root:
        return None
    return Path(project_root) / "bundle" / _PROJECT_WIZARDS_SUBDIR


def skill_roots(
    project_root: Optional[Path] = None,
) -> list[tuple[Path, SkillSource]]:
    """All EXISTING skill roots, tagged by source. Order is irrelevant to
    precedence — the registry resolves that by :class:`SkillSource`."""
    candidates = [
        (builtin_skills_dir(), SkillSource.BUILTIN),
        (bundled_skills_dir(project_root), SkillSource.BUNDLED),
        (local_skills_dir(), SkillSource.LOCAL),
        (project_skills_dir(project_root), SkillSource.PROJECT),
    ]
    return [(d, src) for d, src in candidates if d is not None and d.is_dir()]


def wizard_roots(
    project_root: Optional[Path] = None,
) -> list[tuple[Path, SkillSource]]:
    """All EXISTING wizard roots, tagged by source. Identical shape to
    :func:`skill_roots`; precedence is resolved by the registry by
    :class:`SkillSource`."""
    candidates = [
        (builtin_wizards_dir(), SkillSource.BUILTIN),
        (bundled_wizards_dir(project_root), SkillSource.BUNDLED),
        (local_wizards_dir(), SkillSource.LOCAL),
        (project_wizards_dir(project_root), SkillSource.PROJECT),
    ]
    return [(d, src) for d, src in candidates if d is not None and d.is_dir()]


def tool_roots(
    project_root: Optional[Path] = None,
) -> list[tuple[Path, SkillSource]]:
    """All EXISTING SHARED author-tool roots, tagged by source. Only the BUILTIN
    tier hosts shared tools — :func:`discover_tool_folders` executes code from
    BUILTIN/BUNDLED tiers only, so LOCAL/PROJECT tool folders would never run and
    are not advertised here. Skill-LOCAL tools still live inside a skill folder and
    are found via :func:`skill_roots`; passing both lists to discovery is additive."""
    candidates = [
        (builtin_tools_dir(), SkillSource.BUILTIN),
    ]
    return [(d, src) for d, src in candidates if d is not None and d.is_dir()]
