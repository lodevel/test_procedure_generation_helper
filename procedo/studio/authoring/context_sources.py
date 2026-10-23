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

from ..core.project_manager import (
    ATTACHED_NOTE,
    PROJECT_RULES_WIN,
    RuleDoc,
    attached_as_file,
    rule_doc_detail,
)
from .context import ContextItem, ContextSource
from .pdf_text import extract_pdf_text

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
            if path.suffix.lower() == ".pdf":
                # Decode the datasheet to text here (cheap context, any model).
                # An unreadable/scanned PDF -> a short note so the model knows it
                # is attached but unread, rather than silently dropped.
                text = extract_pdf_text(path)
                if text:
                    blocks.append(_wrap(f"{path.name} (extracted text)", text))
                else:
                    blocks.append(_wrap(
                        path.name,
                        "(PDF attached but no extractable text — likely a "
                        "scanned/image-only datasheet.)"))
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
    """The rule documents the Studio tabs send (``ProjectManager.get_rule_docs``):
    the bundle's rules, keyed by file name, then the project's own documents,
    keyed ``project/<name>``, after a line saying they win. A PDF is only named in
    the text: it goes as it is, attached as a file (:meth:`files`), as in the tabs.

    ``transform`` post-processes each text document (the wiring passes the
    frontmatter stripper)."""

    def __init__(
        self,
        rule_docs: Callable[[], Sequence[RuleDoc]],
        transform: Callable[[str], str] = lambda s: s,
    ):
        super().__init__("rules", "Rules")
        self._rule_docs = rule_docs
        self._transform = transform

    def list_items(self) -> list[ContextItem]:
        items = []
        for doc in self._rule_docs():
            try:
                size = doc.path.stat().st_size
            except OSError:  # gone since the scan
                continue
            items.append(ContextItem("rules", doc.key, doc.key, rule_doc_detail(doc, size)))
        return items

    def _chosen(self, keys: Sequence[str]) -> list[RuleDoc]:
        """The documents of ``keys``, in the documents' order (bundle rules first)."""
        wanted = set(keys)
        return [doc for doc in self._rule_docs() if doc.key in wanted]

    def files(self, keys: Sequence[str]) -> list[Path]:
        return [doc.path for doc in self._chosen(keys) if attached_as_file(doc.path)]

    def materialize(self, keys: Sequence[str]) -> str:
        blocks = []
        headed = False  # the project-wins line is in
        for doc in self._chosen(keys):
            if attached_as_file(doc.path):
                body = ATTACHED_NOTE
            else:
                try:
                    body = self._transform(doc.path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError) as exc:
                    log.warning("could not read rule %s: %s", doc.key, exc)
                    continue
            if not body.strip():
                continue
            if doc.origin == "project" and not headed:
                blocks.append(PROJECT_RULES_WIN)
                headed = True
            blocks.append(_wrap(doc.key, body))
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class ArtifactProvider:
    """One selectable artifact: a stable ``key``, a display ``label``, and a
    ``provide`` callable returning its current text (lazily, on materialize)."""

    key: str
    label: str
    provide: Callable[[], str]
    detail: str = ""
    empty_hint: str = ""


class ArtifactsSource(ContextSource):
    """Project artifacts (procedure text/json, test code, netlist). The set and
    its providers are injected so this stays free of the artifact manager and
    ODB inspection."""

    def __init__(self, providers: Sequence[ArtifactProvider]):
        super().__init__("artifacts", "Artifacts")
        self._providers = {p.key: p for p in providers}

    def list_items(self) -> list[ContextItem]:
        return [
            ContextItem("artifacts", p.key, p.label, p.detail, p.empty_hint)
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
