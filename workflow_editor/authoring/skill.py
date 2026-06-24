"""Value objects for the authoring-skill feature.

A *skill* is a folder holding a ``SKILL.md`` (an LLM system prompt + YAML
frontmatter) and, optionally, a ``tools.py``. Skills are discovered from
several locations and drive the skill-chat window.

These are immutable value objects with no behaviour — loading
(:mod:`.skill_loader`) and discovery (:mod:`.registry`) own all of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Optional

# Default ``kind`` metadata for a chat skill (informational; may be shown in
# the UI). Type discrimination is STRUCTURAL, not flag-based: chat skills live
# in ``authoring_skills/`` and wizards in a parallel ``authoring_wizards/``
# folder the chat scanner never reads — so a wizard's SKILL.md is never on the
# chat-discovery path. The folder you drop into determines the type.
SKILL_CHAT_KIND = "skill-chat"


class SkillSource(IntEnum):
    """Where a skill was found. The integer value IS its precedence: when two
    skills share a ``skill_id`` the higher source wins — project overrides user
    overrides bundled, mirroring the pack drop-in-overrides-bundled rule."""

    BUNDLED = 1  # <project>/bundle/authoring_skills — shipped with a pack
    LOCAL = 2    # <repo>/local_packages/authoring_skills — drop-in, all projects
    PROJECT = 3  # <project>/authoring_skills — this project only


@dataclass(frozen=True)
class Skill:
    """One discovered, loaded skill.

    ``skill_id`` (the folder name) is the stable identity used for
    precedence/dedup; ``title`` (frontmatter ``name``) is for display.
    """

    skill_id: str
    title: str
    system_prompt: str
    path: Path
    source: SkillSource
    when_to_use: str = ""
    target: str = ""
    version: str = ""
    kind: str = "authoring"
    tools_path: Optional[Path] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_tools(self) -> bool:
        """True when the skill ships a ``tools.py`` — custom Python tools, a
        trust step gated behind later phases."""
        return self.tools_path is not None
