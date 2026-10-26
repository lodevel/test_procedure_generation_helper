"""The engine's op table, and which frames each engine role takes.

Loaded by FILE PATH by the persistent daemon ``_execute_op_subprocess.py``
(through ``_sib``) in the project venv — never through the ``procedo``
package, which is absent from the project venv. Each op is
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
                  (with ``project``: the Call steps' warnings from its
                   ``functions/`` too, read without running them)
  functions_check → {"ok": true, "problems": [str]}  (input: {"project"}):
                  each package the project's functions import and the
                  bundle venv lacks, one line each
  function_signatures → {"ok": true, "functions": {name: str}}  (input:
                  {"project"}): each project function's signature, declared
                  units and first docstring line, read without running it
  section_ownership → {"ok": true, "ownership": {section: owner}}
  reconstruct   → {"ok": true, "success": bool, "text": str|null,
                   "json": dict|null, "findings": [{...}]}
  build_manual_run → {"ok": true, "test_name": str, "aborted": bool,
                      "steps": [{...StepDescriptor...}],
                      "skipped": [{...StepDescriptor...}]}
                     (input: {"procedure_json": {...}, "measurements",
                      "controls", "operator_verdicts"})
                     (a Call row: ``outputs`` [{ref, unit, value_text, note,
                      verdict, check}], ``call`` while it is to be made;
                      each Wait step adds ``wait_s`` or ``wait_refused``;
                      each step what the Values panel shows: ``value_text``,
                      ``value_note``, ``names`` [{name, var, value, origin}];
                      + "channels": {device: [declared power channels]},
                      "constants": [{name, value, origin}])
  build_manual_result → {"ok": true, "result": {...the run record (results.json)...}}
                        (input: the same fields + "failures": {node_path:
                         {"error", "then"}}, the bench ops never redone,
                         "operator_stop": stopped before its end, "session":
                         what the bench kept of the run — started, times,
                         evidence, log, folder — at Finish)
  manual_reading → {"ok": true, "value": any, "refusal": str}
                   (input: {"step": {kind, expected_text, resolved_op}, "text"})
  bench_identity → {"ok": true, "endpoints": [str|null], "channels": [[int]]}  (input: {"benches": [bench]})
  pack_contract → {"ok": true, "packs": {module: {"items": [ContractItem], "error": str|null}}}
                  (input: {"modules": [pack parser module]})
  display_snapshot → {"ok": true, "wheel": {module, file, version},
                      "tests": [{folder, mtime_ns, size, text_newer,
                                 data: {meta, steps, expected, equipment},
                                 errors: {key: why}}]}
                     (input: {"project", "tests": null | [folder, ...]}; the
                      bundle is the frame's ``_bundle_dir``)
  extract       → {"ok": true, "results": [{"data": {key: value}, "errors": {key: why}}]}
                  (input: {"project", "procedures": [{...}, ...], "keys": [key, ...]}:
                   a display key or ``equipment_validate`` / ``steps_validate`` /
                   ``expected_validate``, run by the extractor the bundle declares;
                   one result per procedure, in order)
  convert_record → {"ok": true, "record": {...the run record...}}
                   (input: {"results": an old flat results.json, "manifest", "folder"}:
                    the one-time converter's, one phase "Converted run")
"""

from __future__ import annotations

import importlib
import json
import logging
import traceback
from dataclasses import asdict
from pathlib import Path


# The guided-run and extractor ops live beside this module; imported however this one was loaded: as
# procedo.engine_client's (the app) or by path, its folder on sys.path (the daemon's ``_sib``).
def _sibling(name: str):
    return importlib.import_module(f"{__package__}.{name}" if __package__ else name)


_manual = _sibling("_engine_ops_manual")
_extract = _sibling("_engine_ops_extract")

log = logging.getLogger(__name__)


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
        spec["text"], project_root=Path(raw_root) if raw_root is not None else None
    )
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
    warnings = [_format_finding(f) for f in result.findings if f.severity == "warning"]
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
                media.append(
                    {
                        "component": m.component,
                        "pin": m.pin,
                        "is_tp": bool(getattr(m, "is_tp", False)),
                        "caption": m.caption,
                        "type": m.type,
                    }
                )
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
        text,
        controller_profiles=profiles,
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

    if spec.get("project") and _has_call(parsed_json):
        findings.extend(_call_findings(parsed_json, text, spec))

    return {"ok": True, "findings": [_finding_to_dict(f) for f in findings]}


def _has_call(procedure_json) -> bool:
    """A Call step anywhere in the document: JSON escapes the quotes of a text, so only an
    ``op`` key reads ``"op": "call"``."""
    return procedure_json is not None and '"op": "call"' in json.dumps(procedure_json)


def _call_findings(procedure_json: dict, text, spec: dict) -> list:
    """The Call steps' project warnings; none, logged, when the check fails (an older wheel
    has none): it never costs the procedure its other findings."""
    try:
        from rules_packager_base.rules.v2_0_2.parser import function_catalog

        return function_catalog.call_findings(
            procedure_json,
            Path(spec["project"]) / function_catalog.FUNCTIONS,
            text=text,
            bundle=_bundle_label(spec),
        )
    except Exception:  # noqa: BLE001 — a warning check, never the validation's failure
        log.warning("the Call steps' functions check failed", exc_info=True)
        return []


def _op_functions_check(spec: dict) -> dict:
    """Each package ``spec["project"]``'s functions import that this interpreter (the
    bundle venv) lacks, one line each; no function file is imported."""
    from rules_packager_base.rules.v2_0_2.parser import function_catalog

    problems = function_catalog.import_problems(
        Path(spec["project"]) / function_catalog.FUNCTIONS, _bundle_label(spec)
    )
    return {"ok": True, "problems": problems}


def _op_function_signatures(spec: dict) -> dict:
    """What a Call step's tooltip says of each of ``spec["project"]``'s functions; no function
    file is imported."""
    from rules_packager_base.rules.v2_0_2.parser import function_catalog

    texts = function_catalog.signature_texts(Path(spec["project"]) / function_catalog.FUNCTIONS)
    return {"ok": True, "functions": texts}


def _bundle_label(spec: dict) -> str:
    """``<id> <version>`` of the frame's bundle, as a message names it; "" when its
    bundle.json cannot say."""
    try:
        meta = json.loads(
            (_extract.bundle_dir_of(spec) / "bundle.json").read_text(encoding="utf-8")
        )
        return f"{meta['id']} {meta['version']}"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


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


def _op_bench_identity(spec: dict) -> dict:
    """Each bench dict's (``spec["benches"]``) canonical endpoint — the physical
    instrument it reaches, one key for every spelling of one port (``COM22`` of a
    serial controller, ``ASRL22::INSTR`` of a VISA instrument), None when it
    names no address — and its declared power channels: the wheel's one
    canonicalization (bench_endpoints), never a guess of the app's."""
    from rules_packager_base.bench_endpoints import canonical_endpoint, channels_of

    def endpoint(bench):
        try:
            return canonical_endpoint(bench)
        except ValueError:
            return None

    benches = spec.get("benches") or []
    return {
        "ok": True,
        "endpoints": [endpoint(b) for b in benches],
        "channels": [channels_of(b) for b in benches],
    }


def _op_pack_contract(spec: dict) -> dict:
    """Each pack parser module (``spec["modules"]``) checked against the
    pack-author contract, here in the project venv — the packs the bundle
    installed: its ContractItem rows, or why it could not be checked."""
    from rules_packager_base.pack_contract import check_pack_contract

    packs = {}
    for name in spec.get("modules") or []:
        try:
            rows = check_pack_contract(importlib.import_module(name), _rules_index(name))
            packs[name] = {"items": [asdict(row) for row in rows], "error": None}
        except Exception as exc:  # noqa: BLE001 — one pack's failure is its section's
            packs[name] = {"items": [], "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "packs": packs}


def _rules_index(module_name: str) -> dict | None:
    """The pack's parsed ``rules_index.json``, or None when it ships none: the
    installed package's ``rules/<version>/`` index whose
    ``pack_capabilities.parser_fragment_module`` names *module_name* —
    self-validating, so a stale unversioned ``rules/rules_index.json`` (no
    ``pack_capabilities``) never matches. None is a legal checker input."""
    import importlib.resources

    try:
        entries = list((importlib.resources.files(module_name.split(".")[0]) / "rules").iterdir())
    except Exception:  # noqa: BLE001 — no rules dir: no index
        return None
    for entry in entries:
        try:
            index = entry / "rules_index.json"
            data = (
                json.loads(index.read_text(encoding="utf-8"))
                if entry.is_dir() and index.is_file()
                else {}
            )
        except Exception:  # noqa: BLE001 — an unreadable index is no index
            continue
        caps = data.get("pack_capabilities")
        if isinstance(caps, dict) and caps.get("parser_fragment_module") == module_name:
            return data
    return None


def _op_write_not_started(spec: dict) -> dict:
    """The ERROR result of a run that could not start, written by its test's own engine into
    the run's folder (``module_path``, ``results_dir``, ``code``, ``description``)."""
    from rules_packager_base.test_helpers import write_not_started

    write_not_started(spec["module_path"], spec["results_dir"], spec["code"], spec["description"])
    return {"ok": True}


def _op_convert_record(spec: dict) -> dict:
    """An old run's ``results`` (the flat file of Procedo 5.0) and its ``manifest`` as the run
    record, its files in ``folder`` (the one-time converter, procedo.core.convert_records)."""
    from rules_packager_base.run_record import converted

    return {"ok": True, "record": converted(spec["results"], spec["manifest"], spec.get("folder"))}


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
    "functions_check": _op_functions_check,
    "function_signatures": _op_function_signatures,
    "section_ownership": _op_section_ownership,
    "reconstruct": _op_reconstruct,
    "build_manual_run": _manual.op_build_manual_run,
    "build_manual_result": _manual.op_build_manual_result,
    "manual_reading": _manual.op_manual_reading,
    "pack_contract": _op_pack_contract,
    "bench_identity": _op_bench_identity,
    "display_snapshot": _extract.op_display_snapshot,
    "extract": _extract.op_extract,
    "write_not_started": _op_write_not_started,
    "convert_record": _op_convert_record,
}


# The run's own ops: all a RUN engine computes besides its instrument frames.
_RUN_OPS = ("build_manual_run", "build_manual_result", "manual_reading")


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


# The ops that need no pack (pack_contract names a broken pack in its own section), and the per-key ops.
_PACK_FREE = frozenset(
    {
        "is_available",
        "pack_contract",
        "bench_identity",
        "functions_check",
        "function_signatures",
        "section_ownership",
        "write_not_started",
        "convert_record",
    }
)
_PER_KEY = frozenset({"display_snapshot", "extract"})


def packs_unloadable(exc: BaseException) -> str:
    """Why an op cannot trust its packs: the bundle's pack load raised ``exc``."""
    return f"The bundle's packs could not be loaded: {type(exc).__name__}: {exc}"


def _packs_error(spec: dict, registry) -> str:
    """``registry(spec)`` (the daemon's pack load) done: why it failed, else "" — also in a venv
    without the wheel, where each op gives its own "not importable" answer."""
    if registry is None:
        return ""
    try:
        registry(spec)
    except Exception as exc:  # noqa: BLE001 — any load failure is said by the op
        try:
            _import_wheel()
        except ImportError:
            return ""
        return packs_unloadable(exc)
    return ""


def run(spec: dict, registry=None) -> dict:
    """Run ``spec["op"]``; never raises. ``registry(spec)``, when given, loads the
    packs first (the daemon's once-per-process load). A load failure is never silent: a
    :data:`_PACK_FREE` op answers as usual, a :data:`_PER_KEY` op says it in each key's ``errors``
    (the user hears it through the validators' VALIDATOR_NOT_RUN finding: nothing reads a display
    snapshot's), any other op fails with it (``ParserUnavailable``) — it would answer wrong (a pack
    step's text blank). Unknown op → ``Other``; ImportError → ``ParserUnavailable``; any other
    exception → ``Other`` with its traceback."""
    load_error = _packs_error(spec, registry)
    op = spec.get("op")
    if op not in _OPS:
        return {
            "ok": False,
            "kind": "Other",
            "error": f"Unknown op: {op!r}. Valid ops: {sorted(_OPS)}",
        }
    if load_error and op not in _PACK_FREE | _PER_KEY:
        return {"ok": False, "kind": "ParserUnavailable", "error": load_error}
    try:
        answer = _OPS[op](spec)
    except ImportError as exc:
        return {"ok": False, "kind": "ParserUnavailable", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": "Other",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    if load_error and op in _PER_KEY:
        for entry in answer.get("tests") or answer.get("results") or []:
            entry["errors"].update(dict.fromkeys(entry["data"], load_error))
    return answer
