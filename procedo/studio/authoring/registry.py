"""Discover skills and wizards and resolve a reference by id.

Pure logic: :func:`discover_skills` takes explicit :class:`.skill.SkillFolder` entries, so it's
testable with temp dirs and no app/project wiring. :func:`load_skills` / :func:`load_wizards`
are the thin conveniences that pull the default folders from :mod:`.locations`.

Every copy is listed, each with its provenance. A REFERENCE by id (a wizard's ``requires:``,
the ``run_skill`` tool, the wizard API) takes one copy: :func:`skills_by_id`, the only place
that knows the order (project, then bundle, then package).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

from procedo.core.bundle_generator import skill_file

from . import locations
from .skill import Skill, SkillFolder
from .skill_loader import SkillLoadError, load_skill

log = logging.getLogger(__name__)


def discover_skills(folders: Sequence[SkillFolder]) -> list[Skill]:
    """Load every folder that holds a ``SKILL.md``; a folder that fails to load is logged
    and skipped — discovery never raises. All copies are kept, sorted by display title, the
    higher-precedence copy first."""
    found: list[Skill] = []
    for place in folders:
        if skill_file(place.folder) is None:
            continue
        try:
            found.append(load_skill(place))
        except SkillLoadError as exc:
            log.warning("skipping skill folder %s: %s", place.folder, exc)
        except Exception:  # noqa: BLE001 — one bad folder must not break discovery
            log.exception("unexpected error loading skill folder %s", place.folder)
    return sorted(found, key=lambda s: (s.title.lower(), -int(s.source), s.provenance))


def skills_by_id(copies: Iterable[Skill]) -> dict[str, Skill]:
    """The ONE copy a reference by id takes, per id: the highest :class:`.skill.SkillSource`
    (project, then bundle, then package)."""
    chosen: dict[str, Skill] = {}
    for skill in copies:
        held = chosen.get(skill.skill_id)
        if held is None or skill.source > held.source:
            chosen[skill.skill_id] = skill
    return chosen


def load_skills(project_root: Optional[Path] = None) -> list[Skill]:
    """Every skill copy for ``project_root``."""
    return discover_skills(locations.skill_folders(project_root))


def load_wizards(project_root: Optional[Path] = None) -> list[Skill]:
    """Every wizard copy for ``project_root``; the same shape as :func:`load_skills`."""
    return discover_skills(locations.wizard_folders(project_root))
