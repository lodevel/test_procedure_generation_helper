"""Resolve the directories skills are discovered from — the three location
tiers (bundled / local / project).

Thin wiring over the repo-root path helpers in ``project_services`` (the single
source for those dirs — not re-derived here). The discovery LOGIC lives in
:mod:`.registry` and takes roots explicitly, so it stays testable without any of
these app/project paths.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .skill import SkillSource

log = logging.getLogger(__name__)

_SKILLS_SUBDIR = "authoring_skills"


def local_skills_dir() -> Optional[Path]:
    """Install-wide drop-in dir (all projects): the existing ``local_packages``
    folder, under an identifying ``authoring_skills/`` subfolder — the same
    place local pack sources are dropped.

    Returns None when ``project_services`` can't be imported (e.g. the editor
    run fully standalone)."""
    try:
        from project_services.config_manager import get_local_packages_dir
    except Exception:
        log.debug("project_services.config_manager unavailable; no local skills dir")
        return None
    return get_local_packages_dir() / _SKILLS_SUBDIR


def builtin_skills_dir() -> Optional[Path]:
    """Built-in library (committed, ships with the app): ``<repo>/authoring_skills``.

    Returns None when ``project_services`` can't be imported (fully standalone)."""
    try:
        from project_services.config_manager import get_builtin_skills_dir
    except Exception:
        log.debug("project_services.config_manager unavailable; no builtin skills dir")
        return None
    return get_builtin_skills_dir()


def project_skills_dir(project_root: Optional[Path]) -> Optional[Path]:
    """``<project>/authoring_skills`` — skills scoped to one project."""
    if not project_root:
        return None
    return Path(project_root) / _SKILLS_SUBDIR


def bundled_skills_dir(project_root: Optional[Path]) -> Optional[Path]:
    """``<project>/bundle/authoring_skills`` — skills shipped with a pack."""
    if not project_root:
        return None
    return Path(project_root) / "bundle" / _SKILLS_SUBDIR


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
