"""ODB++ board inspection for the editor (components + netlist, no rendering).

The editor runs as its own process and only knows the project root, so it loads
the board inventory itself by shelling the sibling ``odb_image_generator`` CLI's
``--list-nets`` (the same JSON the main GUI consumes). Everything degrades to
empty when there is no archive / no CLI / a failure — the netlist panel then
just shows a friendly empty state, never an error.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def odb_cli_path() -> Optional[Path]:
    """Path to the sibling ``odb_image_generator/cli.py`` (the editor lives at
    ``<repo>/external/test_procedure_generation_helper/workflow_editor/core/``),
    or ``None`` when it isn't on disk (e.g. a standalone editor checkout)."""
    # parents: [core, workflow_editor, test_procedure_generation_helper, external]
    cli = Path(__file__).resolve().parents[3] / "odb_image_generator" / "cli.py"
    return cli if cli.exists() else None


def find_odb_tgz(project_root: Optional[Path]) -> Optional[Path]:
    """First ``*.tgz`` in the project root (alphabetical), or ``None``."""
    if project_root is None:
        return None
    try:
        tgz = sorted(Path(project_root).glob("*.tgz"))
    except OSError:
        return None
    return tgz[0] if tgz else None


def load_board(project_root: Optional[Path]) -> dict:
    """``{components, nets}`` for the project's ODB++ archive (via the CLI
    ``--list-nets``). Returns ``{"components": [], "nets": []}`` when there is no
    archive, no CLI, or the CLI fails — never raises. Synchronous; run it off the
    UI thread."""
    empty = {"components": [], "nets": []}
    tgz = find_odb_tgz(project_root)
    cli = odb_cli_path()
    if tgz is None or cli is None:
        return empty
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), "--odb-tgz", str(tgz), "--list-nets"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return empty
    if proc.returncode != 0 or not proc.stdout.strip():
        return empty
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("components", [])
    data.setdefault("nets", [])
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
    common = [
        "--img-size", str(p["img_size"]), "--render-size", str(p["render_size"]),
        "--max-workers", str(p["max_workers"]), "--batch-size", str(p["batch_size"]),
        "--parallel-render" if p["parallel_render"] else "--no-parallel-render",
        "--parallel-export" if p["parallel_export"] else "--no-parallel-export",
    ]
    for view in ("zoomed", "wide"):
        out = cache / view
        try:
            out.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, str(cli), "--odb-tgz", str(tgz),
                 "--out-dir", str(out), "--target", target,
                 "--window-mm", str(p[f"window_mm_{view}"]),
                 "--cross-arm-mm", str(p[f"cross_arm_mm_{view}"]),
                 "--cross-thickness-px", str(p[f"cross_thickness_px_{view}"])]
                + common,
                capture_output=True, text=True, timeout=180,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return cached_image_paths(project_root, refdes, pad)
