"""The engine's op table, and which frames each engine role takes.

Loaded by FILE PATH by the persistent daemon ``_execute_op_subprocess.py``
(through ``_sib``) in the project venv — never through the ``procedo.studio``
package, whose init imports PySide6 (absent from the project venv). Each op is
computation on the spec it is given plus the in-memory pack registry: a
document travels IN the spec, never as a path.

Spec: ``{"op": "<op_name>", ...op-specific fields...}``

Result (success): ``{"ok": true, ...op-specific result fields...}``
Result (error):   ``{"ok": false, "error": "<message>",
                     "kind": "<ParserUnavailable|ParseFailure|Other>"}``

Ops:
  is_available  → {"ok": true, "available": bool, "reason": str}
  parse_text    → {"ok": true, "json": dict, "warnings": [str]}
                  or {"ok": false, "kind": "ParseFailure", "error": str,
                      "code": str, "line": int|null, "column": int|null,
                      "fix_hint": str, "findings": [{...}]}
  render_text   → {"ok": true, "text": str}
  refresh_media → {"ok": true, "json": dict}
  export_rows   → {"ok": true, "steps": [rows], "expected": [rows]}
                  (batch: input {"procedures": [...]} → {"ok": true, "results": [...]})
  renumber_steps → {"ok": true, "text": str}
  sync_equipment → {"ok": true, "text": str, "warnings": [str]}
  sync_meta     → {"ok": true, "text": str, "warnings": [str]}
  generate_code → {"ok": true, "code": str, "warnings": []}
  validate      → {"ok": true, "findings": [{code, message, severity, line,
                                              col, fix_hint, fixable_by}]}
  section_ownership → {"ok": true, "ownership": {section: owner}}
  reconstruct   → {"ok": true, "success": bool, "text": str|null,
                   "json": dict|null, "findings": [{...}]}
  build_manual_run → {"ok": true, "test_name": str, "aborted": bool,
                      "steps": [{...StepDescriptor...}],
                      "skipped": [{...StepDescriptor...}]}
                     (input: {"procedure_json": {...}, "measurements",
                      "controls", "operator_verdicts"})
  build_manual_result → {"ok": true, "result": {...results.json schema...}}
                        (input: the same fields)
  display_snapshot → {"ok": true, "wheel": {module, file, version},
                      "tests": [{folder, mtime_ns, size, text_newer,
                                 data: {meta, steps, expected, equipment},
                                 errors: {key: why}}]}
                     (input: {"project", "tests": null | [folder, ...]}; the
                      bundle is the frame's ``_bundle_dir``)
"""
from __future__ import annotations

import importlib
import json
import traceback
from dataclasses import asdict
from pathlib import Path


def _finding_to_dict(f) -> dict:
    return {
        "code": getattr(f, "code", "") or "",
        "message": getattr(f, "message", "") or "",
        "severity": getattr(f, "severity", "error") or "error",
        "line": getattr(f, "line", 0) or 0,
        "col": getattr(f, "col", 0) or 0,
        "fix_hint": getattr(f, "fix_hint", "") or "",
        "fixable_by": getattr(f, "fixable_by", "either") or "either",
    }


def _format_finding(f) -> str:
    code = getattr(f, "code", "?")
    line = getattr(f, "line", 0)
    msg = getattr(f, "message", str(f))
    return f"line {line} [{code}] {msg}"


def _import_wheel():
    import rules_packager_base.rules.v2_0_2.parser as _parser
    return _parser


def _import_codegen():
    from rules_packager_base.rules.v2_0_2.parser import codegen as _codegen
    return _codegen


def _op_is_available(spec: dict) -> dict:
    del spec  # no inputs
    try:
        _import_wheel()
        return {"ok": True, "available": True, "reason": "deterministic path active"}
    except ImportError as exc:
        return {
            "ok": True,
            "available": False,
            "reason": (
                f"rules_packager_base.rules.v2_0_2.parser is not importable: {exc}. "
                f"Reinstall the rules_packager_base wheel (>= 2.0.4)."
            ),
        }


def _op_parse_text(spec: dict) -> dict:
    raw_root = spec.get("project_root")
    result = _import_wheel().parse(
        spec["text"], project_root=Path(raw_root) if raw_root is not None else None)
    if not result.success or result.json is None:
        errors = result.errors
        primary = errors[0] if errors else None
        return {
            "ok": False,
            "kind": "ParseFailure",
            "error": str(primary) if primary else "Parsing failed.",
            "code": getattr(primary, "code", "PARSE_ERROR") if primary else "PARSE_ERROR",
            "line": getattr(primary, "line", None) if primary else None,
            "column": getattr(primary, "col", None) if primary else None,
            "fix_hint": getattr(primary, "fix_hint", "") if primary else "",
            "findings": [_finding_to_dict(f) for f in result.findings],
        }
    warnings = [
        _format_finding(f) for f in result.findings if f.severity == "warning"
    ]
    return {"ok": True, "json": result.json, "warnings": warnings}


def _op_refresh_media(spec: dict) -> dict:
    """Recompute a procedure JSON's derived media with the pack's one
    catalogue (what parse() does for its own output; a no-op there)."""
    from rules_packager_base.rules.v2_0_2 import media
    procedure_json = spec["procedure_json"]
    media.populate_media(procedure_json)
    return {"ok": True, "json": procedure_json}


def _op_render_text(spec: dict) -> dict:
    procedure_json = spec["procedure_json"]
    wheel = _import_wheel()
    text = wheel.render(procedure_json)
    return {"ok": True, "text": text}


def _op_export_rows(spec: dict) -> dict:
    """Full-fidelity document-export rows, computed IN THE PROJECT VENV
    where every bundle pack (labscpi/fncore/...) is importable — the GUI
    process typically lacks them, so pack ops would render blank there."""
    from rules_packager_base.rules.v2_0_2 import extractors as _extractors

    def _ser(rows):
        out = []
        for r in rows:
            media = []
            for m in r.media:
                media.append({
                    "component": m.component,
                    "pin": m.pin,
                    "is_tp": bool(getattr(m, "is_tp", False)),
                    "caption": m.caption,
                    "type": m.type,
                })
            out.append({"idx": r.idx, "text": r.text, "media": media})
        return out

    procedures = spec.get("procedures")
    single = procedures is None
    if single:
        procedures = [spec["procedure_json"]]
    results = [
        {
            "steps": _ser(_extractors.extract_export_steps(pj)),
            "expected": _ser(_extractors.extract_export_expected(pj)),
        }
        for pj in procedures
    ]
    if single:
        return {"ok": True, **results[0]}
    return {"ok": True, "results": results}


def _op_renumber_steps(spec: dict) -> dict:
    text = spec["text"]
    wheel = _import_wheel()
    return {"ok": True, "text": wheel.renumber_steps(text)}


def _op_sync_equipment(spec: dict) -> dict:
    text = spec["text"]
    profiles = spec.get("controller_profiles", [])
    wheel = _import_wheel()
    new_text, findings = wheel.sync_equipment_text(
        text, controller_profiles=profiles,
    )
    # Warnings come back as formatted strings (mirrors _op_parse_text)
    # so the GUI can display them without knowing the Finding shape.
    warnings = [_format_finding(f) for f in findings]
    return {"ok": True, "text": new_text, "warnings": warnings}


def _op_sync_meta(spec: dict) -> dict:
    text = spec["text"]
    wheel = _import_wheel()
    new_text, findings = wheel.sync_meta_text(text)
    warnings = [_format_finding(f) for f in findings]
    return {"ok": True, "text": new_text, "warnings": warnings}


def _op_generate_code(spec: dict) -> dict:
    procedure = spec["procedure"]
    codegen = _import_codegen()
    code = codegen.generate(procedure, None)
    return {"ok": True, "code": code, "warnings": []}


def _op_validate(spec: dict) -> dict:
    text = spec.get("text")
    json_obj = spec.get("json_obj")
    mode = spec.get("mode", "all")
    original_text = spec.get("original_text")
    check_names = spec.get("check_names", True)

    wheel = _import_wheel()
    findings = []

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

    return {"ok": True, "findings": [_finding_to_dict(f) for f in findings]}


def _op_section_ownership(spec: dict) -> dict:
    del spec  # no inputs
    return {"ok": True, "ownership": _import_wheel().section_ownership()}


def _op_reconstruct(spec: dict) -> dict:
    fragment = spec["fragment"]
    prior = spec.get("prior")
    ow = spec.get("owned_sections")
    owned = set(ow) if ow is not None else None
    r = _import_wheel().reconstruct(fragment, prior, owned)
    return {
        "ok": True,
        "success": r.success,
        "text": r.text,
        "json": r.json,
        "findings": [_finding_to_dict(f) for f in r.findings],
    }


def _int_keyed(d) -> dict:
    """Restore integer measurement-ref keys lost crossing the JSON boundary.

    The wheel keys ``measurements`` by the integer ``{N}`` ref, but JSON object
    keys are strings, so a dict sent over stdin arrives string-keyed. Convert
    back to int, leaving any non-integer key untouched (defensive)."""
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


def _document(spec: dict):
    """The procedure document the spec carries (``procedure_json``), never read
    from disk: the caller sends what it shows.

    Returns ``(procedure_json, None)`` or ``(None, error_result)`` — the error
    result is the uniform ``{"ok": false, ...}`` shape so the op can return it
    directly instead of dying with a bare traceback."""
    doc = spec.get("procedure_json")
    if not isinstance(doc, dict):
        return None, {"ok": False, "kind": "Other",
                      "error": "the procedure document is not a JSON object"}
    return doc, None


def _op_build_manual_run(spec: dict) -> dict:
    procedure_json, err = _document(spec)
    if err:
        return err
    measurements = spec.get("measurements") or {}   # row node_path keys, as the loop sentinels'
    controls = spec.get("controls") or {}
    operator_verdicts = _int_keyed(spec.get("operator_verdicts") or {})
    run = _import_wheel().build_manual_run(
        procedure_json, measurements, controls, operator_verdicts)
    return {
        "ok": True,
        "test_name": run.test_name,
        "steps": [asdict(s) for s in run.steps],
        "skipped": [asdict(s) for s in run.skipped],
        "aborted": run.aborted,
    }


def _op_build_manual_result(spec: dict) -> dict:
    procedure_json, err = _document(spec)
    if err:
        return err
    measurements = spec.get("measurements") or {}
    controls = spec.get("controls") or {}
    operator_verdicts = _int_keyed(spec.get("operator_verdicts") or {})
    result = _import_wheel().build_manual_result(
        procedure_json, measurements, controls, operator_verdicts)
    return {"ok": True, "result": result}


# The four DISPLAY domains snapshotted for the app. The validate_* twins
# stay in-process (on-demand banner, not on the paint path).
_DISPLAY_KEYS = ("meta", "steps", "expected", "equipment")


def _load_extractor_decls(bundle_dir: Path) -> dict:
    """Return the bundle's ``defaults.json:extractors`` block ({} when
    missing/malformed) — the same declaration pack_extractors uses."""
    defaults = bundle_dir / "defaults.json"
    try:
        doc = json.loads(defaults.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    extractors = doc.get("extractors")
    return extractors if isinstance(extractors, dict) else {}


def _resolve_extractors(bundle_dir: Path) -> tuple[dict, dict, dict]:
    """Resolve the display extractors exactly the way the bundle declares
    them (``{module, function}`` per key), importing INSIDE this (project)
    venv. Returns ``(callables, errors, wheel_info)``; a missing or
    unimportable key lands in ``errors``, never raises."""
    decls = _load_extractor_decls(bundle_dir)
    fns: dict = {}
    errors: dict = {}
    wheel_info: dict = {"module": "", "file": "", "version": ""}
    for key in _DISPLAY_KEYS:
        entry = decls.get(key)
        if not isinstance(entry, dict):
            errors[key] = f"bundle declares no {key!r} extractor"
            continue
        module_name = entry.get("module")
        function_name = entry.get("function")
        if not isinstance(module_name, str) or not isinstance(function_name, str):
            errors[key] = f"extractor {key!r} lacks module/function strings"
            continue
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — import errors of any shape
            errors[key] = f"module {module_name} not importable: {exc}"
            continue
        fn = mod
        for part in function_name.split("."):
            fn = getattr(fn, part, None)
            if fn is None:
                break
        if not callable(fn):
            errors[key] = f"{module_name}.{function_name} is not callable"
            continue
        fns[key] = fn
        if not wheel_info["module"]:
            wheel_info["module"] = module_name
            wheel_info["file"] = getattr(mod, "__file__", "") or ""
            wheel_info["version"] = _wheel_version(module_name)
    return fns, errors, wheel_info


def _wheel_version(module_name: str) -> str:
    """Best-effort distribution version of the wheel providing
    ``module_name`` (its top-level package)."""
    top = module_name.split(".")[0]
    try:
        from importlib import metadata

        return metadata.version(top)
    except Exception:  # noqa: BLE001
        return ""


def _meta_payload(meta) -> dict:
    """ProcedureMeta -> plain dict carrying EVERY wheel field PLUS the
    wheel-computed ``display_name`` (a property, so asdict misses it)."""
    try:
        d = dict(asdict(meta))
    except TypeError:
        d = {k: v for k, v in vars(meta).items() if not k.startswith("_")}
    display_name = getattr(meta, "display_name", "")
    d["display_name"] = display_name if isinstance(display_name, str) else ""
    return d


def _items_payload(items) -> list:
    """list[dataclass] -> list[dict] (asdict recurses media/channels)."""
    return [asdict(item) for item in items]


def _text_newer(procedure_json: Path) -> bool:
    """Same mtime compare as pack_extractors.canonical_text_is_newer,
    computed in the sweep so the LIST can show the stale badge."""
    text_path = procedure_json.parent / "procedure_text.md"
    try:
        text_mtime = text_path.stat().st_mtime
        json_mtime = procedure_json.stat().st_mtime
    except OSError:
        return False
    return text_mtime > json_mtime


def _extract_test(folder: str, proc_path: Path, fns: dict, resolve_errors: dict) -> dict:
    """Extract one test. Per-key failures become error markers."""
    entry: dict = {"folder": folder, "data": {}, "errors": dict(resolve_errors)}
    # Stat BEFORE reading: if the file is rewritten mid-read, the entry
    # carries the OLD mtime, so the app's freshness check (current stat
    # vs entry mtime) fails safe into the in-process fallback.
    try:
        _st = proc_path.stat()
        entry["mtime_ns"] = _st.st_mtime_ns
        entry["size"] = _st.st_size
    except OSError as exc:
        entry["mtime_ns"] = 0
        for key in _DISPLAY_KEYS:
            entry["errors"][key] = f"cannot stat {proc_path.name}: {exc}"
        entry["text_newer"] = False
        return entry
    entry["text_newer"] = _text_newer(proc_path)
    try:
        doc = json.loads(proc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        for key in _DISPLAY_KEYS:
            entry["errors"][key] = f"cannot read {proc_path.name}: {exc}"
        return entry
    for key, fn in fns.items():
        try:
            if key == "meta":
                entry["data"][key] = _meta_payload(fn(doc))
            else:
                entry["data"][key] = _items_payload(fn(doc))
        except Exception as exc:  # noqa: BLE001 — one bad test/key must not kill the sweep
            entry["errors"][key] = f"{type(exc).__name__}: {exc}"
    return entry


def _op_display_snapshot(spec: dict) -> dict:
    """The main window's display data for the tests of ``spec["project"]``
    (``spec["tests"]``: null = every test folder), extracted by the bundle's
    PINNED wheel with the extractors ``defaults.json`` declares. The pack
    registry is the daemon's, loaded from the frame's ``_bundle_dir``.
    Per-test / per-key failures never fail the sweep: the key is absent from
    ``data`` and explained in ``errors``."""
    project_root = Path(spec["project"])
    bundle_dir = Path(spec.get("_bundle_dir") or (project_root / "bundle"))
    requested = spec.get("tests")

    fns, resolve_errors, wheel_info = _resolve_extractors(bundle_dir)

    tests_dir = project_root / "tests"
    if requested is None:
        try:
            folders = sorted(d.name for d in tests_dir.iterdir() if d.is_dir())
        except OSError as exc:
            return {"ok": False, "kind": "Other", "error": f"cannot list {tests_dir}: {exc}"}
    else:
        folders = [f for f in requested if isinstance(f, str)]

    out = []
    for folder in folders:
        proc_path = tests_dir / folder / "procedure.json"
        if not proc_path.exists():
            # Deleted/never-compiled test: omit it — the store drops
            # requested-but-absent folders from the snapshot.
            continue
        out.append(_extract_test(folder, proc_path, fns, resolve_errors))
    return {"ok": True, "wheel": wheel_info, "tests": out}


_OPS = {
    "is_available": _op_is_available,
    "parse_text": _op_parse_text,
    "render_text": _op_render_text,
    "refresh_media": _op_refresh_media,
    "export_rows": _op_export_rows,
    "renumber_steps": _op_renumber_steps,
    "sync_equipment": _op_sync_equipment,
    "sync_meta": _op_sync_meta,
    "generate_code": _op_generate_code,
    "validate": _op_validate,
    "section_ownership": _op_section_ownership,
    "reconstruct": _op_reconstruct,
    "build_manual_run": _op_build_manual_run,
    "build_manual_result": _op_build_manual_result,
    "display_snapshot": _op_display_snapshot,
}


# The run's own ops: all a RUN engine computes besides its instrument frames.
_RUN_OPS = ("build_manual_run", "build_manual_result")


def refusal(role: str, req: dict) -> dict | None:
    """``Protocol`` for a frame outside ``role``'s job, else None. RUN takes the
    instrument and bench frames and the run's own ops; TOOLS takes every op and
    never a frame that could open a device. Both take ping and shutdown."""
    cmd, op = req.get("cmd"), req.get("op")
    takes = cmd == "op" if role == "tools" else (cmd != "op" or op in _RUN_OPS)
    if takes or cmd in ("ping", "shutdown"):
        return None
    what = f"op {op!r}" if cmd == "op" else f"{cmd!r}"
    return {"ok": False, "kind": "Protocol", "error": f"the {role} engine does not take {what}"}


def run(spec: dict, registry=None) -> dict:
    """Run ``spec["op"]``; never raises. ``registry(spec)``, when given, loads the
    packs first (the daemon's once-per-process load). A load failure does
    not fail the op: it degrades to 'unknown pack' findings. Unknown op →
    ``Other``; ImportError → ``ParserUnavailable``; any other exception →
    ``Other`` with its traceback."""
    try:
        if registry is not None:
            registry(spec)
    except Exception:  # noqa: BLE001
        pass
    op = spec.get("op")
    if op not in _OPS:
        return {
            "ok": False,
            "kind": "Other",
            "error": f"Unknown op: {op!r}. Valid ops: {sorted(_OPS)}",
        }
    try:
        return _OPS[op](spec)
    except ImportError as exc:
        return {
            "ok": False,
            "kind": "ParserUnavailable",
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": "Other",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
