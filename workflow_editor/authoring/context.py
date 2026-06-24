"""Context-source abstraction for the skill-chat picker.

A *context source* exposes a list of checkable items (rules, documents,
artifacts) and materializes a chosen subset into LLM-ready text. The picker UI
renders any source uniformly; :func:`assemble` concatenates the checked items
across sources into one push payload and reports its size.

Pure: no Qt, no managers — concrete sources (:mod:`.context_sources`) inject
their dependencies, so this layer and the assembler are unit-testable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ContextItem:
    """One checkable unit within a source (a rule file, a document, an
    artifact). ``key`` is unique within the source and is what gets passed back
    to :meth:`ContextSource.materialize`."""

    source_id: str
    key: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class ContextBundle:
    """The assembled push payload: the concatenated text plus its size (for the
    picker's token/char readout)."""

    text: str
    char_count: int


class ContextSource(ABC):
    """A named provider of checkable context items."""

    def __init__(self, source_id: str, title: str):
        self.source_id = source_id
        self.title = title

    @abstractmethod
    def list_items(self) -> list[ContextItem]:
        """The items the user may check (may be empty)."""

    @abstractmethod
    def materialize(self, keys: Sequence[str]) -> str:
        """Render the chosen ``keys`` to text. Unknown keys are ignored."""


def assemble(
    selections: Sequence[tuple[ContextSource, Sequence[str]]],
) -> ContextBundle:
    """Concatenate the checked items across sources into one payload.

    Each source contributes a ``# <title>`` section; sources with no checked
    keys (or empty content) are skipped entirely."""
    blocks: list[str] = []
    for source, keys in selections:
        if not keys:
            continue
        body = source.materialize(keys).strip()
        if not body:
            continue
        blocks.append(f"# {source.title}\n\n{body}")
    text = "\n\n".join(blocks)
    return ContextBundle(text=text, char_count=len(text))
