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

#: Full set of recognised section names (lowercase).
CANONICAL_SECTIONS: frozenset[str] = frozenset(
    {"test_id", "description", "meta", "equipment", "steps", "expected"}
)

#: Baked-in default used when the bundle has no ``section_ownership.json``.
DEFAULT_OWNERSHIP: dict[str, str] = {
    "test_id": OWNER_PARSER,
    "description": OWNER_PARSER,
    "meta": OWNER_PARSER,
    "equipment": OWNER_LLM,
    "steps": OWNER_LLM,
    "expected": OWNER_LLM,
}

#: Relative path inside a bundle directory.  Public so bundle builders can
#: reference it without hard-coding the string.
OWNERSHIP_REL_PATH = "rules/section_ownership.json"


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

    Args:
        ownership_map: Flat ``{section: owner}`` dict (e.g. from bundle JSON
            or :data:`DEFAULT_OWNERSHIP`).  Unknown sections are ignored with
            a debug log; unrecognised owner values are treated as
            ``"parser"`` with a warning.
        task_override: When provided, this is the **authoritative** set of
            LLM-owned sections — everything else becomes parser-owned.
            *ownership_map* is not consulted when *task_override* is given.

    Returns:
        Frozen :class:`SectionOwnership` with both partition sets populated.
        The union always equals :data:`CANONICAL_SECTIONS`.
    """
    if task_override is not None:
        llm_sections = _normalise_set(task_override, context="task_override")
        return SectionOwnership(
            llm_sections=llm_sections,
            parser_sections=CANONICAL_SECTIONS - llm_sections,
        )

    llm_sections: set[str] = set()
    parser_sections: set[str] = set()

    for raw_key, raw_owner in ownership_map.items():
        section = raw_key.strip().lower()
        if section not in CANONICAL_SECTIONS:
            log.debug("section_ownership.resolve: ignoring unknown key %r", raw_key)
            continue
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

    # Sections absent from the map fall back to parser ownership (conservative).
    unmentioned = CANONICAL_SECTIONS - llm_sections - parser_sections
    if unmentioned:
        log.debug(
            "section_ownership.resolve: sections not in map, defaulting to parser: %s",
            sorted(unmentioned),
        )
    parser_sections.update(unmentioned)

    return SectionOwnership(
        llm_sections=frozenset(llm_sections),
        parser_sections=frozenset(parser_sections),
    )


# ---------------------------------------------------------------------------
# Thin IO loader
# ---------------------------------------------------------------------------


def load_bundle_ownership(bundle_dir: Path) -> dict[str, str] | None:
    """Read ``<bundle_dir>/rules/section_ownership.json``.

    Returns the parsed flat ``{str: str}`` dict — which **may be ``{}``** for
    a valid but empty file — or ``None`` when the file is absent, unreadable,
    not valid JSON, not a JSON object, or contains non-string keys/values.
    ``None`` signals *unknown* so the caller can fall back to
    :data:`DEFAULT_OWNERSHIP`; an empty dict ``{}`` means the bundle
    explicitly owns zero LLM sections.
    """
    path = bundle_dir / OWNERSHIP_REL_PATH
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
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()):
        log.info(
            "section_ownership: %s contains non-string keys or values; will use default",
            path,
        )
        return None
    return raw


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
    """Lowercase, strip, and filter to :data:`CANONICAL_SECTIONS`.

    Unknown entries are dropped with a debug log.
    """
    result: set[str] = set()
    for raw in sections:
        s = str(raw).strip().lower()
        if s in CANONICAL_SECTIONS:
            result.add(s)
        else:
            log.debug(
                "section_ownership.%s: ignoring unrecognised section %r", context, raw
            )
    return frozenset(result)
