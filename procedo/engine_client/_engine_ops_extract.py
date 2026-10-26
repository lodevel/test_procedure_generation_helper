"""The engine's extractor ops: ``display_snapshot`` and ``extract`` — the bundle's declared
extractors (``defaults.json:extractors``) run on the tests' documents. Part of the engine's op
table (``_engine_ops``), loaded beside it — never through the ``procedo`` package, which is
absent from the project venv. Their specs and results are documented in ``_engine_ops``."""

from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

# The four DISPLAY domains snapshotted for the app. The validate_* twins are
# asked per document (``extract``): an on-demand banner, not the paint path.
_DISPLAY_KEYS = ("meta", "steps", "expected", "equipment")

#: How many tests a sweep reads at once: on a share each read is a round trip, answered
#: slowly but several at a time (the app reads its own the same way, ``file_cache``).
_READERS = 8

#: A test whose document could not be read (its entry says why).
_UNREAD = object()


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


def _resolve_extractors(bundle_dir: Path, keys=_DISPLAY_KEYS) -> tuple[dict, dict, dict]:
    """Resolve the extractors of ``keys`` exactly the way the bundle declares
    them (``{module, function}`` per key), importing INSIDE this (project)
    venv. Returns ``(callables, errors, wheel_info)``; a missing or
    unimportable key lands in ``errors``, never raises."""
    decls = _load_extractor_decls(bundle_dir)
    fns: dict = {}
    errors: dict = {}
    wheel_info: dict = {"module": "", "file": "", "version": ""}
    for key in keys:
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


def _read_test(folder: str, proc_path: Path, resolve_errors: dict) -> tuple:
    """One test's files: ``(entry, document)`` — :data:`_UNREAD` when it could not be read,
    the entry saying why — or ``(None, None)`` for a folder without ``procedure.json``."""
    if not proc_path.exists():
        return None, None
    entry: dict = {"folder": folder, "data": {}, "errors": dict(resolve_errors)}
    # Stat BEFORE reading: if the file is rewritten mid-read, the entry
    # carries the OLD mtime, so the app's freshness check (current stat
    # vs entry mtime) fails safe: that test is not served from the snapshot.
    try:
        _st = proc_path.stat()
        entry["mtime_ns"] = _st.st_mtime_ns
        entry["size"] = _st.st_size
    except OSError as exc:
        entry["mtime_ns"] = 0
        for key in _DISPLAY_KEYS:
            entry["errors"][key] = f"cannot stat {proc_path.name}: {exc}"
        entry["text_newer"] = False
        return entry, _UNREAD
    entry["text_newer"] = _text_newer(proc_path)
    try:
        return entry, json.loads(proc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        for key in _DISPLAY_KEYS:
            entry["errors"][key] = f"cannot read {proc_path.name}: {exc}"
        return entry, _UNREAD


def _extract_into(entry: dict, doc, fns: dict) -> None:
    """Each extractor of ``fns`` run on ``doc``, its JSON payload in
    ``entry["data"]``; one that raises is explained in ``entry["errors"]``."""
    for key, fn in fns.items():
        try:
            if key == "meta":
                entry["data"][key] = _meta_payload(fn(doc))
            else:
                entry["data"][key] = _items_payload(fn(doc))
        except Exception as exc:  # noqa: BLE001 — one bad test/key must not kill the sweep
            entry["errors"][key] = f"{type(exc).__name__}: {exc}"


def op_display_snapshot(spec: dict) -> dict:
    """The main window's display data for the tests of ``spec["project"]``
    (``spec["tests"]``: null = every test folder), extracted by the bundle's
    PINNED wheel with the extractors ``defaults.json`` declares. The pack
    registry is the daemon's, loaded from the frame's ``_bundle_dir``.
    Per-test / per-key failures never fail the sweep: the key is absent from
    ``data`` and explained in ``errors``."""
    project_root = Path(spec["project"])
    requested = spec.get("tests")

    fns, resolve_errors, wheel_info = _resolve_extractors(bundle_dir_of(spec))

    tests_dir = project_root / "tests"
    if requested is None:
        try:
            folders = sorted(d.name for d in tests_dir.iterdir() if d.is_dir())
        except OSError as exc:
            return {"ok": False, "kind": "Other", "error": f"cannot list {tests_dir}: {exc}"}
    else:
        folders = [f for f in requested if isinstance(f, str)]

    # The files several tests at a time; the extractors one test after another.
    paths = [tests_dir / folder / "procedure.json" for folder in folders]
    with ThreadPoolExecutor(_READERS) as pool:
        read = list(pool.map(_read_test, folders, paths, [resolve_errors] * len(folders)))
    out = []
    for entry, doc in read:
        if entry is None:
            # Deleted/never-compiled test: omit it — the store drops
            # requested-but-absent folders from the snapshot.
            continue
        if doc is not _UNREAD:
            _extract_into(entry, doc, fns)  # per-key failures become error markers
        out.append(entry)
    return {"ok": True, "wheel": wheel_info, "tests": out}


def bundle_dir_of(spec: dict) -> Path:
    """The bundle a frame's extractors come from: the frame's ``_bundle_dir``,
    else the project's ``bundle/``."""
    return Path(spec.get("_bundle_dir") or (Path(spec["project"]) / "bundle"))


def op_extract(spec: dict) -> dict:
    """``spec["keys"]`` of each document of ``spec["procedures"]``, by the
    extractors the bundle declares — what the display snapshot does not
    hold: an untagged or just-edited document, and the validators. A key
    that cannot be run is explained in that result's ``errors``, never
    fails the op."""
    fns, errors, _wheel = _resolve_extractors(bundle_dir_of(spec), tuple(spec.get("keys") or ()))
    results = []
    for doc in spec.get("procedures") or []:
        entry: dict = {"data": {}, "errors": dict(errors)}
        _extract_into(entry, doc, fns)  # any shape: a validator reports PROC_NOT_OBJECT
        results.append(entry)
    return {"ok": True, "results": results}
