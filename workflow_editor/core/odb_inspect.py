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
