"""ODB++ board inspection for the editor (components + netlist, no rendering).

The editor runs as its own process and only knows the project root, so it loads
the board inventory itself by shelling the sibling ``odb_image_generator`` CLI's
``--list-nets`` (the same JSON the main GUI consumes). Everything degrades to
empty when there is no archive / no CLI / a failure — the netlist panel then
just shows a friendly empty state, never an error.
"""

from __future__ import annotations

import atexit
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional


def odb_cli_path() -> Optional[Path]:
    """Path to the sibling ``odb_image_generator/cli.py`` (the editor lives at
    ``<repo>/external/test_procedure_generation_helper/workflow_editor/core/``),
    or ``None`` when it isn't on disk (e.g. a standalone editor checkout)."""
    # parents: [core, workflow_editor, test_procedure_generation_helper, external]
    cli = Path(__file__).resolve().parents[3] / "odb_image_generator" / "cli.py"
    return cli if cli.exists() else None


class _RenderDaemonClient:
    """Managed long-lived render daemon (NDJSON over stdio) — keeps the parsed
    board + decoded faces in RAM so warm renders cost ~0.3-0.4s instead of
    ~1.7s per one-shot CLI run. Any failure returns None and the caller falls
    back to the CLI (the daemon is an accelerator, never a dependency)."""

    def __init__(self) -> None:
        self._proc = None
        self._lock = threading.Lock()
        self._next_id = 0

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._proc = None
        cli = odb_cli_path()
        daemon = cli.with_name("render_daemon.py") if cli else None
        if daemon is None or not daemon.exists():
            return False
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(daemon)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError:
            self._proc = None
            return False
        return True

    def request(self, obj: dict, timeout: float):
        with self._lock:
            if not self._ensure():
                return None
            self._next_id += 1
            obj = {**obj, "id": self._next_id}
            try:
                self._proc.stdin.write(json.dumps(obj) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self.kill()
                return None
            holder: dict = {}

            def _read() -> None:
                try:
                    holder["line"] = self._proc.stdout.readline()
                except Exception:  # noqa: BLE001
                    holder["line"] = ""

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout)
            line = holder.get("line", "")
            if t.is_alive() or not line:
                self.kill()
                return None
            try:
                return json.loads(line)
            except ValueError:
                self.kill()
                return None

    def kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


_render_daemon = _RenderDaemonClient()
atexit.register(_render_daemon.kill)


def find_odb_tgz(project_root: Optional[Path]) -> Optional[Path]:
    """The project's ODB archive: a configured override in
    ``config.json:odb_archive`` (relative to the project root, or absolute) when
    it exists, else the first ``*.tgz`` in the project root. ``None`` if neither.
    Matches the main GUI's resolution so both read the same archive."""
    if project_root is None:
        return None
    root = Path(project_root)
    try:
        data = json.loads((root / "config" / "config.json").read_text(
            encoding="utf-8"))
        raw = data.get("odb_archive")
        if isinstance(raw, str) and raw:
            p = Path(raw)
            if not p.is_absolute():
                p = root / raw
            if p.exists():
                return p
    except (OSError, ValueError):
        pass
    try:
        tgz = sorted(root.glob("*.tgz"))
    except OSError:
        return None
    return tgz[0] if tgz else None


def load_board(project_root: Optional[Path]) -> dict:
    """``{components, nets, error}`` for the project's ODB++ archive (via the CLI
    ``--list-nets``). The ODB tgz is auto-detected as the first ``*.tgz`` in the
    project root. ``error`` is ``""`` on success, else a human-readable reason
    (no project / no archive at <path> / CLI not found / CLI failed: …) so the
    panel can surface WHY rather than a blanket 'no archive'. Never raises;
    synchronous — run it off the UI thread."""
    empty = {"components": [], "nets": [], "error": ""}
    if project_root is None:
        return {**empty, "error": "No project is open in the editor."}
    tgz = find_odb_tgz(project_root)
    if tgz is None:
        return {**empty, "error":
                f"No ODB++ .tgz archive in the project folder:\n{project_root}"}
    cli = odb_cli_path()
    if cli is None:
        return {**empty,
                "error": "ODB image-generator CLI not found next to the editor."}
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), "--odb-tgz", str(tgz), "--list-nets"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {**empty, "error": f"Could not run the ODB CLI: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        why = tail[-1] if tail else f"exit code {proc.returncode}"
        return {**empty, "error": f"ODB CLI failed (exit {proc.returncode}): {why}"}
    if not proc.stdout.strip():
        return {**empty, "error": "ODB CLI returned no data."}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {**empty, "error": "ODB CLI output was not valid JSON."}
    if not isinstance(data, dict):
        return {**empty, "error": "ODB CLI output had an unexpected shape."}
    data.setdefault("components", [])
    data.setdefault("nets", [])
    data.setdefault("error", "")
    return data


def is_hidden_net(name: str, hide_prefixes) -> bool:
    """True if *name* starts (case-insensitive) with any of *hide_prefixes*
    (e.g. ``["Net"]`` to hide Altium autogen net names like ``NetD16_A``)."""
    n = (name or "").lower()
    return any(n.startswith((p or "").lower()) for p in (hide_prefixes or ()) if p)


_DEFAULT_HIDE_PREFIXES = ["Net"]


def load_hide_prefixes(project_root: Optional[Path]) -> list:
    """Net-name prefixes to hide by default, from
    ``<project>/config/config.json`` (``net_explorer.hide_prefixes``). Defaults
    to ``["Net"]`` (Altium autogen) when unset / unreadable."""
    if project_root is None:
        return list(_DEFAULT_HIDE_PREFIXES)
    cfg = Path(project_root) / "config" / "config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(_DEFAULT_HIDE_PREFIXES)
    pref = (data.get("net_explorer") or {}).get("hide_prefixes")
    if isinstance(pref, list):
        return [str(p) for p in pref]
    return list(_DEFAULT_HIDE_PREFIXES)


def save_hide_prefixes(project_root: Optional[Path], prefixes: list) -> bool:
    """Persist net-name hide prefixes to ``<project>/config/config.json``
    (merging into the existing object). Returns True on success."""
    if project_root is None:
        return False
    cfg = Path(project_root) / "config" / "config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    ne = data.get("net_explorer")
    if not isinstance(ne, dict):
        ne = {}
    ne["hide_prefixes"] = [str(p) for p in (prefixes or [])]
    data["net_explorer"] = ne
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        return True
    except OSError:
        return False


def cached_image_paths(project_root: Optional[Path], refdes: str,
                       pad: Optional[str]) -> tuple:
    """(zoomed, wide) cached PNG paths for a target under
    ``<project>/.media_cache`` (shared layout with the main GUI), or
    ``(None, None)`` when not present."""
    if project_root is None or not refdes:
        return (None, None)
    cache = Path(project_root) / ".media_cache"
    fname = f"{refdes}_pad{pad}.png" if pad else f"{refdes}.png"
    z = cache / "zoomed" / "images" / fname
    w = cache / "wide" / "images" / fname
    return (z if z.exists() else None, w if w.exists() else None)


# Image-generator render params. These defaults mirror the main GUI's
# image-generator settings, so absent a project override the editor renders
# byte-identical images into the shared cache. A project may override any of
# these via <project>/config/config.json:image_generator (the planned
# template / Project-Configuration field).
_DEFAULT_RENDER = {
    "img_size": 1024, "render_size": 4096, "max_workers": 0, "batch_size": 50,
    "parallel_render": False, "parallel_export": True,
    "window_mm_zoomed": 40.0, "cross_arm_mm_zoomed": 3.0, "cross_thickness_px_zoomed": 6,
    "window_mm_wide": 200.0, "cross_arm_mm_wide": 6.0, "cross_thickness_px_wide": 12,
}


def load_render_params(project_root: Optional[Path]) -> dict:
    """Image-generator render params for the project, from
    ``<project>/config/config.json:image_generator``, falling back to the main
    GUI defaults for any unset key."""
    params = dict(_DEFAULT_RENDER)
    if project_root is None:
        return params
    cfg = Path(project_root) / "config" / "config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return params
    ig = data.get("image_generator")
    if isinstance(ig, dict):
        for k in params:
            if k in ig:
                params[k] = ig[k]
    return params


def render_target(project_root: Optional[Path], refdes: str,
                  pad: Optional[str]) -> tuple:
    """Render ONE component/pin board image into the SHARED
    ``<project>/.media_cache`` (zoomed + wide passes via the ODB CLI) and return
    its ``(zoomed, wide)`` cached paths. Cache-first. **Graceful** ``(None, None)``
    when there is no archive / CLI, or on failure. Only ``mkdir``s the view dirs —
    never touches the GUI's ``.odb_checksum`` (so it can't wipe the shared cache).
    Synchronous; run it OFF the UI thread."""
    z, w = cached_image_paths(project_root, refdes, pad)
    if z or w:
        return (z, w)
    tgz = find_odb_tgz(project_root)
    cli = odb_cli_path()
    if tgz is None or cli is None or not refdes:
        return (None, None)
    p = load_render_params(project_root)
    cache = Path(project_root) / ".media_cache"
    target = f"{refdes}:{pad}" if pad else refdes

    # Persistent daemon first: warm renders ~0.3-0.4s (board + decoded faces
    # held in RAM). Falls back to a one-shot CLI run on any failure.
    resp = _render_daemon.request({
        "cmd": "render", "odb": str(tgz),
        "img_size": p["img_size"], "render_size": p["render_size"],
        "png_compress_level": 1,
        "render_cache": str(cache / "base_renders"),
        "views": [
            {"out_dir": str(cache / "zoomed"),
             "window_mm": p["window_mm_zoomed"],
             "cross_arm_mm": p["cross_arm_mm_zoomed"],
             "cross_thickness_px": p["cross_thickness_px_zoomed"]},
            {"out_dir": str(cache / "wide"),
             "window_mm": p["window_mm_wide"],
             "cross_arm_mm": p["cross_arm_mm_wide"],
             "cross_thickness_px": p["cross_thickness_px_wide"]},
        ],
        "targets": [target],
    }, timeout=120)
    if resp and resp.get("ok"):
        return cached_image_paths(project_root, refdes, pad)

    # ONE invocation: both views crop from a single board render; the face
    # render is disk-cached (wiped with the GUI's .odb_checksum mechanism).
    try:
        subprocess.run(
            [sys.executable, str(cli), "--odb-tgz", str(tgz),
             "--view", str(cache / "zoomed"),
             str(p["window_mm_zoomed"]), str(p["cross_arm_mm_zoomed"]),
             str(p["cross_thickness_px_zoomed"]),
             "--view", str(cache / "wide"),
             str(p["window_mm_wide"]), str(p["cross_arm_mm_wide"]),
             str(p["cross_thickness_px_wide"]),
             "--render-cache", str(cache / "base_renders"),
             "--target", target,
             "--img-size", str(p["img_size"]),
             "--render-size", str(p["render_size"]),
             "--max-workers", str(p["max_workers"]),
             "--batch-size", str(p["batch_size"]),
             "--parallel-render" if p["parallel_render"] else "--no-parallel-render",
             "--parallel-export" if p["parallel_export"] else "--no-parallel-export"],
            capture_output=True, text=True, timeout=240,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return cached_image_paths(project_root, refdes, pad)
