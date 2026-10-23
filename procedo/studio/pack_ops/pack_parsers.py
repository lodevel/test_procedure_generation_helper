"""Phase 5.1: direct wheel-side parser/validator helpers, run in the project's engine.

Each entry point dispatches to the **project's** venv Python rather than the
editor's own interpreter, so the wheel imported is the one bundled with the
active project (which may differ from the GUI's installed wheel).

When ``project_root=None`` (editor startup, no project loaded) every entry
point falls back to in-process import — the original behaviour — so the
editor stays usable with no project open.

Mechanism
---------
An op is a frame to the project's TOOLS engine (:mod:`.engine`): one persistent
``llm/_execute_op_subprocess.py --role tools`` per project, run by FILE PATH in
the machine-local venv (``_project_local_state``), serving the op table
``llm/_engine_ops.py``. By path, not ``-m``: ``procedo/studio/__init__.py``
imports PySide6, which the project venv does not have.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from .. import launch_env
from ..llm import section_ownership
from . import bundle_capabilities
from .engine_replies import (
    _make_parse_failure,
    _ReconstructReport,
    _reconstruct_issues_from_dicts,
    _reconstruct_parse_failure,
    _ValidateReport,
)
from .engine_session import ParserUnavailable, _timeout

log = logging.getLogger(__name__)

# Wheel version we expect (used in error messages only).
_REQUIRED_WHEEL = "rules_packager_base.rules.v2_0_2.parser"

# Default timeouts (seconds).
_TIMEOUT_IS_AVAILABLE = 5


# ---------------------------------------------------------------------------
# Engine dispatcher
# ---------------------------------------------------------------------------


def _engine_call(project_root: Path, spec: dict[str, Any],
                 timeout: Optional[float] = None) -> dict[str, Any]:
    """``spec`` answered by the project's TOOLS engine: the op's result dict, or
    ``{"ok": False, "kind", "error"}``. Raises ParserUnavailable without a venv."""
    from . import engine  # the project's engine processes
    return engine.tools_op(project_root, spec, timeout or _timeout())


# ---------------------------------------------------------------------------
# In-process fallback helpers (project_root=None path)
# ---------------------------------------------------------------------------


_REGISTRY_LOAD: str | None = None  # None: not tried; "": loaded (or no bundle); else why it failed


def _ensure_inproc_registry_populated() -> None:
    """Populate the process-global pack registry once for the in-process path.

    Post pack-pluggable cutover the base engine resolves pack verbs/schema/
    equipment from a process-global registry; nothing parses psu/eload/scope/
    controller until it is populated. Source the bundle dir from
    ``PROCEDO_BUNDLE_DEFAULTS_PATH`` (a defaults.json file → its parent dir). No-op
    when unset (no-project editor mode). A failed load is tried once and said
    on every call (``ParserUnavailable``), as the engine's ops say it.
    """
    global _REGISTRY_LOAD
    if _REGISTRY_LOAD is None:
        _REGISTRY_LOAD = ""
        defaults_path = os.environ.get(launch_env.BUNDLE_DEFAULTS_PATH)
        if defaults_path:
            try:
                from rules_packager_base.rules.v2_0_2.parser._pack_registry import (
                    load_packs_into_registry,
                )
                load_packs_into_registry(Path(defaults_path).parent)
            except Exception as exc:  # noqa: BLE001 — said by every call below
                from ..llm._engine_ops import packs_unloadable

                _REGISTRY_LOAD = packs_unloadable(exc)
    if _REGISTRY_LOAD:
        raise ParserUnavailable(_REGISTRY_LOAD)


def _inproc_import_wheel():
    try:
        import rules_packager_base.rules.v2_0_2.parser as _parser
        _ensure_inproc_registry_populated()
        return _parser
    except ImportError as exc:
        raise ParserUnavailable(
            f"{_REQUIRED_WHEEL} is not importable: {exc}. "
            f"Reinstall the rules_packager_base wheel (>= 2.0.4) into "
            f"the venv running Procedo Studio. The LLM fallback "
            f"remains available."
        ) from exc


def _inproc_import_codegen():
    try:
        from rules_packager_base.rules.v2_0_2.parser import codegen as _codegen
        _ensure_inproc_registry_populated()
        return _codegen
    except ImportError as exc:
        raise ParserUnavailable(
            f"rules_packager_base.rules.v2_0_2.parser.codegen is not "
            f"importable: {exc}. Reinstall the rules_packager_base wheel "
            f"(>= 2.0.1) into the venv running Procedo Studio. The "
            f"LLM fallback remains available."
        ) from exc


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def is_available(
    project_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Probe whether the wheel imports cleanly. Returns ``(available, reason)``.

    When ``project_root`` is None (no project open), probes the editor's own
    venv via in-process import (original behaviour). When ``project_root`` is
    given, asks the project's engine.
    """
    if project_root is None:
        try:
            _inproc_import_wheel()
            return True, "deterministic path active"
        except ParserUnavailable as exc:
            return False, str(exc)

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
    if project_root is None:
        return _inproc_parse_text(text)

    result = _engine_call(
        project_root, {"op": "parse_text", "text": text, "project_root": str(project_root)})

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
    if project_root is None:
        return _inproc_render_text(procedure_json)

    result = _engine_call(
        project_root, {"op": "render_text", "procedure_json": procedure_json})
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
    JSON needs. Returns the refreshed dict (a fresh one from the subprocess
    path). Raises :class:`ParserUnavailable` when the wheel cannot run."""
    if project_root is None:
        try:
            from rules_packager_base.rules.v2_0_2 import media as _media
        except ImportError as exc:
            raise ParserUnavailable(f"rules_packager_base media catalogue not importable: {exc}") from exc
        _media.populate_media(procedure_json)
        return procedure_json

    result = _engine_call(
        project_root, {"op": "refresh_media", "procedure_json": procedure_json})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result["json"]


def export_rows(
    procedure_json: dict[str, Any],
    project_root: Optional[Path] = None,
) -> tuple[list[dict], list[dict]]:
    """Document-export rows (steps, expected) computed by the project
    venv's wheel — the only environment guaranteed to have every bundle
    pack importable, so pack ops render their canonical clause instead
    of blank. Raises :class:`ParserUnavailable` when the engine cannot
    answer."""
    if project_root is None:
        raise ParserUnavailable("export_rows requires a project venv")

    result = _engine_call(
        project_root, {"op": "export_rows", "procedure_json": procedure_json})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result.get("steps", []), result.get("expected", [])


def export_rows_batch(
    procedures: list[dict[str, Any]],
    project_root: Path,
) -> list[dict]:
    """Batched :func:`export_rows` — one engine op for a whole export run.
    Returns one ``{"steps": [...], "expected": [...]}`` per input procedure.
    Raises :class:`ParserUnavailable` when the engine cannot answer."""
    result = _engine_call(project_root, {"op": "export_rows", "procedures": procedures})
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return result.get("results", [])


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
    if project_root is None:
        return _inproc_renumber_steps(text)

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
            for p in bundle_capabilities._load_bundle_equipment_profiles(project_root)
            if p.get("equipment_type") == "controller"
        ]

    if project_root is None:
        return _inproc_sync_equipment(text, controller_profiles)

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
    if project_root is None:
        return _inproc_sync_meta(text)

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
    if project_root is None:
        return _inproc_generate_code(procedure)

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
    if project_root is None:
        return _inproc_validate(
            text=text,
            json_obj=json_obj,
            mode=mode,
            original_text=original_text,
            check_names=check_names,
        )

    spec: dict[str, Any] = {"op": "validate", "mode": mode, "check_names": check_names,
                            "project": str(project_root)}
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

    ``project_root`` is None (editor / no project): the in-process wheel
    default, unchanged.
    """
    if project_root is None:
        return _inproc_import_wheel().section_ownership()

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
    if project_root is None:
        pr = _inproc_import_wheel().reconstruct(fragment, prior, owned_sections)
        return _ReconstructReport.from_parse_result(pr)

    result = _engine_call(
        project_root,
        {
            "op": "reconstruct",
            "fragment": fragment,
            "prior": prior,
            "owned_sections": (
                sorted(owned_sections) if owned_sections is not None else None
            ),
        },
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "engine error"))
    return _ReconstructReport.from_dict(result)


# ---------------------------------------------------------------------------
# In-process implementations (used when project_root=None)
# ---------------------------------------------------------------------------


def _inproc_parse_text(
    text: str,
    *,
    project_root: Optional[Path] = None,
) -> tuple[dict[str, Any], list[str]]:
    result = _inproc_import_wheel().parse(text, project_root=project_root)
    if not result.success or result.json is None:
        raise _make_parse_failure(result.errors, result.findings)
    warnings = [
        _format_finding(f) for f in result.findings if f.severity == "warning"
    ]
    return result.json, warnings


def _inproc_render_text(procedure_json: dict[str, Any]) -> str:
    wheel = _inproc_import_wheel()
    return wheel.render(procedure_json)


def _inproc_renumber_steps(text: str) -> str:
    wheel = _inproc_import_wheel()
    return wheel.renumber_steps(text)


def _inproc_sync_equipment(
    text: str, controller_profiles: list[dict],
) -> tuple[str, list[str]]:
    wheel = _inproc_import_wheel()
    new_text, findings = wheel.sync_equipment_text(
        text, controller_profiles=controller_profiles,
    )
    warnings = [_format_finding(f) for f in findings]
    return new_text, warnings


def _inproc_sync_meta(text: str) -> tuple[str, list[str]]:
    wheel = _inproc_import_wheel()
    new_text, findings = wheel.sync_meta_text(text)
    warnings = [_format_finding(f) for f in findings]
    return new_text, warnings


def _inproc_generate_code(procedure: dict[str, Any]) -> tuple[str, list[str]]:
    codegen = _inproc_import_codegen()
    code = codegen.generate(procedure, None)
    return code, []


def _inproc_validate(
    *,
    text: Optional[str],
    json_obj: Optional[dict[str, Any]],
    mode: str,
    original_text: Optional[str],
    check_names: bool,
) -> "_ValidateReport":
    wheel = _inproc_import_wheel()
    findings: list[Any] = []

    parsed_json = json_obj
    if text is not None:
        result = wheel.parse(text)
        findings.extend(result.findings)
        if result.success and result.json is not None:
            parsed_json = parsed_json or result.json

    if parsed_json is not None and mode in ("all", "schema"):
        findings.extend(wheel.validate_schema(parsed_json))

    if text is not None and original_text and check_names:
        findings.extend(wheel.check_name_fidelity(original_text, text))

    return _ValidateReport(findings)


def _format_finding(finding: Any) -> str:
    code = getattr(finding, "code", "?")
    line = getattr(finding, "line", 0)
    msg = getattr(finding, "message", str(finding))
    return f"line {line} [{code}] {msg}"


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
