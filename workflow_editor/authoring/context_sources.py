"""Concrete context sources for the skill-chat picker: Rules, Documents,
Artifacts.

Each keeps its real dependency (project manager, a folder, the artifact
manager / netlist) at arm's length via injected callables, so the sources are
unit-testable with fakes and temp dirs. The Qt wiring builds them with the live
objects.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .context import ContextItem, ContextSource

log = logging.getLogger(__name__)


def _wrap(label: str, body: str) -> str:
    """A ``## label`` sub-block (sources contribute these; ``assemble`` adds the
    top-level ``# source`` header)."""
    return f"## {label}\n\n{body.strip()}"


class DocumentsSource(ContextSource):
    """Files dropped in the per-project ``documents/`` folder."""

    def __init__(self, documents_dir: Path):
        super().__init__("documents", "Documents")
        self._dir = Path(documents_dir)

    def _files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(
            p for p in self._dir.iterdir() if p.is_file() and not p.name.startswith(".")
        )

    def list_items(self) -> list[ContextItem]:
        return [
            ContextItem("documents", p.name, p.name, _human_size(p))
            for p in self._files()
        ]

    def materialize(self, keys: Sequence[str]) -> str:
        wanted = set(keys)
        blocks = []
        for path in self._files():
            if path.name not in wanted:
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                log.info("skipping non-text document %s", path.name)
                continue
            if body.strip():
                blocks.append(_wrap(path.name, body))
        return "\n\n".join(blocks)


class RulesSource(ContextSource):
    """Project rule docs — the same set the text-only tab sends.

    ``rules_files`` yields the available rule paths (e.g.
    ``project_manager.get_rules_files``); ``transform`` post-processes each
    file's text (the wiring passes the frontmatter stripper)."""

    def __init__(
        self,
        rules_files: Callable[[], Sequence[Path]],
        transform: Callable[[str], str] = lambda s: s,
    ):
        super().__init__("rules", "Rules")
        self._rules_files = rules_files
        self._transform = transform

    def _by_name(self) -> dict[str, Path]:
        return {p.name: p for p in self._rules_files()}

    def list_items(self) -> list[ContextItem]:
        return [
            ContextItem("rules", name, name) for name in sorted(self._by_name())
        ]

    def materialize(self, keys: Sequence[str]) -> str:
        by_name = self._by_name()
        blocks = []
        for name in keys:
            path = by_name.get(name)
            if path is None:
                continue
            try:
                body = self._transform(path.read_text(encoding="utf-8"))
            except OSError as exc:
                log.warning("could not read rule %s: %s", name, exc)
                continue
            if body.strip():
                blocks.append(_wrap(name, body))
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class ArtifactProvider:
    """One selectable artifact: a stable ``key``, a display ``label``, and a
    ``provide`` callable returning its current text (lazily, on materialize)."""

    key: str
    label: str
    provide: Callable[[], str]
    detail: str = ""


class ArtifactsSource(ContextSource):
    """Project artifacts (procedure text/json, test code, netlist). The set and
    its providers are injected so this stays free of the artifact manager and
    ODB inspection."""

    def __init__(self, providers: Sequence[ArtifactProvider]):
        super().__init__("artifacts", "Artifacts")
        self._providers = {p.key: p for p in providers}

    def list_items(self) -> list[ContextItem]:
        return [
            ContextItem("artifacts", p.key, p.label, p.detail)
            for p in self._providers.values()
        ]

    def materialize(self, keys: Sequence[str]) -> str:
        blocks = []
        for key in keys:
            provider = self._providers.get(key)
            if provider is None:
                continue
            try:
                content = provider.provide() or ""
            except Exception:  # noqa: BLE001 — a failing artifact must not break the rest
                log.exception("artifact provider %r failed", key)
                continue
            if content.strip():
                blocks.append(_wrap(provider.label, content))
        return "\n\n".join(blocks)


def _human_size(path: Path) -> str:
    try:
        n = float(path.stat().st_size)
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""
