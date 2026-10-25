"""Task / chat configuration data types shared by the task-config modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import List, Optional, Union


# Either a list of rule filenames, the sentinel string ``"all"``, or ``None``
# (inherit from tab-level legacy ``selected_rules`` field at load time).
SelectedRules = Union[List[str], str, None]


@dataclass
class TaskConfig:
    """Configuration for a single LLM task.

    ``selected_rules`` resolves at load time: if a task has its own value
    it wins; otherwise the loader copies the deprecated tab-level
    ``selected_rules`` onto each task. ``None`` after load means "use all
    available rules" (consumer-side expansion).
    """

    id: str
    name: str
    button_label: str
    prompt_template: Optional[str] = None
    enabled: bool = True
    max_validator_attempts: Optional[int] = None
    selected_rules: SelectedRules = None
    # Per-task section-ownership override: the section names the LLM may
    # author for this task. ``None`` → use the bundle/wheel default
    # (current behavior). A list (incl. ``[]`` = LLM authors nothing) is
    # the authoritative LLM-owned set. Threaded into both the prompt
    # emit-list and reconstruction so they stay consistent.
    llm_owned_sections: Optional[list[str]] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskConfig":
        """Tolerant constructor — unknown keys are dropped so older configs
        (without newer fields) keep loading."""
        valid_keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid_keys})


@dataclass
class ChatConfig:
    """Per-tab configuration for the AD_HOC_CHAT feature (chat panel)."""

    enabled: bool = True
    system_prompt: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChatConfig":
        return cls(**{k: v for k, v in data.items() if k in ("enabled", "system_prompt")})


def clone_selected_rules(value: SelectedRules) -> SelectedRules:
    """Defensive copy so callers can't mutate cached state."""
    if isinstance(value, list):
        return list(value)
    return value
