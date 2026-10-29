"""Phase 5.1: direct wheel-side parser/validator helpers, run in the project's engine.

Each entry point dispatches to the **project's** venv Python rather than the
editor's own interpreter, so the wheel imported is the one bundled with the
active project (which may differ from the GUI's installed wheel). The app's
own venv has no wheel: with no project open (``project_root=None``) there is
no engine, so every entry point raises :class:`ParserUnavailable`
(:func:`is_available` says so).

Mechanism
---------
An op is a frame to the project's TOOLS engine (:mod:`procedo.engine_client.engine`): one persistent
``engine_client/_execute_op_subprocess.py --role tools`` per project, run by FILE PATH in
the machine-local venv (``project_venv``), serving the op table
``engine_client/_engine_ops.py``. By path, not ``-m``: the project venv does not
have the ``procedo`` package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from procedo.core import display_snapshot, section_ownership
from procedo.engine_client.engine_replies import (
    _ReconstructReport,
    _reconstruct_issues_from_dicts,
    _reconstruct_parse_failure,
    _ValidateReport,
)
from procedo.engine_client.engine_session import ParserUnavailable, _timeout

from . import bundle_capabilities

# Default timeouts (seconds).
_TIMEOUT_IS_AVAILABLE = 5

#: The ops whose answer only gates or labels the window (Bench's display reads): asked under
#: the bundle as last read. Every other op's answer is written: asked fresh.
_DISPLAY_OPS = frozenset({"is_available", "section_ownership"})

#: Why no op runs with no project open: the wheel runs only in a project's engine.
NO_PROJECT = "No project is open."


# ---------------------------------------------------------------------------
# Engine dispatcher
# ---------------------------------------------------------------------------


def _engine_call(
    project_root: Optional[Path], spec: dict[str, Any], timeout: Optional[float] = None
) -> dict[str, Any]:
    """``spec`` answered by the project's TOOLS engine: the op's result dict, or
    ``{"ok": False, "kind", "error"}``. Raises ParserUnavailable with no project
    open or without a venv. A display read (:data:`_DISPLAY_OPS`: what a button or a list
    shows) is asked under the bundle as last read — on a share, the fresh look is a dozen
    round trips before every ask."""
    if project_root is None:
        raise ParserUnavailable(NO_PROJECT)
    from procedo.engine_client import engine  # the project's engine processes

    # Studio writes what its ops answer (a procedure, a test.py): never from a superseded
    # engine — a sync pull replacing wheels sends it no event.
    fresh = spec["op"] not in _DISPLAY_OPS
    if not fresh and display_snapshot.environment_missing(project_root):
        raise ParserUnavailable("the project has no environment on this PC yet")
    return engine.tools_op(project_root, spec, timeout or _timeout(), fresh=fresh)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def is_available(
    project_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Probe whether the project's engine imports the wheel cleanly. Returns
    ``(available, reason)``; never available with no project open."""
    try:
        result = _engine_call(project_root, {"op": "is_available"}, _TIMEOUT_IS_AVAILABLE)
    except ParserUnavailable as exc:
        return False, str(exc)
    if result.get("ok") and "available" in result:
        return result["available"], result.get("reason", "")
    return False, result.get("error", "engine error")


def parse_text(
    text: str,
    project_root: Optional[Path] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Parse canonical-text procedure into the v2.0.1 JSON shape.

    Returns ``(procedure_json, warnings)``. Raises :class:`ParserUnavailable`
    or :class:`ParseFailure` on failure.
    """
    result = _engine_call(
        project_root, {"op": "parse_text", "text": text, "project_root": str(project_root)}
    )

    if not result.get("ok"):
        kind = result.get("kind", "Other")
        if kind == "ParseFailure":
            return _reconstruct_parse_failure(result)
        raise ParserUnavailable(result.get("error", "engine error"))

    return result["json"], result.get("warnings", [])


def render_text(
    procedure_json: dict[str, Any],
    project_root: Optional[Path] = None,
) -> str:
    """Emit canonical-text procedure from the v2.0.1 JSON shape."""
    result = _engine_call(project_root, {"op": "render_text", "procedure_json": procedure_json})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["text"]


def refresh_media(
    procedure_json: dict[str, Any],
    project_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Recompute the derived ``media`` of a procedure JSON with the pack's one
    catalogue. ``parse()`` already does this for its own output, so this is a
    no-op on a parser-produced JSON and the refresh a hand- or LLM-edited
    JSON needs. Returns the refreshed dict (a fresh one). Raises
    :class:`ParserUnavailable` when the wheel cannot run."""
    result = _engine_call(project_root, {"op": "refresh_media", "procedure_json": procedure_json})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["json"]


def renumber_steps_text(
    text: str,
    project_root: Optional[Path] = None,
) -> str:
    """Rewrite the leading ``N.`` prefix on every step line inside the
    ``## Steps`` block to be sequential 1..N. Pure text transform — no
    parse, no validation. Wraps the bundle's
    ``parser.renumber_steps``. Returns the new text (or the original
    when nothing changed). Raises :class:`ParserUnavailable` if the
    wheel isn't importable."""
    result = _engine_call(project_root, {"op": "renumber_steps", "text": text})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["text"]


def sync_equipment_from_steps(
    text: str,
    project_root: Optional[Path] = None,
    controller_profiles: Optional[list[dict]] = None,
) -> tuple[str, list[str]]:
    """Synthesize / merge a ``## Equipment`` block from the device
    references in ``## Steps``. Returns ``(new_text, warnings)`` where
    warnings is a flat list of formatted strings (typically one per
    measure-only channel).

    When ``controller_profiles`` is None, falls back to reading the
    bundle's ``equipment_profiles`` from the project's bundle
    defaults.json (project-root-first, ``PROCEDO_BUNDLE_DEFAULTS_PATH`` env
    var as legacy fallback — same resolution the workflows editor uses
    for its bundle defaults), filtered to
    ``equipment_type == "controller"`` and shimmed
    to the wheel's record shape (``pattern`` <- ``id_pattern``), so the
    GUI button can call this with just ``text`` and still get controller
    subtype inference.

    Raises :class:`ParserUnavailable` if the wheel isn't importable.
    """
    if controller_profiles is None:
        controller_profiles = [
            {**p, "pattern": p.get("id_pattern")}
            for p in bundle_capabilities.load_bundle_equipment_profiles(project_root)
            if p.get("equipment_type") == "controller"
        ]

    result = _engine_call(
        project_root,
        {
            "op": "sync_equipment",
            "text": text,
            "controller_profiles": controller_profiles,
        },
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["text"], result.get("warnings", [])


def sync_meta_text(
    text: str,
    project_root: Optional[Path] = None,
) -> tuple[str, list[str]]:
    """Regenerate the ``## Meta`` block (format_version / board + the per-pack
    ``rules_pack`` / ``labscpi_pack`` / ``fncore_pack`` version pins) from the
    active bundle, with no LLM — the deterministic counterpart to the LLM. Board
    / format_version / extra keys are preserved. Returns ``(new_text, warnings)``.
    Raises :class:`ParserUnavailable` if the wheel isn't importable."""
    result = _engine_call(project_root, {"op": "sync_meta", "text": text})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["text"], result.get("warnings", [])


def sync_meta_json(
    procedure_json: dict[str, Any],
    project_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Return the parser-owned ``meta`` block for ``procedure_json``, re-derived
    from the ACTIVE BUNDLE.

    JSON-side counterpart to :func:`sync_meta_text`. Composes the existing
    bridges render_text (JSON->text) -> sync_meta_text (rewrites ``## Meta`` from
    the active bundle, PRESERVING operator-owned keys like ``board``) ->
    parse_text (text->JSON), so the pin-derivation lives in exactly one place
    (the wheel) rather than being re-implemented in the consumer. Raises
    :class:`ParserUnavailable` / :class:`ParseFailure` on any failure; callers
    treat that as 'leave meta as the LLM authored it'.
    """
    rendered = render_text(procedure_json, project_root=project_root)
    synced, _ = sync_meta_text(rendered, project_root=project_root)
    reparsed, _ = parse_text(synced, project_root=project_root)
    meta = reparsed.get("meta")
    return meta if isinstance(meta, dict) else {}


def generate_code(
    procedure: dict[str, Any],
    project_root: Optional[Path] = None,
) -> tuple[str, list[str]]:
    """Generate test.py source from a procedure JSON dict.

    Returns ``(code, warnings)``. Raises :class:`ParserUnavailable` if the
    wheel isn't importable.
    """
    result = _engine_call(project_root, {"op": "generate_code", "procedure": procedure})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["code"], result.get("warnings", [])


def validate(
    *,
    text: Optional[str] = None,
    json_obj: Optional[dict[str, Any]] = None,
    mode: str = "all",
    original_text: Optional[str] = None,
    check_names: bool = True,
    project_root: Optional[Path] = None,
) -> "_ValidateReport":
    """Run the full deterministic validation pipeline (with ``project_root``, in its engine,
    the Call steps checked against its ``functions/`` too). Returns a ``_ValidateReport``
    duck-typed against the legacy ProcedureTextRenderer.validate() shape; raises
    :class:`ParserUnavailable` if the wheel isn't importable."""
    spec: dict[str, Any] = {
        "op": "validate",
        "mode": mode,
        "check_names": check_names,
        "project": str(project_root),
    }
    if text is not None:
        spec["text"] = text
    if json_obj is not None:
        spec["json_obj"] = json_obj
    if original_text is not None:
        spec["original_text"] = original_text

    result = _engine_call(project_root, spec)
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))

    return _ValidateReport(_reconstruct_issues_from_dicts(result.get("findings", [])))


def get_section_ownership(
    project_root: Optional[Path] = None,
) -> dict[str, str]:
    """Return the EFFECTIVE section→owner map (``parser``/``llm``).

    Resolution chain (section ownership is project CONFIGURATION, not a
    bundle property):

    1. ``<project_root>/config/section_ownership.json`` — project-config
       OVERRIDE (survives bundle reinstall/upgrade, rides template export);
       layered per-section over the base below.
    2. ``<project_root>/bundle/rules/section_ownership.json`` — the pack's
       declaration copied at build (:func:`section_ownership.load_bundle_ownership`).
       A loaded dict (including an explicit ``{}`` "LLM owns nothing") is
       the base as-is.
    3. Wheel default (subprocess) only when the side-car is absent or
       invalid; raises :class:`ParserUnavailable` on failure.

    ``project_root`` is None (no project open): raises :class:`ParserUnavailable`.
    """
    if project_root is None:
        raise ParserUnavailable(NO_PROJECT)
    override = section_ownership.load_config_override(project_root)
    loaded = section_ownership.load_bundle_ownership(project_root / "bundle")
    if loaded is None:
        result = _engine_call(project_root, {"op": "section_ownership"})
        if not result.get("ok"):
            raise ParserUnavailable(result.get("error", "engine error"))
        loaded = dict(result["ownership"])
    # Override layers over whichever base won (bundle side-car OR wheel
    # default); it never shrinks the section universe on its own.
    return {**loaded, **override} if override else loaded


def reconstruct_text(
    fragment: str,
    prior: Optional[str] = None,
    owned_sections: Optional[set[str]] = None,
    project_root: Optional[Path] = None,
) -> "_ReconstructReport":
    """Splice an LLM-authored fragment with a prior into a full procedure.

    Returns a ``_ReconstructReport`` duck-typed against ``_ValidateReport``.
    Raises :class:`ParserUnavailable` if the wheel isn't importable.
    """
    result = _engine_call(
        project_root,
        {
            "op": "reconstruct",
            "fragment": fragment,
            "prior": prior,
            "owned_sections": (sorted(owned_sections) if owned_sections is not None else None),
        },
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return _ReconstructReport.from_dict(result)


# ---------------------------------------------------------------------------
# Step-text extraction from canonical procedure_text.md (no wheel needed)
# ---------------------------------------------------------------------------


_STEP_TEXTS_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
_STEPS_SECTION_RE = re.compile(
    r"^##\s+Steps\s*$(.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def step_texts_from_canonical(procedure_text: str) -> dict[int, str]:
    """Return ``{step_number: canonical_line}`` from a procedure_text.md.

    Pure-regex helper — no wheel import. Unchanged from pre-refactor.
    """
    if not procedure_text:
        return {}
    section_match = _STEPS_SECTION_RE.search(procedure_text)
    if section_match is None:
        return {}
    body = section_match.group(1)
    out: dict[int, str] = {}
    for m in _STEP_TEXTS_LINE_RE.finditer(body):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        text = m.group(2).rstrip(".").rstrip()
        out[n] = text
    return out
