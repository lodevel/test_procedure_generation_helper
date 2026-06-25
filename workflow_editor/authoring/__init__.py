"""Authoring-skill feature.

Discover ``SKILL.md`` skills (a folder = an LLM system prompt + frontmatter,
optionally a ``tools.py``) and drive the skill-chat window. This package is the
pure-Python core — value object, single-folder loader, and cross-location
discovery. Qt surfaces live in the editor's UI modules and depend on this core,
not the other way round.
"""
from .context import ContextBundle, ContextItem, ContextSource, assemble
from .context_sources import (
    ArtifactProvider,
    ArtifactsSource,
    DocumentsSource,
    RulesSource,
)
from .netlist_text import (
    format_component_ids,
    format_other_component_ids,
    format_netlist,
)
from .registry import discover_skills, load_skills
from .skill import Skill, SkillSource
from .skill_chat import SkillChatSession, SkillTurn
from .skill_loader import SkillLoadError, load_skill, split_frontmatter

__all__ = [
    # skills
    "Skill",
    "SkillSource",
    "SkillLoadError",
    "load_skill",
    "split_frontmatter",
    "discover_skills",
    "load_skills",
    # context picker
    "ContextItem",
    "ContextBundle",
    "ContextSource",
    "assemble",
    "DocumentsSource",
    "RulesSource",
    "ArtifactsSource",
    "ArtifactProvider",
    "format_netlist",
    "format_component_ids",
    "format_other_component_ids",
    # skill chat (bridge)
    "SkillChatSession",
    "SkillTurn",
]
