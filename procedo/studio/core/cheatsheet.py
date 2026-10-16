"""DSL cheat-sheet + reference-doc extraction from the active bundle's rule docs.

The authoritative authoring spec is the per-pack rule ``.md`` docs shipped IN the
bundle (``<project>/bundle/rules/``, built from the wheel/packs). This module
renders a compact cheat-sheet from them at runtime, and lists the full docs as
reference pages.

**Migration seam:** if a future bundle build ships a pre-rendered
``rules/cheatsheet.md``, :func:`build_cheatsheet` returns it verbatim instead of
extracting — so the "what is the procedure syntax" content can move INTO the
wheel/bundle without any editor change.

Everything degrades to ``""`` / ``[]`` when there is no rules dir / no docs — the
feature is optional (the operator simply sees a friendly empty state).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PREBUILT_NAME = "cheatsheet.md"   # a bundle-shipped sheet takes precedence
_SCHEMA_RE = re.compile(r"schema", re.IGNORECASE)


# --------------------------------------------------------------------------
# Fence-aware doc model
# --------------------------------------------------------------------------

def _parse_doc(text: str) -> list[tuple[str, str]]:
    """``[(kind, line)]`` where kind is 'heading'|'fence'|'text'. Headings inside
    fenced code blocks are NOT headings."""
    out: list[tuple[str, str]] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.rstrip("\r")
        if stripped.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(("fence", stripped))
        elif not in_fence and re.match(r"^#{1,6} ", stripped):
            out.append(("heading", stripped))
        else:
            out.append(("text", stripped))
    return out


def _first_h1(lines: list[tuple[str, str]]) -> Optional[str]:
    for kind, line in lines:
        if kind == "heading" and line.startswith("# "):
            return line[2:].strip()
    return None


def _norm(s: str) -> str:
    """Normalize a heading for matching: drop leading #, backticks, lowercase."""
    return re.sub(r"`", "", re.sub(r"^#+ ", "", s)).strip().lower()


def _section_body(lines, predicate):
    """Body lines of the first section whose heading matches *predicate*, up to
    the next heading of same-or-higher level. Returns ``(heading, body)`` or None."""
    start = level = None
    body: list[str] = []
    for kind, line in lines:
        if kind == "heading":
            lvl = len(line) - len(line.lstrip("#"))
            if start is None:
                if predicate(_norm(line)):
                    start, level = line, lvl
            elif lvl <= level:
                return start, body
        elif start is not None:
            body.append(line)
    return (start, body) if start is not None else None


def _first_table(body) -> Optional[list[str]]:
    tbl, in_tbl = [], False
    for line in body:
        if line.lstrip().startswith("|"):
            tbl.append(line)
            in_tbl = True
        elif in_tbl:
            break
    return tbl or None


def _first_fence(body) -> Optional[list[str]]:
    blk, in_blk = [], False
    for line in body:
        if line.lstrip().startswith("```"):
            blk.append(line)
            if in_blk:
                return blk
            in_blk = True
        elif in_blk:
            blk.append(line)
    return None


def _canonical_form_tables(lines):
    """Fallback: every table whose header row mentions 'Canonical form', each
    prefixed by its nearest preceding heading. Recovers verb tables whose
    section heading varies (e.g. the scope doc has no 'Verbs and forms')."""
    results, cur_heading, tbl, hdr = [], None, [], None

    def flush():
        nonlocal tbl, hdr
        if tbl and tbl[0].count("|") >= 2 and "canonical form" in tbl[0].lower():
            results.append((hdr, list(tbl)))
        tbl, hdr = [], None

    for kind, line in lines:
        if kind == "heading":
            flush()
            cur_heading = line
        elif line.lstrip().startswith("|"):
            if not tbl:
                hdr = cur_heading
            tbl.append(line)
        else:
            flush()
    flush()
    return results


# --------------------------------------------------------------------------
# Bundle access
# --------------------------------------------------------------------------

def _manifest_entries(root: Path) -> list[dict]:
    """Ordered doc entries from ``manifest.json``; falls back to globbing
    ``*.md`` in filename order when the manifest is missing/unreadable."""
    try:
        data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return sorted(data, key=lambda e: e.get("index", 0))
    except (OSError, ValueError):
        # Expected when the manifest is missing/corrupt — glob fallback below.
        logger.debug("manifest.json unreadable under %s; falling back to *.md glob", root)
    return [{"index": i, "filename": p.name, "source": ""}
            for i, p in enumerate(sorted(root.glob("*.md")), 1)]


def doc_title(filename: str, text: str) -> str:
    """A friendly title: the doc's first H1, else the filename with its
    ``NNN_pack-<name>_`` prefix and ``.md`` stripped."""
    h1 = _first_h1(_parse_doc(text))
    if h1:
        return h1
    name = re.sub(r"^\d+_", "", filename)
    name = re.sub(r"\.md$", "", name)
    return name.replace("_", " ")


def list_docs(rules_root) -> list[dict]:
    """``[{title, filename, source, text}]`` in manifest order — the full rule
    docs as reference pages. Empty when there is no readable rules dir."""
    if rules_root is None:
        return []
    root = Path(rules_root)
    docs: list[dict] = []
    for entry in _manifest_entries(root):
        fn = entry.get("filename", "")
        try:
            text = (root / fn).read_text(encoding="utf-8")
        except OSError:
            continue
        docs.append({
            "title": doc_title(fn, text),
            "filename": fn,
            "source": entry.get("source", ""),
            "text": text,
        })
    return docs


# --------------------------------------------------------------------------
# Cheat-sheet
# --------------------------------------------------------------------------

def build_cheatsheet(rules_root) -> str:
    """Compact DSL cheat-sheet (markdown) for the bundle at *rules_root*.

    Prefers a bundle-shipped ``rules/cheatsheet.md`` (the wheel migration seam);
    else extracts verb/directive/criterion tables, equipment lines and per-pack
    verb-form tables from the docs. Returns ``""`` when rules_root is missing or
    has no docs."""
    if rules_root is None:
        return ""
    root = Path(rules_root)

    prebuilt = root / PREBUILT_NAME
    if prebuilt.is_file():
        try:
            return prebuilt.read_text(encoding="utf-8")
        except OSError:
            # Prebuilt sheet unreadable — fall through to runtime extraction.
            logger.debug("prebuilt %s unreadable; extracting cheat-sheet at runtime", prebuilt)

    entries = _manifest_entries(root)
    if not entries:
        return ""

    sheet: list[str] = [
        "# DSL cheat-sheet",
        "",
        "_Auto-extracted from the active bundle's rule docs. The full reference "
        "is in the per-doc pages on the left._",
        "",
    ]

    for entry in entries:
        fn = entry.get("filename", "")
        try:
            text = (root / fn).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = _parse_doc(text)
        title = _first_h1(lines) or doc_title(fn, text)

        if _SCHEMA_RE.search(fn):
            continue  # JSON-schema doc — no DSL authoring surface

        has_verbs = _section_body(lines, lambda h: h == "canonical verbs") is not None

        sheet += [f"## {title}", ""]

        if has_verbs:
            # Base doc: canonical verbs + directives + criterion comparators.
            for label, pred in [
                ("Canonical verbs", lambda h: h == "canonical verbs"),
                ("Directive set", lambda h: h == "directive set"),
            ]:
                sec = _section_body(lines, pred)
                tbl = _first_table(sec[1]) if sec else None
                if tbl:
                    sheet += [f"### {label}", ""] + tbl + [""]
            sec = _section_body(lines,
                                lambda h: "expected" in h and "criterion" in h)
            if sec:
                tbl = _first_table(sec[1])
                if tbl:
                    sheet += ["### Expected-criterion comparators", ""] + tbl + [""]
                ex = _first_fence(sec[1])
                if ex:
                    sheet += ["### Criterion examples", ""] + ex + [""]
            continue

        # Pack doc: equipment line + verbs-and-forms + common mistakes.
        sec = _section_body(lines, lambda h: h == "equipment line")
        fence = _first_fence(sec[1]) if sec else None
        if fence:
            sheet += ["### Equipment line", ""] + fence + [""]

        sec = _section_body(lines, lambda h: h.startswith("verbs and forms"))
        tbl = _first_table(sec[1]) if sec else None
        if tbl:
            sheet += ["### Verbs and forms", ""] + tbl + [""]
        else:
            found = _canonical_form_tables(lines)
            if found:
                for hdg, t in found:
                    label = re.sub(r"^#+ ", "", hdg) if hdg else "Ops"
                    sheet += [f"### {label}", ""] + t + [""]
            elif sec:
                fb = _first_fence(sec[1])
                if fb:
                    sheet += ["### Verbs and forms", ""] + fb + [""]

        sec = _section_body(lines, lambda h: h.startswith("common mistakes"))
        tbl = _first_table(sec[1]) if sec else None
        if tbl:
            sheet += ["### Common mistakes (wrong -> right)", ""] + tbl + [""]

    return "\n".join(sheet) + "\n"
