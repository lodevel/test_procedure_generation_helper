"""Populate `media` arrays on procedure.json step ops by extracting
PCB component/pin references from each step's text.

Per the v1 design (restored 2026-04-28): every step optionally carries
``media: [{type: "image", ref: {component, pin}, caption: "..."}]``,
derived from the step's free-text description. The procedure GUI uses
these to render PCB images via the ODB CLI.

The extraction logic is **delegated**, in order of preference:

1. The bundle wheel's pack-side extractor
   (``rules_packager_base.rules.v2_0_2.media``, via
   :mod:`pack_parsers`) — the regex catalogue implements the pack's
   own writing conventions (canonical rules §"Test-point (TP)
   prefixing rule"), so it is bundle-versioned alongside the parser
   and schema.
2. The project's ``config/text_parser.py`` plugin — legacy fallback
   for projects whose installed wheel predates the media module.
3. No-op (empty media arrays) so the procedure remains schema-valid.

Per-op text sources:

  - ``connect`` / ``note`` / ``move``: extract from the op's ``text``
    field.
  - Ops with structured probe-point fields (``operator.read`` /
    ``operator.read_diff``): extract from ``location`` /
    ``location_a`` / ``location_b``.
  - All other ops: no free-text component refs (the structured fields
    point at equipment ids, channels, resource ids — not PCB
    components). ``media`` stays empty for them.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# Cache the loaded parser instance per project_root mtime to avoid
# re-importing the user's plugin on every save. The cache key is the
# project_root path; the value is (mtime, parser_instance_or_None).
_PARSER_CACHE: dict[Path, tuple[float, Optional[Any]]] = {}


def _load_project_text_parser(project_root: Path) -> Optional[Any]:
    """Dynamically import ``<project_root>/config/text_parser.py`` and
    return an instance of its ``ProcedureTextParser`` class.

    Returns ``None`` when the plugin is missing or fails to load.
    Cached on the parser file's mtime so multiple calls within a save
    cycle don't pay the import cost twice.
    """
    parser_path = project_root / "config" / "text_parser.py"
    if not parser_path.exists():
        return None
    try:
        mtime = parser_path.stat().st_mtime
    except OSError:
        return None
    cached = _PARSER_CACHE.get(project_root)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    spec = importlib.util.spec_from_file_location(
        f"_project_text_parser_{abs(hash(str(parser_path)))}",
        parser_path,
    )
    if spec is None or spec.loader is None:
        _PARSER_CACHE[project_root] = (mtime, None)
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        log.warning(
            "media_extraction: failed to import %s: %s", parser_path, exc,
        )
        _PARSER_CACHE[project_root] = (mtime, None)
        return None

    parser_cls = getattr(module, "ProcedureTextParser", None)
    if parser_cls is None:
        log.warning(
            "media_extraction: %s has no ProcedureTextParser class.",
            parser_path,
        )
        _PARSER_CACHE[project_root] = (mtime, None)
        return None
    try:
        instance = parser_cls()
    except Exception as exc:
        log.warning(
            "media_extraction: ProcedureTextParser() raised: %s", exc,
        )
        _PARSER_CACHE[project_root] = (mtime, None)
        return None

    _PARSER_CACHE[project_root] = (mtime, instance)
    return instance


def _extract_media_from_text(parser: Any, text: str) -> list[dict[str, Any]]:
    """Call the project parser's media extractor on ``text`` and return
    the resulting list of media dicts (or empty on failure).

    The plugin's API is ``_extract_media_refs(text: str) -> list[dict]``.
    Its output already matches the v1 ``media`` shape the procedure GUI
    expects (`{"type": "image", "ref": {component, pin}, "caption"}`).
    """
    extractor = getattr(parser, "_extract_media_refs", None)
    if extractor is None:
        return []
    try:
        result = extractor(text)
    except Exception as exc:
        log.warning("media_extraction: _extract_media_refs raised: %s", exc)
        return []
    if not isinstance(result, list):
        return []
    return result


# Op types whose `text` field carries free-text component refs worth
# scanning. Other ops are structured (device, channel, resource id) and
# don't carry PCB component references.
_TEXT_BEARING_OPS: frozenset[str] = frozenset({"connect", "note", "move"})

# Structured step fields that name a probe point on the PCB (e.g.
# ``operator.read``'s ``location``, ``operator.read_diff``'s
# ``location_a``/``location_b``). Scanned on every op that carries them.
_LOCATION_FIELDS: tuple[str, ...] = ("location", "location_a", "location_b")


def populate_media_on_steps(
    procedure_json: dict[str, Any],
    project_root: Optional[Path],
) -> int:
    """Mutate ``procedure_json`` so each step op's `media` field reflects
    the current step text.

    Media is **derived data** — extracted deterministically from each
    step's ``text`` by the project's text_parser plugin. It is NOT
    operator-pinned configuration like a VISA address. Any existing
    ``media`` on a step is **always overwritten** with a fresh
    extraction so that an updated step text immediately produces the
    correct media refs (no stale references survive an LLM regen or a
    text edit).

    Returns the number of steps whose extraction yielded any media.

    Silent no-op when no project root is given or the project has no
    ``config/text_parser.py``.
    """
    if project_root is None:
        return 0

    # Preferred route: the bundle wheel's pack-side extractor, so the
    # media policy is versioned with the parser and schema it belongs
    # to. ParserUnavailable (wheel predates the media module) and any
    # subprocess hiccup fall through to the project plugin below.
    try:
        from . import pack_parsers
        updated, n_populated = pack_parsers.populate_media(
            procedure_json, project_root
        )
        if updated is not procedure_json and isinstance(
            updated.get("steps"), list
        ):
            procedure_json["steps"] = updated["steps"]
        return n_populated
    except Exception as exc:  # noqa: BLE001 — any failure falls back
        log.debug(
            "media_extraction: pack-side populate_media unavailable "
            "(%s); falling back to project text_parser plugin.", exc,
        )

    parser = _load_project_text_parser(project_root)
    if parser is None:
        return 0

    steps = procedure_json.get("steps")
    if not isinstance(steps, list):
        return 0

    n_populated = 0
    # Depth-first over steps INCLUDING block bodies ("body" of
    # for/if/... blocks) — mirrors the pack-side extractor.
    stack: list = list(reversed(steps))
    while stack:
        step = stack.pop()
        if not isinstance(step, dict):
            continue
        body = step.get("body")
        if isinstance(body, list):
            stack.extend(reversed(body))
        sources: list[str] = []
        if step.get("op") in _TEXT_BEARING_OPS:
            text = step.get("text")
            if isinstance(text, str) and text.strip():
                sources.append(text)
        for field_name in _LOCATION_FIELDS:
            value = step.get(field_name)
            if isinstance(value, str) and value.strip():
                sources.append(value)
        if not sources:
            # Nothing scannable on this op — strip any stale media that
            # might have been left behind by an older parser pass.
            step.pop("media", None)
            continue
        media: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in sources:
            for entry in _extract_media_from_text(parser, source):
                key = repr(sorted((entry.get("ref") or {}).items())) + repr(
                    entry.get("caption")
                )
                if key not in seen:
                    seen.add(key)
                    media.append(entry)
        # Always assign — even an empty list — for round-trip
        # consistency: every scannable op carries the field.
        step["media"] = media
        if media:
            n_populated += 1

    return n_populated
