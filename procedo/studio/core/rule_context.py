"""Rule documents -> what the LLM gets: the chosen documents, their text, the files attached.

One reading for the Studio tabs (``TabContext``) and the skill chat (``RulesSource``): the
chosen documents in the documents' order (bundle rules first, then the project's own, see
``ProjectManager.get_rule_docs``); a text document's text without its byte-order mark and
without the build pipeline's frontmatter fields; a PDF only named (``ATTACHED_NOTE``), as it
goes as it is, attached as a file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .project_manager import ATTACHED_NOTE, PROJECT_RULES_WIN, RuleDoc, attached_as_file

log = logging.getLogger(__name__)


# YAML frontmatter fields stripped from rule docs before they ship to the LLM.
# These exist for the build pipeline (rules_index.json, audience routing) and add
# no semantic value the LLM uses to perform its tasks. Strip them on read so the
# markdown source on disk stays canonical while the LLM payload is leaner.
_LLM_USELESS_FRONTMATTER_FIELDS = frozenset({"related", "audience"})


def _strip_llm_useless_frontmatter(content: str) -> str:
    """
    Remove allowlisted top-level frontmatter fields from a markdown doc.

    Recognizes the standard `---\\n<yaml>\\n---\\n` frontmatter block at the
    start of the file. Only top-level fields whose name is in
    `_LLM_USELESS_FRONTMATTER_FIELDS` are stripped, including any continuation
    lines indented under them (so multi-line list values like
    `related:\\n  - a\\n  - b` are dropped as a unit). Other frontmatter is
    preserved as-is. Files without frontmatter are returned unchanged.
    """
    if not content.startswith("---\n"):
        return content
    end_marker_idx = content.find("\n---\n", 4)
    if end_marker_idx == -1:
        return content
    fm_body = content[4:end_marker_idx]
    rest = content[end_marker_idx + len("\n---\n"):]

    kept_lines: list[str] = []
    skipping = False
    for line in fm_body.split("\n"):
        # A top-level field starts at column 0 with `<name>:` (no leading space).
        if line and not line[0].isspace() and ":" in line:
            field_name = line.split(":", 1)[0].strip()
            skipping = field_name in _LLM_USELESS_FRONTMATTER_FIELDS
            if skipping:
                continue
        elif skipping:
            # Continuation line (indented, blank, or comment under a stripped field).
            continue
        kept_lines.append(line)

    new_fm = "\n".join(kept_lines)
    return f"---\n{new_fm}\n---\n{rest}"


def chosen_rule_docs(docs: Sequence[RuleDoc], keys: Iterable[str]) -> list[RuleDoc]:
    """The documents of *keys*, in the order of *docs* (bundle rules first); a key no document
    has any more is logged and skipped."""
    wanted = set(keys)
    chosen = [doc for doc in docs if doc.key in wanted]
    for key in sorted(wanted - {doc.key for doc in chosen}):
        log.warning("selected rule document not found: %s", key)
    return chosen


def attached_files(docs: Sequence[RuleDoc]) -> list[Path]:
    """The documents of *docs* sent as they are, attached as files (PDFs)."""
    return [doc.path for doc in docs if attached_as_file(doc.path)]


def rule_doc_texts(docs: Sequence[RuleDoc]) -> list[tuple[RuleDoc, str]]:
    """Each of *docs* with the text the LLM reads: a PDF's is ``ATTACHED_NOTE``; a document
    that cannot be read is logged and left out."""
    texts = []
    for doc in docs:
        if attached_as_file(doc.path):
            texts.append((doc, ATTACHED_NOTE))
            continue
        try:
            text = doc.path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("cannot read rule document %s: %s", doc.path, exc)
            continue
        texts.append((doc, _strip_llm_useless_frontmatter(text)))
    return texts


def _project_rules_preamble(attached: bool) -> str:
    """Heads the project's own rule documents, sent after the bundle rules: they win (owner
    decision 2026-10-19); *attached*: some come as files attached to the message."""
    return (f"\n{'='*60}\n# Project rules\n{'='*60}\n"
            + PROJECT_RULES_WIN + "\n"
            + ("Some of them are attached to this message as files.\n" if attached else ""))


def rules_prompt_text(docs: Sequence[RuleDoc]) -> Optional[str]:
    """The Studio tabs' rules text of *docs* (None: none): the bundle rules first, then the
    project's own documents under a header saying they win where the two conflict; a PDF is
    only named here, it is attached as a file."""
    parts: dict[str, list[str]] = {"bundle": [], "project": []}
    attached = False
    for doc, text in rule_doc_texts(docs):
        if attached_as_file(doc.path):
            attached = True
            text += "\n"
        title = "Project rules" if doc.origin == "project" else "Rules"
        parts[doc.origin].append(
            f"\n{'='*60}\n# {title} from: {doc.path.name}\n{'='*60}\n{text}")
    if parts["project"]:
        parts["project"].insert(0, _project_rules_preamble(attached))
    contents = parts["bundle"] + parts["project"]
    return "\n".join(contents) if contents else None
