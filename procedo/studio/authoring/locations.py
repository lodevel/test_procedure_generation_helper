"""Where skills, wizards and tools are found: the folders of each copy, labelled with its
provenance.

Three places offer them: the switched-on packages (one folder per item, from
:mod:`procedo.core.package_store`; builtin and Local are packages like any other), the
project's bundle (``<project>/bundle/authoring_*``, ``<project>/bundle/tools``) and the
project itself (``<project>/authoring_*``). The discovery LOGIC lives in :mod:`.registry`
and takes the folders explicitly, so it stays testable without any of these paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from procedo.core import package_store

from .skill import SkillFolder, SkillSource

PROJECT_LABEL = "Project"
BUNDLE_LABEL = "Bundle"

_PROJECT_SKILLS_SUBDIR = "authoring_skills"
_PROJECT_WIZARDS_SUBDIR = "authoring_wizards"


def local_skills_dir() -> Path:
    """``packages/local/skills``, where *Open skills folder…* opens (the caller creates it)."""
    return package_store.local_root() / "skills"


def _package_folders(kind: str) -> list[SkillFolder]:
    return [
        SkillFolder(item.folder, item.id, SkillSource.PACKAGE, item.package.name)
        for item in package_store.items(kind)
    ]


def _children(root: Optional[Path], source: SkillSource, label: str) -> list[SkillFolder]:
    if root is None or not root.is_dir():
        return []
    return [SkillFolder(d, d.name, source, label) for d in sorted(root.iterdir()) if d.is_dir()]


def _project_folders(project_root: Optional[Path], subdir: str) -> list[SkillFolder]:
    """The bundle's and the project's folders of one kind."""
    if not project_root:
        return []
    root = Path(project_root)
    return _children(root / "bundle" / subdir, SkillSource.BUNDLED, BUNDLE_LABEL) + _children(
        root / subdir, SkillSource.PROJECT, PROJECT_LABEL
    )


def skill_folders(project_root: Optional[Path] = None) -> list[SkillFolder]:
    """Every skill folder: the packages', the bundle's, the project's. Order is irrelevant to
    precedence (:func:`.registry.skills_by_id` resolves it by :class:`SkillSource`)."""
    return _package_folders("skills") + _project_folders(project_root, _PROJECT_SKILLS_SUBDIR)


def wizard_folders(project_root: Optional[Path] = None) -> list[SkillFolder]:
    """Every wizard folder, as :func:`skill_folders`."""
    return _package_folders("wizards") + _project_folders(project_root, _PROJECT_WIZARDS_SUBDIR)


def tool_candidates(project_root: Optional[Path] = None) -> list[SkillFolder]:
    """The folders that may hold a tool server: the packages' tool items, the bundle's
    ``tools/`` and every skill folder (a skill may ship its own ``tools.py``). Whether a
    folder is one, and trusted to run, is :func:`.tool_folders.discover_tool_folders`'s."""
    bundled = (
        _children(Path(project_root) / "bundle" / "tools", SkillSource.BUNDLED, BUNDLE_LABEL)
        if project_root
        else []
    )
    return _package_folders("tools") + bundled + skill_folders(project_root)
