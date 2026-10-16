"""Section ownership — single source of truth for which procedure sections
the LLM authors vs the parser reconstructs.

Canonical sections: test_id, description, meta, equipment, steps, expected.

Consumers (prompt builder, reconstruction call-site) import
:func:`for_bundle` or :class:`SectionOwnership`; they must not re-implement
the ownership logic themselves.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OWNER_PARSER: str = "parser"
OWNER_LLM: str = "llm"

# NOTE: the section universe (which sections exist + their order) is driven by
# the bundle's ownership map keys (see ``resolve``). The constants below are no
# longer the universe authority — they are the standalone fallback (the default
# map) plus the friendly-label lookup, and describe the v2.0.2 default ruleset.

#: Full set of recognised section names (lowercase) for the default ruleset.
CANONICAL_SECTIONS: frozenset[str] = frozenset(
    {"test_id", "title", "description", "meta", "equipment", "steps", "expected"}
)

#: Default emit order of procedure sections (matches the DSL); fallback when a
#: SectionOwnership has no declared ``section_order``.
CANONICAL_SECTION_ORDER: tuple[str, ...] = (
    "test_id", "title", "description", "meta", "equipment", "steps", "expected",
)

#: Human-facing heading label per section, for prompts/UI.
SECTION_HEADINGS: dict[str, str] = {
    "test_id": "# <TEST_ID>",
    "title": "## Title",
    "description": "## Description",
    "meta": "## Meta",
    "equipment": "## Equipment",
    "steps": "## Steps",
    "expected": "## Expected",
}

def heading_label(section: str) -> str:
    """Display heading for *section* (prompts/UI only — never parsed back).

    Canonical sections map through :data:`SECTION_HEADINGS`; a bundle-declared
    section outside it gets a derived label: ``"## "`` + the key with
    underscores as spaces, capitalised (``power_stage`` -> ``## Power stage``).
    """
    known = SECTION_HEADINGS.get(str(section).strip().lower())
    if known is not None:
        return known
    return "## " + str(section).strip().replace("_", " ").capitalize()


#: Baked-in default used when the bundle has no ``section_ownership.json``.
DEFAULT_OWNERSHIP: dict[str, str] = {
    "test_id": OWNER_PARSER,
    "title": OWNER_PARSER,
    "description": OWNER_PARSER,
    "meta": OWNER_PARSER,
    "equipment": OWNER_LLM,
    "steps": OWNER_LLM,
    "expected": OWNER_LLM,
}

#: Relative path inside a bundle directory.  Public so bundle builders can
#: reference it without hard-coding the string.
OWNERSHIP_REL_PATH = "rules/section_ownership.json"

#: Relative path inside a PROJECT directory of the project-config ownership
#: OVERRIDE.  Section ownership is configuration: this file survives bundle
#: reinstall/upgrade and participates in template export, unlike the bundle's
#: own side-car (the pack's read-only declaration).
CONFIG_OVERRIDE_REL_PATH = "config/section_ownership.json"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionOwnership:
    """Immutable snapshot of which sections belong to which owner.

    Constructed by :func:`resolve`; consumed by prompt builder and
    reconstruction call-site.
    """

    llm_sections: frozenset[str]
    parser_sections: frozenset[str]
    #: Full section universe (llm ∪ parser) in declared order. Default ``()``
    #: so any pre-existing construction without this field still works.
    section_order: tuple[str, ...] = ()

    def owner_of(self, section: str) -> str:
        """Return ``"llm"`` or ``"parser"`` for *section*.

        Raises :exc:`KeyError` for unknown sections so callers surface
        mismatches early rather than silently defaulting.
        """
        s = section.lower()
        if s in self.llm_sections:
            return OWNER_LLM
        if s in self.parser_sections:
            return OWNER_PARSER
        raise KeyError(f"Unknown section: {section!r}")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"SectionOwnership("
            f"llm={sorted(self.llm_sections)}, "
            f"parser={sorted(self.parser_sections)})"
        )


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def resolve(
    ownership_map: Mapping[str, str],
    task_override: Iterable[str] | None = None,
) -> SectionOwnership:
    """Derive a :class:`SectionOwnership` from *ownership_map* or *task_override*.

    The section **universe** (which sections exist + their order) comes from the
    keys of *ownership_map*, NOT from :data:`CANONICAL_SECTIONS`.  This lets a
    bundle with a different/renamed ruleset declare its own sections.

    Args:
        ownership_map: Flat ``{section: owner}`` dict (e.g. from bundle JSON
            or :data:`DEFAULT_OWNERSHIP`).  Its keys (normalised, deduped) are
            the section universe and define the emit order; unrecognised owner
            values are treated as ``"parser"`` with a warning.
        task_override: When provided, this is the **authoritative** set of
            LLM-owned sections — everything else in the universe becomes
            parser-owned.  The override still partitions over the bundle's
            universe (the map's keys): a section named in the override but
            absent from the universe is dropped with a debug log.

    Returns:
        Frozen :class:`SectionOwnership` with both partition sets populated and
        ``section_order`` set to the declared universe.  The union always
        equals the bundle's declared universe (the map's keys), not
        :data:`CANONICAL_SECTIONS`.
    """
    # Build the ordered universe from the map keys (dedup, preserve first-seen).
    order: list[str] = []
    seen: set[str] = set()
    for raw_key in ownership_map:
        section = str(raw_key).strip().lower()
        if section in seen:
            continue
        seen.add(section)
        order.append(section)
    universe = set(order)

    if task_override is not None:
        override = _normalise_set(task_override, context="task_override")
        llm_sections = {s for s in override if s in universe}
        dropped = override - llm_sections
        if dropped:
            log.debug(
                "section_ownership.resolve: override sections not in bundle "
                "universe, dropped: %s",
                sorted(dropped),
            )
        return SectionOwnership(
            llm_sections=frozenset(llm_sections),
            parser_sections=frozenset(universe - llm_sections),
            section_order=tuple(order),
        )

    llm_sections: set[str] = set()
    parser_sections: set[str] = set()

    for raw_key, raw_owner in ownership_map.items():
        section = str(raw_key).strip().lower()
        owner = str(raw_owner).strip().lower()
        if owner == OWNER_LLM:
            llm_sections.add(section)
        elif owner == OWNER_PARSER:
            parser_sections.add(section)
        else:
            log.warning(
                "section_ownership.resolve: unrecognised owner %r for section %r; "
                "defaulting to %r",
                raw_owner, section, OWNER_PARSER,
            )
            parser_sections.add(section)

    return SectionOwnership(
        llm_sections=frozenset(llm_sections),
        parser_sections=frozenset(parser_sections),
        section_order=tuple(order),
    )


# ---------------------------------------------------------------------------
# Thin IO loader
# ---------------------------------------------------------------------------


def _load_flat_map(path: Path) -> dict[str, str] | None:
    """Read one ownership JSON file into a flat ``{section: owner}`` dict.

    Shared body of :func:`load_bundle_ownership` and
    :func:`load_config_override` — same validation/flattening for both
    tiers. Returns the flat dict (which **may be ``{}``**) or ``None``
    when the file is absent, unreadable, not valid JSON, not a JSON
    object, or contains non-string keys / invalid value shapes.
    """
    if not path.exists():
        log.debug("section_ownership: %s not found; will use default", path)
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.info(
            "section_ownership: could not parse %s (%s); will use default", path, exc
        )
        return None
    if not isinstance(raw, dict):
        log.info(
            "section_ownership: expected a JSON object in %s, got %s; will use default",
            path, type(raw).__name__,
        )
        return None
    # Two entry shapes are accepted:
    #   "meta": "parser"                          (legacy)
    #   "meta": {"owner": "parser", ...}          (object-shape; Commit B)
    # Object-shape may also carry required/required_keys consumed by the
    # parser side (section_requirements) — they're ignored here so the
    # flat owner-only return signature stays the same for legacy callers.
    flat: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            log.info(
                "section_ownership: non-string key in %s; will use default", path,
            )
            return None
        if isinstance(v, str):
            flat[k] = v
        elif isinstance(v, dict) and isinstance(v.get("owner"), str):
            flat[k] = v["owner"]
        else:
            log.info(
                "section_ownership: section %r has invalid value shape in %s; "
                "will use default", k, path,
            )
            return None
    return flat


def load_bundle_ownership(bundle_dir: Path) -> dict[str, str] | None:
    """Read ``<bundle_dir>/rules/section_ownership.json``.

    Returns the parsed flat ``{str: str}`` dict — which **may be ``{}``** for
    a valid but empty file — or ``None`` when the file is absent, unreadable,
    not valid JSON, not a JSON object, or contains non-string keys/values.
    ``None`` signals *unknown* so the caller can fall back to
    :data:`DEFAULT_OWNERSHIP`; an empty dict ``{}`` means the bundle
    explicitly owns zero LLM sections.
    """
    return _load_flat_map(bundle_dir / OWNERSHIP_REL_PATH)


def load_config_override(project_root: Path) -> dict[str, str] | None:
    """Read ``<project_root>/config/section_ownership.json`` — the
    project-config ownership OVERRIDE tier.

    Section ownership is configuration consumed by the editor/parser:
    the override survives bundle reinstall/upgrade and rides template
    export. Same validation/flattening as :func:`load_bundle_ownership`;
    ``None`` = no override (fall through to the bundle side-car / wheel
    default).
    """
    return _load_flat_map(project_root / CONFIG_OVERRIDE_REL_PATH)


def supports_section_ownership(bundle_dir: Path) -> bool:
    """True when *bundle_dir* declares a section-ownership map.

    A bundle that ships ``rules/section_ownership.json`` supports per-task
    section control; the GUI calls this with ``<project>/bundle`` to show/hide
    the control.
    """
    return load_bundle_ownership(bundle_dir) is not None


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def for_bundle(
    bundle_dir: Path,
    task_override: Iterable[str] | None = None,
) -> SectionOwnership:
    """Convenience: load the bundle's ownership map then resolve.

    Falls back to :data:`DEFAULT_OWNERSHIP` when the bundle has no map.
    """
    loaded = load_bundle_ownership(bundle_dir)
    effective_map = loaded if loaded is not None else DEFAULT_OWNERSHIP
    return resolve(effective_map, task_override)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_set(sections: Iterable[str], context: str) -> frozenset[str]:
    """Lowercase + strip each entry.

    Filtering to the bundle's universe is the caller's job (:func:`resolve`
    intersects with the declared section keys); *context* is kept for symmetry
    with future per-call logging.
    """
    return frozenset(str(raw).strip().lower() for raw in sections)
