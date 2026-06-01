"""Subprocess CLI runner for rules_packager_base wheel operations.

Invoked as ``python -m workflow_editor.llm._pack_parsers_subprocess`` with
PYTHONPATH set to the editor package's parent dir.  Reads a JSON op spec
from stdin, executes the requested operation via direct in-process import
of ``rules_packager_base``, writes a JSON result to stdout.

Wire format (stdin → stdout):

  Input:
    {"op": "<op_name>", ...op-specific fields...}

  Output (success):
    {"ok": true, ...op-specific result fields...}

  Output (error):
    {"ok": false, "error": "<message>", "kind": "<ParserUnavailable|ParseFailure|Other>"}

Ops:
  is_available  → {"ok": true, "available": bool, "reason": str}
  parse_text    → {"ok": true, "json": dict, "warnings": [str]}
                  or {"ok": false, "kind": "ParseFailure", "error": str,
                      "code": str, "line": int|null, "column": int|null,
                      "fix_hint": str, "findings": [{...}]}
  render_text   → {"ok": true, "text": str}
  generate_code → {"ok": true, "code": str, "warnings": [str]}
  validate      → {"ok": true, "findings": [{code, message, severity, line,
                                              col, fix_hint, fixable_by}]}
  section_ownership → {"ok": true, "ownership": {section: owner}}
  reconstruct   → {"ok": true, "success": bool, "text": str|null,
                   "json": dict|null, "findings": [{...}]}
"""
from __future__ import annotations

import json
import sys
import traceback
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
                f"Reinstall the rules_packager_base wheel (>= 2.0.1)."
            ),
        }


def _op_parse_text(spec: dict) -> dict:
    text = spec["text"]
    raw_root = spec.get("project_root")
    wheel = _import_wheel()
    # Older wheels (< the Commit C bump) don't accept project_root yet.
    # Fall back to the no-kwarg call so a stale project venv stays usable.
    if raw_root is not None:
        from pathlib import Path as _Path
        try:
            result = wheel.parse(text, project_root=_Path(raw_root))
        except TypeError:
            result = wheel.parse(text)
    else:
        result = wheel.parse(text)
    if not result.success or result.json is None:
        errors = result.errors if hasattr(result, "errors") else [
            f for f in result.findings if f.severity == "error"
        ]
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


def _op_render_text(spec: dict) -> dict:
    procedure_json = spec["procedure_json"]
    wheel = _import_wheel()
    text = wheel.render(procedure_json)
    return {"ok": True, "text": text}


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


_OPS = {
    "is_available": _op_is_available,
    "parse_text": _op_parse_text,
    "render_text": _op_render_text,
    "renumber_steps": _op_renumber_steps,
    "sync_equipment": _op_sync_equipment,
    "generate_code": _op_generate_code,
    "validate": _op_validate,
    "section_ownership": _op_section_ownership,
    "reconstruct": _op_reconstruct,
}


def main() -> None:
    try:
        raw = sys.stdin.read()
        spec = json.loads(raw)
        # Populate the process-global pack registry from the project bundle
        # before running the op (fresh process per op). Best-effort: an old
        # wheel without _pack_registry, or a bundle without pack_dispatch,
        # degrades to 'unknown pack' findings rather than failing the runner.
        _bundle_dir = spec.get("_bundle_dir")
        if _bundle_dir:
            try:
                from rules_packager_base.rules.v2_0_2.parser._pack_registry import (
                    load_packs_into_registry,
                )
                load_packs_into_registry(Path(_bundle_dir))
            except Exception:  # noqa: BLE001
                pass
        op = spec.get("op")
        if op not in _OPS:
            result = {
                "ok": False,
                "kind": "Other",
                "error": f"Unknown op: {op!r}. Valid ops: {sorted(_OPS)}",
            }
        else:
            result = _OPS[op](spec)
    except ImportError as exc:
        result = {
            "ok": False,
            "kind": "ParserUnavailable",
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "kind": "Other",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
