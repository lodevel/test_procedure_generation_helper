"""Phase 5.1: direct wheel-side parser/validator helpers — subprocess edition.

Each entry point dispatches to the **project's** venv Python rather than the
editor's own interpreter, so the wheel imported is the one bundled with the
active project (which may differ from the GUI's installed wheel).

When ``project_root=None`` (editor startup, no project loaded) every entry
point falls back to in-process import — the original behaviour — so
standalone-editor mode stays usable.

Mechanism
---------
- Sibling script ``_pack_parsers_subprocess.py`` is a standalone CLI runner.
  It reads a JSON op-spec from stdin and writes a JSON result to stdout.
- The subprocess is invoked by FILE PATH (not ``-m``)::

      <project>/.venv/Scripts/python.exe <abs>/_pack_parsers_subprocess.py

  Invoking via ``-m`` would trigger ``workflow_editor/__init__.py`` which
  imports PySide6 (the editor GUI). The project venv has the bundle wheels
  but NOT PySide6 — and shouldn't, since it is for test execution only.
  Running the script by path keeps ``__name__ == "__main__"`` and skips the
  package init entirely.

Fallback (project_root=None)
-----------------------------
``is_available()`` and every other entry point call the in-process helpers
(``_inproc_*``) directly.  This is the original behaviour; it is kept because
the GUI may probe ``is_available()`` during startup before any project is open.

is_available() caching
-----------------------
``is_available(project_root)`` is cheap but may be polled on every repaint;
it is cached per ``(project_root, venv_python_mtime)``.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import sys
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from . import section_ownership

log = logging.getLogger(__name__)

# Wheel version we expect (used in error messages only; the subprocess
# runner imports the real wheel).
_REQUIRED_WHEEL = "rules_packager_base.rules.v2_0_2.parser"

# Default timeouts (seconds).
_TIMEOUT_IS_AVAILABLE = 5
_TIMEOUT_DEFAULT = 30
# Live single-op remote execution: connect + one driver call + close. Generous
# margin over a device's own VISA/serial timeout so a slow bench doesn't trip
# the transport before the instrument answers.
_TIMEOUT_REMOTE_EXEC = 45

# env-var overrides
_ENV_TIMEOUT = "TPG_PACK_PARSER_TIMEOUT"


class ParserUnavailable(RuntimeError):
    """Raised when the deterministic parser/codegen path can't run."""


class ParseFailure(Exception):
    """Raised by :func:`parse_text` when the wheel's parser produced
    error-severity findings. Duck-typed against the legacy ParseError
    (carries ``.code``, ``.line``, ``.column``, ``.fix_hint``,
    ``.findings``)."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _venv_python(project_root: Path) -> Path:
    """Resolve the project venv Python path (Windows or POSIX)."""
    if sys.platform == "win32" or os.name == "nt":
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"


def _timeout() -> int:
    try:
        return int(os.environ.get(_ENV_TIMEOUT, _TIMEOUT_DEFAULT))
    except (ValueError, TypeError):
        return _TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# is_available() cache
# ---------------------------------------------------------------------------

# Cache: (project_root_str, mtime_ns) → (available, reason)
_IS_AVAILABLE_CACHE: dict[tuple[str, int], tuple[bool, str]] = {}


def _cached_is_available(project_python: Path) -> tuple[bool, str]:
    try:
        mtime = project_python.stat().st_mtime_ns
    except OSError:
        return False, f"Project venv Python not found: {project_python}"
    key = (str(project_python), mtime)
    if key in _IS_AVAILABLE_CACHE:
        return _IS_AVAILABLE_CACHE[key]
    result = _subprocess_call(
        project_python,
        {"op": "is_available"},
        timeout=_TIMEOUT_IS_AVAILABLE,
    )
    if result.get("ok") and "available" in result:
        out = (result["available"], result.get("reason", ""))
    else:
        out = (False, result.get("error", "subprocess error"))
    _IS_AVAILABLE_CACHE[key] = out
    return out


# ---------------------------------------------------------------------------
# Subprocess dispatcher
# ---------------------------------------------------------------------------


def _subprocess_call(
    project_python: Path,
    spec: dict[str, Any],
    timeout: int,
    runner_name: str = "_pack_parsers_subprocess.py",
) -> dict[str, Any]:
    """Invoke the subprocess runner and return its parsed JSON output.

    Never raises — returns ``{"ok": False, "error": ..., "kind": ...}`` on
    any transport-level failure so callers can map it uniformly.

    The runner is invoked as a standalone script file, NOT via ``-m``.
    Running it as a module would trigger ``workflow_editor/__init__.py``
    which imports PySide6 (the editor GUI). The project's venv has the
    bundle wheels but NOT PySide6 — and shouldn't, since the project
    venv is for test execution, not the editor UI. Invoking by path
    sets ``__name__ == "__main__"`` and skips the package init.

    ``runner_name`` selects the sibling runner script — the default parser
    runner, or ``_execute_op_subprocess.py`` for live single-op execution.
    """
    runner_path = Path(__file__).resolve().parent / runner_name

    # Tell the child where the project bundle lives so it can populate the pack
    # registry before running the op. project_python is <project>/.venv/<bin>/python,
    # so the project root is parents[2] and the bundle is <project>/bundle.
    # Injected once here rather than in every op-spec builder.
    spec = {**spec, "_bundle_dir": str(project_python.parents[2] / "bundle")}

    try:
        proc = subprocess.run(
            [str(project_python), str(runner_path)],
            input=json.dumps(spec),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "kind": "ParserUnavailable",
            "error": f"Subprocess timed out after {timeout}s (op={spec.get('op')})",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": "ParserUnavailable",
            "error": f"Failed to launch subprocess: {exc}",
        }

    if proc.returncode != 0:
        stderr = proc.stderr.strip() if proc.stderr else "(no stderr)"
        return {
            "ok": False,
            "kind": "ParserUnavailable",
            "error": f"Subprocess exited {proc.returncode}: {stderr}",
        }

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "kind": "Other",
            "error": f"Subprocess stdout is not valid JSON: {exc}\nstdout={proc.stdout!r}",
        }


def _resolve_project_python(project_root: Path) -> Path:
    """Return the project venv Python path; raise ParserUnavailable if absent."""
    py = _venv_python(project_root)
    if not py.exists():
        raise ParserUnavailable(
            f"No project venv at {py}. "
            f"Reinstall the project bundle to create the venv. "
            f"The LLM fallback remains available."
        )
    return py


# ---------------------------------------------------------------------------
# In-process fallback helpers (project_root=None path)
# ---------------------------------------------------------------------------


_REGISTRY_POPULATED = False


def _ensure_inproc_registry_populated() -> None:
    """Populate the process-global pack registry once for the in-process path.

    Post pack-pluggable cutover the base engine resolves pack verbs/schema/
    equipment from a process-global registry; nothing parses psu/eload/scope/
    controller until it is populated. Source the bundle dir from
    ``TPG_BUNDLE_DEFAULTS_PATH`` (a defaults.json file → its parent dir). No-op
    when unset (no-project editor mode), when the wheel predates the registry
    (old project), or on any load error — the parser then degrades to the
    monolithic-free 'unknown pack' findings rather than crashing the editor.
    """
    global _REGISTRY_POPULATED
    if _REGISTRY_POPULATED:
        return
    _REGISTRY_POPULATED = True  # set before work so a failure isn't retried per call
    defaults_path = os.environ.get("TPG_BUNDLE_DEFAULTS_PATH")
    if not defaults_path:
        return
    try:
        from rules_packager_base.rules.v2_0_2.parser._pack_registry import (
            load_packs_into_registry,
        )
        load_packs_into_registry(Path(defaults_path).parent)
    except Exception as exc:  # noqa: BLE001 — old wheel / bad bundle: degrade, don't crash
        log.debug("in-proc pack registry population skipped: %s", exc)


def _inproc_import_wheel():
    try:
        import rules_packager_base.rules.v2_0_2.parser as _parser
        _ensure_inproc_registry_populated()
        return _parser
    except ImportError as exc:
        raise ParserUnavailable(
            f"{_REQUIRED_WHEEL} is not importable: {exc}. "
            f"Reinstall the rules_packager_base wheel (>= 2.0.1) into "
            f"the venv running the workflow editor. The LLM fallback "
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
            f"(>= 2.0.1) into the venv running the workflow editor. The "
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
    given, invokes the project venv Python as a subprocess; result is cached
    per ``(venv_python_path, mtime)``.
    """
    if project_root is None:
        try:
            _inproc_import_wheel()
            return True, "deterministic path active"
        except ParserUnavailable as exc:
            return False, str(exc)

    try:
        project_python = _resolve_project_python(project_root)
    except ParserUnavailable as exc:
        return False, str(exc)
    return _cached_is_available(project_python)


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

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {"op": "parse_text", "text": text, "project_root": str(project_root)},
        timeout=_timeout(),
    )

    if not result.get("ok"):
        kind = result.get("kind", "Other")
        if kind == "ParseFailure":
            return _reconstruct_parse_failure(result)
        raise ParserUnavailable(result.get("error", "subprocess error"))

    return result["json"], result.get("warnings", [])


def render_text(
    procedure_json: dict[str, Any],
    project_root: Optional[Path] = None,
) -> str:
    """Emit canonical-text procedure from the v2.0.1 JSON shape."""
    if project_root is None:
        return _inproc_render_text(procedure_json)

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {"op": "render_text", "procedure_json": procedure_json},
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
    return result["text"]


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

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {"op": "renumber_steps", "text": text},
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
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
    bundle's ``equipment_profiles`` via the ``TPG_BUNDLE_DEFAULTS_PATH``
    env var (same path the workflows editor uses for its bundle
    defaults), filtered to ``equipment_type == "controller"`` and shimmed
    to the wheel's record shape (``pattern`` <- ``id_pattern``), so the
    GUI button can call this with just ``text`` and still get controller
    subtype inference.

    Raises :class:`ParserUnavailable` if the wheel isn't importable.
    """
    if controller_profiles is None:
        controller_profiles = [
            {**p, "pattern": p.get("id_pattern")}
            for p in _load_bundle_equipment_profiles()
            if p.get("equipment_type") == "controller"
        ]

    if project_root is None:
        return _inproc_sync_equipment(text, controller_profiles)

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {
            "op": "sync_equipment",
            "text": text,
            "controller_profiles": controller_profiles,
        },
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
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

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {"op": "sync_meta", "text": text},
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
    return result["text"], result.get("warnings", [])


def supports_sync_meta(project_root: Optional[Path] = None) -> bool:
    """Whether the ACTIVE wheel provides ``sync_meta_text``.

    The "Sync Meta" button is gated on this: the feature is delivered by the
    package, so if the installed wheel predates it (imports fine but lacks the
    function) the button is hidden rather than shown-then-erroring on click.
    Mirrors :func:`is_available`'s in-proc/subprocess split. Best-effort — any
    probe failure returns ``False`` (hide the button)."""
    if project_root is None:
        try:
            return hasattr(_inproc_import_wheel(), "sync_meta_text")
        except ParserUnavailable:
            return False
    try:
        project_python = _resolve_project_python(project_root)
    except ParserUnavailable:
        return False
    result = _subprocess_call(
        project_python,
        {"op": "supports", "attr": "sync_meta_text"},
        timeout=_timeout(),
    )
    return bool(result.get("ok") and result.get("supported"))


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


def _load_procedure_document(procedure_path: Path | str) -> dict[str, Any]:
    """json.load the procedure document for the in-process bridge branch.
    Raises :class:`ParserUnavailable` (the bridges' uniform failure type) with
    a clear message when the file is missing/unreadable/not JSON."""
    path = Path(procedure_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParserUnavailable(f"cannot read procedure file {str(path)!r}: {exc}")


def build_manual_run(
    procedure_path: Path | str,
    measurements: Optional[dict] = None,
    controls: Optional[dict] = None,
    project_root: Optional[Path] = None,
    operator_verdicts: Optional[dict] = None,
) -> "ManualRunResult":
    """Materialize a guided-manual run plan from the procedure.json at
    ``procedure_path`` — the wheel-side StepperSession surfaced to the GUI's
    guided-manual execution mode.

    ``measurements`` maps each resolved ``{N}`` ref (int) to the operator's
    entered value (drives @IF branch selection and live PASS/FAIL verdicts);
    ``controls`` maps a loop-sentinel ``node_path`` to its decision. Both are
    optional — omit for the static (no-input) plan. Returns a
    :class:`ManualRunResult` (the GUI never imports the wheel's dataclasses).
    Raises :class:`ParserUnavailable` if the wheel isn't importable or the
    file is unreadable.
    """
    procedure_path = str(Path(procedure_path).resolve())
    if project_root is None:
        return _inproc_build_manual_run(
            _load_procedure_document(procedure_path),
            measurements, controls, operator_verdicts)

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {
            "op": "build_manual_run",
            "procedure_path": procedure_path,
            "measurements": measurements,
            "controls": controls,
            "operator_verdicts": operator_verdicts,
        },
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
    return ManualRunResult.from_dict(result)


def supports_build_manual_run(project_root: Optional[Path] = None) -> bool:
    """Whether the ACTIVE wheel provides ``build_manual_run`` — gates the
    "Guided Manual" entry point so an older wheel (imports fine but predates the
    feature) hides the action rather than erroring on launch. Mirrors
    :func:`supports_sync_meta`; any probe failure returns ``False``."""
    if project_root is None:
        try:
            return hasattr(_inproc_import_wheel(), "build_manual_run")
        except ParserUnavailable:
            return False
    try:
        project_python = _resolve_project_python(project_root)
    except ParserUnavailable:
        return False
    result = _subprocess_call(
        project_python,
        {"op": "supports", "attr": "build_manual_run"},
        timeout=_timeout(),
    )
    return bool(result.get("ok") and result.get("supported"))


# Every wheel function the guided-manual mode needs to operate end-to-end
# (walk the plan AND Finish/Save). The launch button gates on ALL of them so it
# never enables a mode the operator couldn't complete.
_GUIDED_MANUAL_FNS = ("build_manual_run", "build_manual_result")


def supports_guided_manual(project_root: Optional[Path] = None) -> bool:
    """Whether the wheel exposes EVERY function guided-manual needs
    (:data:`_GUIDED_MANUAL_FNS`). Mirrors :func:`supports_sync_meta`'s in-proc /
    subprocess split; any probe failure returns ``False``."""
    if project_root is None:
        try:
            wheel = _inproc_import_wheel()
            return all(hasattr(wheel, fn) for fn in _GUIDED_MANUAL_FNS)
        except ParserUnavailable:
            return False
    try:
        project_python = _resolve_project_python(project_root)
    except ParserUnavailable:
        return False
    result = _subprocess_call(
        project_python,
        {"op": "supports", "attrs": list(_GUIDED_MANUAL_FNS)},
        timeout=_timeout(),
    )
    return bool(result.get("ok") and result.get("supported"))


def build_manual_result(
    procedure_path: Path | str,
    measurements: Optional[dict] = None,
    controls: Optional[dict] = None,
    project_root: Optional[Path] = None,
    operator_verdicts: Optional[dict] = None,
) -> dict:
    """Evaluate a guided-manual run (procedure.json at ``procedure_path``) into
    a ``results.json``-compatible dict — byte-compatible with the automated
    test.py output (same Result schema, same build_criteria/eval_verdicts), so
    the GUI's status badges + report generation consume it unchanged. Dispatches
    into the PROJECT venv (subprocess) when ``project_root`` is set — the same
    wheel that runs the test. Raises :class:`ParserUnavailable` if the wheel
    isn't importable or the file is unreadable."""
    procedure_path = str(Path(procedure_path).resolve())
    if project_root is None:
        return _inproc_build_manual_result(
            _load_procedure_document(procedure_path),
            measurements, controls, operator_verdicts)

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {
            "op": "build_manual_result",
            "procedure_path": procedure_path,
            "measurements": measurements,
            "controls": controls,
            "operator_verdicts": operator_verdicts,
        },
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
    out = result.get("result")
    return out if isinstance(out, dict) else {}


def _load_bundle_equipment_profiles() -> list[dict]:
    """Read ``equipment_profiles`` from the bundle's defaults.json.

    The parent app exposes the bundle path via ``TPG_BUNDLE_DEFAULTS_PATH``;
    same convention as ``TaskConfigManager._load_pack_workflow_defaults``.
    Returns ``[]`` when the env var is unset or the file is missing /
    malformed (no bundle context). A readable bundle defaults.json
    WITHOUT the ``equipment_profiles`` key is a pre-bundle/2 artifact —
    that is a hard error (D4), never a silent ``[]``.
    """
    import json
    import os
    path_str = os.environ.get("TPG_BUNDLE_DEFAULTS_PATH")
    if not path_str:
        return []
    p = Path(path_str)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    profiles = data.get("equipment_profiles")
    if not isinstance(profiles, list):
        raise ParserUnavailable(
            "bundle predates equipment_profiles (bundle/1) — rebuild the bundle"
        )
    return [p for p in profiles if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# Per-capability gating (parse / emit / schema / codegen) — read from the
# bundle's pack_dispatch.capabilities, so the editor can enable/disable actions
# per the equipment a procedure actually uses, without importing any pack.
# ---------------------------------------------------------------------------

# All four concerns a pack may provide for an equipment type.
CAPABILITIES = ("parse", "emit", "schema", "codegen")

_EQUIP_LINE_RE = None  # lazily compiled


def _bundle_defaults_path(project_root: Optional[Path]) -> Optional[Path]:
    """Resolve the bundle defaults.json: a project's ``<root>/bundle/defaults.json``
    when a project is open, else ``TPG_BUNDLE_DEFAULTS_PATH`` (editor mode)."""
    if project_root is not None:
        p = Path(project_root) / "bundle" / "defaults.json"
        return p if p.exists() else None
    env = os.environ.get("TPG_BUNDLE_DEFAULTS_PATH")
    return Path(env) if env else None


def pack_capabilities(project_root: Optional[Path] = None) -> dict[str, dict]:
    """Return the bundle's per-equipment-type capability map
    ``{etype: {parse, schema, emit, codegen, pack}}``. Empty dict when no bundle
    / no capabilities block (pre-capability bundle) — callers treat 'absent' as
    'unknown', see :func:`can`."""
    path = _bundle_defaults_path(project_root)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    caps = (data.get("pack_dispatch") or {}).get("capabilities")
    return caps if isinstance(caps, dict) else {}


def bench_fields(project_root: Optional[Path] = None) -> dict[str, list]:
    """Return the bundle's per-equipment-type config-field descriptor
    ``{etype: [{name, type, label, default, required?, choices?}]}`` — the single
    source the equipment-config editor renders from (packs declare it in
    rules_index.json; codegen derives its ``{field: default}`` from the same
    data). Empty dict when no bundle / pre-bench_fields bundle (caller falls back
    to the project-config regex)."""
    path = _bundle_defaults_path(project_root)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    bf = (data.get("pack_dispatch") or {}).get("bench_fields")
    return bf if isinstance(bf, dict) else {}


def bench_constant_names(etype: str, eid: str, project_root: Optional[Path] = None) -> list[str]:
    """Generated-code constant names for one equipment instance, e.g.
    ``["PSU1_VISA", "PSU1_REMOTE", ...]`` — derived from the declared bench_fields
    as ``<sanitized-upper eid>_<FIELD.upper()>``. Lets the GUI identify
    operator-editable bench constants from pack declarations instead of regex.
    Empty when the etype has no declared fields."""
    import re as _re
    prefix = _re.sub(r"\W", "_", str(eid)).upper()
    return [f"{prefix}_{f['name'].upper()}" for f in bench_fields(project_root).get(etype, [])
            if isinstance(f, dict) and isinstance(f.get("name"), str)]


class RemoteSession:
    """Persistent daemon channel for ONE guided-manual run.

    Holds a long-lived subprocess (``_execute_op_subprocess.py`` in the project
    venv) that keeps ``per_session`` devices OPEN across steps, so a held PSU
    output survives between ops — the whole reason the daemon exists.  The dialog
    creates ONE per run, routes ⚡/auto-run through it, and sends ``shutdown``
    (or, on a wedge, ``kill`` + a fresh :func:`safe_off_remote`) at the end.

    Thread-safe: a lock serialises requests (one in-flight at a time, matching
    the daemon's single-threaded loop).  Requests NEVER raise — they return
    ``{"ok": False, "kind": "SessionDead"|"SessionTimeout"|"ParserUnavailable",
    "error": ..., "stderr": [...]}`` so the dialog can trigger recovery (fresh
    safe-off) and surface "session lost".  ``_bundle_dir`` and
    ``procedure_path`` are injected per frame; the daemon json.loads the
    document ONCE per session from that path.
    """

    def __init__(self, procedure_path: Path | str, project_root: Path) -> None:
        self._procedure_path = str(Path(procedure_path).resolve())
        self._project_root = Path(project_root)
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_tail: deque = deque(maxlen=80)
        self._lock = threading.Lock()
        self._dead = False
        self._bundle_dir: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------
    def _start(self) -> None:
        py = _resolve_project_python(self._project_root)   # may raise ParserUnavailable
        self._bundle_dir = str(py.parents[2] / "bundle")
        runner = Path(__file__).resolve().parent / "_execute_op_subprocess.py"
        self._proc = subprocess.Popen(
            [str(py), str(runner)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._dead = False
        self._stderr_tail.clear()
        threading.Thread(target=self._drain_stderr, args=(self._proc,),
                         daemon=True).start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stderr.readline, ""):
                self._stderr_tail.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            pass

    def _alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and not self._dead)

    def _terminate(self) -> None:
        """Terminate AND reap the daemon, so its serial/VISA handle is released
        before any fresh recovery daemon re-opens the same port."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)   # reap -> device handle freed
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _dead_resp(self, kind: str, error: str) -> dict[str, Any]:
        # dead == True is a HARD guarantee the proc is GONE (and its device handle
        # released) — so _session()'s lazy-recreate and the dialog's recovery
        # safe-off never open a 2nd process on a device the old daemon still holds.
        self._dead = True
        self._terminate()
        return {"ok": False, "kind": kind, "error": error,
                "stderr": list(self._stderr_tail)}

    # -- transport ---------------------------------------------------------
    def _request(self, frame: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            if self._dead:
                return {"ok": False, "kind": "SessionDead",
                        "error": "remote session already dead",
                        "stderr": list(self._stderr_tail)}
            try:
                if not self._alive():
                    self._start()
            except ParserUnavailable as exc:
                return self._dead_resp("ParserUnavailable", str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._dead_resp("SessionDead", f"failed to start daemon: {exc}")
            frame = {**frame, "_bundle_dir": self._bundle_dir,
                     "procedure_path": self._procedure_path}
            try:
                self._proc.stdin.write(json.dumps(frame) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                return self._dead_resp("SessionDead", f"write failed: {exc}")
            box: dict = {}

            def _read() -> None:
                try:
                    box["line"] = self._proc.stdout.readline()
                except Exception as exc:  # noqa: BLE001
                    box["err"] = exc

            reader = threading.Thread(target=_read, daemon=True)
            reader.start()
            reader.join(timeout)
            if reader.is_alive():                     # wedged synchronous VISA read
                return self._dead_resp(               # _dead_resp terminates+reaps
                    "SessionTimeout", f"daemon unresponsive after {timeout}s")
            if "err" in box or not box.get("line"):
                rc = self._proc.poll()
                return self._dead_resp("SessionDead", f"daemon closed pipe (exit={rc})")
            try:
                return json.loads(box["line"])
            except json.JSONDecodeError as exc:
                return self._dead_resp("SessionDead", f"corrupt frame: {exc}")

    # -- API ---------------------------------------------------------------
    def exec_ops(self, ops: list[dict[str, Any]], bench_map: dict[str, Any],
                 timeout: Optional[int] = None) -> dict[str, Any]:
        """Run a contiguous batch; per_session devices stay HELD afterward."""
        return self._request(
            {"cmd": "exec_ops", "ops": ops, "bench_map": bench_map},
            timeout or (_TIMEOUT_REMOTE_EXEC + 5 * len(ops)))

    def exec_op(self, target_op: dict[str, Any], bench: dict[str, Any],
                timeout: Optional[int] = None) -> dict[str, Any]:
        """⚡ single op — batch of one, unwrapped to the flat shape the dialog
        consumes: ``{"ok", "has_value", "value", "unit", "ref", "log"}``."""
        device = target_op.get("device")
        res = self.exec_ops([{"node_path": "", "op": target_op}],
                            {device: bench}, timeout)
        if not res.get("ok"):
            return res
        r0 = (res.get("results") or [{}])[0]
        if not r0.get("ok"):
            out = {"ok": False, "kind": "ExecError",
                   "error": r0.get("error", "remote execution failed"),
                   "log": res.get("log", [])}
            if res.get("unsafe"):
                out["unsafe"] = res["unsafe"]
            return out
        out = {"ok": True, "has_value": r0.get("has_value", False),
               "value": r0.get("value"), "unit": r0.get("unit", ""),
               "ref": r0.get("ref"), "log": res.get("log", [])}
        if res.get("unsafe"):
            out["unsafe"] = res["unsafe"]
        return out

    def safe_off(self, targets: list[dict[str, Any]], bench_map: dict[str, Any],
                 timeout: Optional[int] = None) -> dict[str, Any]:
        """Turn the given outputs OFF (ELOAD before PSU), keeping devices held."""
        return self._request(
            {"cmd": "safe_off", "safe_off": targets, "bench_map": bench_map},
            timeout or _TIMEOUT_REMOTE_EXEC)

    def exec_raw_command(self, device: str, etype: str, subtype: str, text: str,
                         bench: dict[str, Any],
                         timeout: Optional[int] = None) -> dict[str, Any]:
        """Send ONE raw command line to ``device`` over the live session and
        return ``{"ok", "response", "log"}`` (or a typed failure).  ``etype`` is
        the DECLARED equipment type + ``subtype`` (the daemon resolves the pack
        namespace from them — controller->fncore).  Reuses the device if a step
        already armed it (held), else opens a transient link — the same connect
        semantics as a per_step op.  ``"?"`` in ``text`` makes it a query (reply
        returned); otherwise it is a write (empty reply)."""
        return self._request(
            {"cmd": "raw", "device": device, "etype": etype, "subtype": subtype,
             "text": text, "bench_map": {device: bench}},
            timeout or _TIMEOUT_REMOTE_EXEC)

    def ping(self, timeout: float = 5) -> dict[str, Any]:
        return self._request({"cmd": "ping"}, timeout)

    def shutdown(self, timeout: Optional[int] = None) -> dict[str, Any]:
        """Safe-off all held + close (unlock) + exit. Idempotent / safe to call
        on an already-dead session."""
        if self._proc is None or self._dead:
            return {"ok": True, "log": []}
        res = self._request(
            {"cmd": "shutdown"},
            timeout or (_TIMEOUT_REMOTE_EXEC + 30))
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._terminate()
        self._dead = True
        return res

    def kill(self) -> None:
        """Hard-stop a wedged daemon (the parent's last-resort)."""
        self._dead = True
        self._terminate()

    @property
    def dead(self) -> bool:
        return self._dead


def safe_off_remote(
    targets: list[dict[str, Any]],
    procedure_path: Path | str,
    bench_map: dict[str, dict[str, Any]],
    project_root: Path,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """One-shot RECOVERY safe-off: spawn a FRESH daemon, drive the given devices'
    outputs OFF (ELOAD before PSU), then shut it down (close + unlock).  Used by
    the dialog when the live :class:`RemoteSession` died/wedged, and on Stop /
    window-close as a standalone.  ``targets`` = ``[{"device","etype",
    "channels":[...]}]``.  Never raises — returns ``{"ok": True, "log": [...],
    "unsafe": [...]?}`` or ``{"ok": False, "kind", "error"}``.
    """
    sess = RemoteSession(procedure_path, project_root)
    try:
        res = sess.safe_off(targets, bench_map, timeout)
        sess.shutdown()              # close + unlock the just-opened devices
        return res
    finally:
        sess.kill()


def equipment_types_in(procedure_text: str) -> list[str]:
    """Extract the declared equipment types from a procedure's ``## Equipment``
    block (lines ``<ID> : <type> [params]``). Order-preserving, de-duplicated.
    Used to decide which packs an action needs."""
    global _EQUIP_LINE_RE
    if _EQUIP_LINE_RE is None:
        import re
        _EQUIP_LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]*\s*:\s*(\S+)")
    types: list[str] = []
    in_block = False
    for raw in (procedure_text or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_block = line[3:].strip().lower() == "equipment"
            continue
        if in_block:
            m = _EQUIP_LINE_RE.match(line)
            if m and m.group(1) not in types:
                types.append(m.group(1))
    return types


# Equipment types the base engine handles natively (no pack needed).
_BASE_NATIVE_TYPES = {"dmm"}


def can(
    capability: str,
    procedure_text: str,
    project_root: Optional[Path] = None,
) -> tuple[bool, list[tuple[str, str]]]:
    """Whether *capability* is available for EVERY equipment type the procedure
    uses. Returns ``(ok, missing)`` where ``missing`` is a list of
    ``(equipment_type, pack_or_reason)`` pairs lacking it.

    Rules:
    - Base-native types (dmm) always satisfy every capability.
    - When the bundle declares NO capabilities block (pre-capability bundle),
      assume everything is available (back-compat — preserves today's behavior).
    - An equipment type absent from the capabilities map → no pack provides it
      → missing as ``(etype, "no pack")``.
    - A type present but with the capability flag false → missing as
      ``(etype, pack_name)``.
    """
    return can_for_types(capability, equipment_types_in(procedure_text), project_root)


def can_for_types(
    capability: str,
    equipment_types,
    project_root: Optional[Path] = None,
) -> tuple[bool, list[tuple[str, str]]]:
    """Like :func:`can` but takes equipment types directly (e.g. from a JSON
    procedure's ``equipment[*].type``) instead of parsing canonical text."""
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}; expected one of {CAPABILITIES}")
    caps = pack_capabilities(project_root)
    if not caps:
        return True, []  # pre-capability bundle: don't gate
    missing: list[tuple[str, str]] = []
    for etype in equipment_types:
        if etype in _BASE_NATIVE_TYPES:
            continue
        entry = caps.get(etype)
        if entry is None:
            missing.append((etype, "no pack"))
        elif not entry.get(capability, False):
            missing.append((etype, entry.get("pack", "?")))
    return (not missing), missing


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

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {"op": "generate_code", "procedure": procedure},
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
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
    """Run the full deterministic validation pipeline.

    Returns a ``_ValidateReport`` duck-typed against the legacy
    ProcedureTextRenderer.validate() shape. Raises :class:`ParserUnavailable`
    if the wheel isn't importable.
    """
    if project_root is None:
        return _inproc_validate(
            text=text,
            json_obj=json_obj,
            mode=mode,
            original_text=original_text,
            check_names=check_names,
        )

    project_python = _resolve_project_python(project_root)
    spec: dict[str, Any] = {"op": "validate", "mode": mode, "check_names": check_names}
    if text is not None:
        spec["text"] = text
    if json_obj is not None:
        spec["json_obj"] = json_obj
    if original_text is not None:
        spec["original_text"] = original_text

    result = _subprocess_call(project_python, spec, timeout=_timeout())
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))

    return _ValidateReport(_reconstruct_issues_from_dicts(result.get("findings", [])))


def get_section_ownership(
    project_root: Optional[Path] = None,
) -> dict[str, str]:
    """Return the bundle's declared default section→owner map (``parser``/``llm``).

    The editable ``<project_root>/bundle/rules/section_ownership.json`` side-car
    is authoritative when present; the wheel's baked-in map is the FALLBACK.

    - ``project_root`` given: read the side-car first
      (:func:`section_ownership.load_bundle_ownership`). A loaded dict (including
      an explicit ``{}`` "LLM owns nothing") is returned as-is. Only when the
      side-car is absent or invalid (``None``) do we fall through to the wheel
      subprocess, which raises :class:`ParserUnavailable` on failure.
    - ``project_root`` is None (editor / no project): the in-process wheel
      default, unchanged.
    """
    if project_root is None:
        return _inproc_import_wheel().section_ownership()

    loaded = section_ownership.load_bundle_ownership(project_root / "bundle")
    if loaded is not None:
        return loaded

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {"op": "section_ownership"},
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
    return result["ownership"]


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

    project_python = _resolve_project_python(project_root)
    result = _subprocess_call(
        project_python,
        {
            "op": "reconstruct",
            "fragment": fragment,
            "prior": prior,
            "owned_sections": (
                sorted(owned_sections) if owned_sections is not None else None
            ),
        },
        timeout=_timeout(),
    )
    if not result.get("ok"):
        raise ParserUnavailable(result.get("error", "subprocess error"))
    return _ReconstructReport.from_dict(result)


# ---------------------------------------------------------------------------
# In-process implementations (used when project_root=None)
# ---------------------------------------------------------------------------


def _inproc_parse_text(
    text: str,
    *,
    project_root: Optional[Path] = None,
) -> tuple[dict[str, Any], list[str]]:
    wheel = _inproc_import_wheel()
    # Older wheels (< the Commit C bump) don't accept project_root yet.
    # Fall back to the no-kwarg call so the test harness can keep using
    # whatever wheel happens to be installed.
    try:
        result = wheel.parse(text, project_root=project_root)
    except TypeError:
        result = wheel.parse(text)
    if not result.success or result.json is None:
        errors = result.errors if hasattr(result, "errors") else [
            f for f in result.findings if f.severity == "error"
        ]
        raise _make_parse_failure(errors, result.findings)
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


def _inproc_build_manual_run(
    procedure_json: dict[str, Any],
    measurements: Optional[dict],
    controls: Optional[dict],
    operator_verdicts: Optional[dict] = None,
) -> "ManualRunResult":
    wheel = _inproc_import_wheel()
    if not hasattr(wheel, "build_manual_run"):
        # Old wheel imports fine but predates the feature — surface the declared
        # exception (matches the subprocess path) instead of a raw AttributeError.
        raise ParserUnavailable(
            f"{_REQUIRED_WHEEL} predates build_manual_run; reinstall a newer "
            f"rules_packager_base wheel. The LLM fallback remains available."
        )
    # Normalize None -> {} to match the subprocess worker exactly (both paths
    # then hand the wheel the same shapes).
    run = wheel.build_manual_run(
        procedure_json, measurements or {}, controls or {}, operator_verdicts or {})
    return ManualRunResult.from_wheel(run)


def _inproc_build_manual_result(
    procedure_json: dict[str, Any],
    measurements: Optional[dict],
    controls: Optional[dict],
    operator_verdicts: Optional[dict] = None,
) -> dict:
    wheel = _inproc_import_wheel()
    if not hasattr(wheel, "build_manual_result"):
        raise ParserUnavailable(
            f"{_REQUIRED_WHEEL} predates build_manual_result; reinstall a newer "
            f"rules_packager_base wheel. The LLM fallback remains available."
        )
    return wheel.build_manual_result(
        procedure_json, measurements or {}, controls or {}, operator_verdicts or {})


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


# ---------------------------------------------------------------------------
# Reconstruction helpers: JSON dicts → editor-facing types
# ---------------------------------------------------------------------------


def _reconstruct_parse_failure(result: dict[str, Any]) -> "never":
    """Raise ParseFailure reconstructed from a subprocess error dict."""
    findings_dicts = result.get("findings", [])
    issues = [_IssueFromDict(d) for d in findings_dicts]
    exc = ParseFailure(result.get("error", "Parsing failed."))
    exc.code = result.get("code", "PARSE_ERROR")
    exc.line = result.get("line")
    exc.column = result.get("column")
    exc.fix_hint = result.get("fix_hint", "")
    exc.findings = issues
    raise exc


def _reconstruct_issues_from_dicts(findings: list[dict]) -> list["_IssueFromDict"]:
    return [_IssueFromDict(d) for d in findings]


class _IssueFromDict:
    """Editor-facing _Issue reconstructed from a subprocess JSON dict."""

    def __init__(self, d: dict) -> None:
        self.code = d.get("code", "") or ""
        self.message = d.get("message", "") or ""
        self.severity = d.get("severity", "error") or "error"
        line = d.get("line", 0) or 0
        col = d.get("col", 0) or 0
        loc: dict[str, Any] = {}
        if line:
            loc["line"] = line
        if col:
            loc["column"] = col
        self.location = loc
        self.fix_hint = d.get("fix_hint", "") or ""
        self.fixable_by = d.get("fixable_by", "either") or "either"


class _Issue:
    """Editor-facing adapter over a wheel ``Finding`` (in-process path).

    Mirrors the legacy canonical.py wrapper's _Issue shape so
    ``validator_dispatch._issue_from_validator`` can consume it.
    """

    def __init__(self, finding: Any) -> None:
        self.code = getattr(finding, "code", "") or ""
        self.message = getattr(finding, "message", "") or ""
        self.severity = getattr(finding, "severity", "error") or "error"
        line = getattr(finding, "line", 0)
        col = getattr(finding, "col", 0)
        loc: dict[str, Any] = {}
        if line:
            loc["line"] = line
        if col:
            loc["column"] = col
        self.location = loc
        self.fix_hint = getattr(finding, "fix_hint", "") or ""
        self.fixable_by = getattr(finding, "fixable_by", "either") or "either"


class _ValidateReport:
    """Duck-typed report shape consumed by ``validator_dispatch``."""

    def __init__(self, findings: list[Any]) -> None:
        # Accept both Finding objects (in-process) and _IssueFromDict (subprocess).
        self._issues = [
            f if hasattr(f, "location") else _Issue(f) for f in findings
        ]
        self.ok = not any(i.severity == "error" for i in self._issues)
        self.errors = [i for i in self._issues if i.severity == "error"]
        self.warnings = [i for i in self._issues if i.severity == "warning"]


class _ReconstructReport:
    """Duck-typed report for :func:`reconstruct_text`.

    Mirrors ``_ValidateReport``'s finding normalization while also carrying
    the reconstructed ``.text``/``.json`` payload. Build via the
    :meth:`from_parse_result` (in-process) / :meth:`from_dict` (subprocess)
    classmethods rather than ``__init__`` directly.
    """

    def __init__(
        self,
        success: bool,
        text: Optional[str],
        json: Optional[dict],
        findings: list[Any],
    ) -> None:
        self.success = success
        self.text = text
        self.json = json
        self.findings = findings
        self.ok = success
        self.errors = [i for i in self.findings if i.severity == "error"]
        self.warnings = [i for i in self.findings if i.severity == "warning"]

    @classmethod
    def from_parse_result(cls, pr: Any) -> "_ReconstructReport":
        """Wrap an in-process wheel ``ParseResult``."""
        return cls(
            success=pr.success,
            text=pr.text,
            json=pr.json,
            findings=[_Issue(f) for f in pr.findings],
        )

    @classmethod
    def from_dict(cls, d: dict) -> "_ReconstructReport":
        """Wrap a subprocess JSON result dict."""
        return cls(
            success=d["success"],
            text=d.get("text"),
            json=d.get("json"),
            findings=_reconstruct_issues_from_dicts(d.get("findings", [])),
        )


# ---------------------------------------------------------------------------
# Guided-manual run plan: editor-facing views over the wheel's ManualRun /
# StepDescriptor, so the GUI consumes one stable shape across both the
# in-process (dataclass) and subprocess (JSON) paths.
#
# These two are PUBLIC (unlike the private _Report/_Issue siblings above)
# because they are the GUI-facing RETURN type of build_manual_run: the modal
# binds their field names directly, so a leading underscore would leak into
# call sites. Collections are tuples so the frozen views are genuinely immutable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManualStep:
    """One step in a guided-manual run — a faithful mirror of the wheel's
    ``StepDescriptor`` (the source of truth for these fields; a CI test asserts
    field-name parity). Built only via :meth:`from_dict`, which ignores unknown
    keys so a newer wheel adding a field never breaks construction here."""

    node_path: str = ""
    n: int = 0
    op: str = ""
    text: str = ""
    is_binding: bool = False
    kind: str = ""
    ref: Optional[int] = None
    quantity: str = ""
    target: str = ""
    expected_text: str = ""
    verdict: str = ""               # PASS | FAIL | SKIP | "" (pending)
    media: tuple[dict, ...] = ()    # raw step["media"] entries, pass-through
    status: str = "pending"         # pending | answered | skipped
    control_deciding: bool = False  # blocks forward skip/jump until answered
    iteration: int = 0
    index: int = 0
    # op with per-iteration substitution applied (mirrors StepDescriptor.resolved_op);
    # excluded from eq/hash so a dict field keeps the frozen step hashable.
    resolved_op: Optional[dict] = field(default=None, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> "ManualStep":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        if isinstance(data.get("media"), list):  # JSON/asdict give a list; freeze it
            data["media"] = tuple(data["media"])
        return cls(**data)


@dataclass(frozen=True)
class ManualRunResult:
    """Editor-facing guided-manual run plan returned by :func:`build_manual_run`.

    ``steps`` is the on-path plan the operator walks; ``skipped`` holds off-path
    bindings (retained as ``SKIP``); ``aborted`` is set when an ``@ABORT``
    truncated the plan. Build via :meth:`from_wheel` (in-process) or
    :meth:`from_dict` (subprocess) — both funnel through :meth:`ManualStep.from_dict`."""

    test_name: str = ""
    steps: tuple[ManualStep, ...] = ()       # on-path plan
    skipped: tuple[ManualStep, ...] = ()     # off-path bindings (retained as SKIP)
    aborted: bool = False

    @classmethod
    def from_wheel(cls, run: Any) -> "ManualRunResult":
        """Wrap an in-process wheel ``ManualRun`` (StepDescriptor dataclasses)."""
        return cls(
            test_name=run.test_name,
            steps=tuple(ManualStep.from_dict(asdict(s)) for s in run.steps),
            skipped=tuple(ManualStep.from_dict(asdict(s)) for s in run.skipped),
            aborted=bool(run.aborted),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "ManualRunResult":
        """Wrap a subprocess JSON result dict."""
        return cls(
            test_name=d.get("test_name", ""),
            steps=tuple(ManualStep.from_dict(s) for s in d.get("steps", [])),
            skipped=tuple(ManualStep.from_dict(s) for s in d.get("skipped", [])),
            aborted=bool(d.get("aborted", False)),
        )


# ---------------------------------------------------------------------------
# Internal: ParseFailure construction (in-process path)
# ---------------------------------------------------------------------------


def _make_parse_failure(errors: list[Any], findings: list[Any]) -> "ParseFailure":
    primary = errors[0] if errors else None
    code = getattr(primary, "code", "PARSE_ERROR") if primary else "PARSE_ERROR"
    line = getattr(primary, "line", None) if primary else None
    col = getattr(primary, "col", None) if primary else None
    fix_hint = getattr(primary, "fix_hint", "") if primary else ""
    msg = str(primary) if primary else "Parsing failed."
    exc = ParseFailure(msg)
    exc.code = code
    exc.line = line
    exc.column = col
    exc.fix_hint = fix_hint
    exc.findings = [_Issue(f) for f in findings]
    return exc


def _format_finding(finding: Any) -> str:
    code = getattr(finding, "code", "?")
    line = getattr(finding, "line", 0)
    msg = getattr(finding, "message", str(finding))
    return f"line {line} [{code}] {msg}"


# ---------------------------------------------------------------------------
# Step-text extraction from canonical procedure_text.md (no wheel needed)
# ---------------------------------------------------------------------------


import re as _step_re

_STEP_TEXTS_LINE_RE = _step_re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", _step_re.MULTILINE)
_STEPS_SECTION_RE = _step_re.compile(
    r"^##\s+Steps\s*$(.*?)(?=^##\s+|\Z)",
    _step_re.MULTILINE | _step_re.DOTALL,
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
