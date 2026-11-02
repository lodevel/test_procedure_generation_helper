"""Load a single skill folder into a :class:`.skill.Skill`.

Kept separate from discovery (:mod:`.registry`) so parsing one folder is
independently testable. Frontmatter is YAML between ``---`` fences; the body
after the closing fence is the LLM system prompt.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from procedo.core.bundle_generator import skill_file

from .skill import SKILL_CHAT_KIND, Skill, SkillFolder

log = logging.getLogger(__name__)

# Exact match only — unlike SKILL.md (the folder's identity, matched
# case-insensitively), the optional tools module follows Python's own casing.
_TOOLS_FILE = "tools.py"


class SkillLoadError(Exception):
    """A skill folder exists but cannot be loaded (missing/empty SKILL.md, or
    malformed frontmatter). Discovery logs and skips these — never crashes."""


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---``-fenced YAML frontmatter from the markdown body.

    Returns ``(metadata, body)``. No frontmatter (or an unterminated opening
    fence) yields ``({}, text)``. Malformed YAML, or frontmatter that isn't a
    mapping, raises :class:`SkillLoadError`.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                raise SkillLoadError(f"malformed frontmatter: {exc}") from exc
            if not isinstance(meta, dict):
                raise SkillLoadError("frontmatter is not a mapping")
            return meta, body.lstrip("\n")
    return {}, text  # opening fence but no closing fence — all body


def load_skill(place: SkillFolder) -> Skill:
    """Load one skill folder. Raises :class:`SkillLoadError` on any problem."""
    folder = place.folder
    path = skill_file(folder)
    if path is None:
        raise SkillLoadError(f"no SKILL.md in {folder}")
    text = path.read_text(encoding="utf-8-sig")
    meta, body = split_frontmatter(text)
    body = body.strip()
    if not body:
        raise SkillLoadError(f"empty SKILL.md body in {folder}")
    tools = folder / _TOOLS_FILE
    return Skill(
        skill_id=place.id,
        title=str(meta.get("name") or folder.name),
        system_prompt=body,
        path=folder,
        source=place.source,
        provenance=place.provenance,
        # ``description`` is the Anthropic Agent-Skills standard field; accept it
        # as an alias for our ``when-to-use``.
        when_to_use=str(meta.get("when-to-use") or meta.get("description") or ""),
        target=str(meta.get("target", "")),
        version=str(meta.get("version", "")),
        # Missing kind defaults to a chat skill (the common hand-authored case);
        # a wizard must declare ``kind: wizard`` explicitly.
        kind=str(meta.get("kind") or SKILL_CHAT_KIND),
        tools_path=tools if tools.is_file() else None,
        metadata=meta,
    )
