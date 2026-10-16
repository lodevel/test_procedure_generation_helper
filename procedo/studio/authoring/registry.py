"""Discover skills across roots and resolve precedence.

Pure logic: :func:`discover_skills` takes explicit ``(root, source)`` pairs, so
it's testable with temp dirs and no app/project wiring. :func:`load_skills` is
the thin convenience that pulls the default roots from :mod:`.locations`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple

from . import locations
from .skill import Skill, SkillSource
from .skill_loader import SkillLoadError, find_skill_file, load_skill

log = logging.getLogger(__name__)


def _iter_skill_folders(root: Path) -> Iterator[Path]:
    """Yield immediate subfolders of ``root`` that contain a SKILL.md."""
    for child in sorted(root.iterdir()):
        if child.is_dir() and find_skill_file(child) is not None:
            yield child


def discover_skills(
    roots: Sequence[Tuple[Path, SkillSource]],
) -> list[Skill]:
    """Load every skill under ``roots`` and resolve duplicates by precedence.

    A folder that fails to load is logged and skipped — discovery never raises.
    When two skills share a ``skill_id`` the higher :class:`SkillSource` wins;
    a same-source collision keeps the first seen (and is logged, so an author
    whose skill is shadowed can tell why). Returns skills sorted by display
    title."""
    by_id: dict[str, Skill] = {}
    for root, source in roots:
        for folder in _iter_skill_folders(Path(root)):
            try:
                skill = load_skill(folder, source)
            except SkillLoadError as exc:
                log.warning("skipping skill folder %s: %s", folder, exc)
                continue
            except Exception:  # noqa: BLE001 — one bad folder must not break discovery
                log.exception("unexpected error loading skill folder %s", folder)
                continue
            existing = by_id.get(skill.skill_id)
            if existing is not None and skill.source == existing.source:
                log.warning(
                    "duplicate skill_id %r in %s; keeping %s, ignoring %s",
                    skill.skill_id, source.name, existing.path, skill.path,
                )
                continue
            if existing is None or skill.source > existing.source:
                by_id[skill.skill_id] = skill
    return sorted(by_id.values(), key=lambda s: s.title.lower())


def load_skills(project_root: Optional[Path] = None) -> list[Skill]:
    """Discover skills from the default roots for ``project_root``."""
    return discover_skills(locations.skill_roots(project_root))


def load_wizards(project_root: Optional[Path] = None) -> list[Skill]:
    """Discover wizards from the default roots for ``project_root`` — the
    ``authoring_wizards`` SIBLING of each skill root. Mirrors
    :func:`load_skills`; reuses :func:`discover_skills` unchanged."""
    return discover_skills(locations.wizard_roots(project_root))
