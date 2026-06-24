"""Resolve the directories skills are discovered from — the three location
tiers (bundled / user / project).

Thin wiring over :func:`get_app_data_dir` (the app's single source for the
per-user config dir — not re-derived here). The discovery LOGIC lives in
:mod:`.registry` and takes roots explicitly, so it stays testable without any
of these app/project paths.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .skill import SkillSource

log = logging.getLogger(__name__)

_SKILLS_SUBDIR = "authoring_skills"


def user_skills_dir() -> Optional[Path]:
    """Per-user drop-in dir (all projects): ``<app-data>/authoring_skills``.

    Returns None when the app-data dir can't be resolved (e.g. the editor run
    standalone without ``project_services`` on the path)."""
    try:
        from project_services.app_settings import get_app_data_dir
    except Exception:
        log.debug("project_services.app_settings unavailable; no user skills dir")
        return None
    return get_app_data_dir() / _SKILLS_SUBDIR


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
        (bundled_skills_dir(project_root), SkillSource.BUNDLED),
        (user_skills_dir(), SkillSource.USER),
        (project_skills_dir(project_root), SkillSource.PROJECT),
    ]
    return [(d, src) for d, src in candidates if d is not None and d.is_dir()]
