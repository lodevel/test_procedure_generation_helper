"""Per-project cache of the DCDC finder's parsed IC power list.

The finder LLM pass is slow, so its parsed worklist is cached: reopening the
wizard shows the ICs immediately instead of re-running the finder. Only the LIST
is cached — the builds themselves are NOT (closing the wizard drops them, by
design; that's the operator's call). The cache auto-invalidates when the board
archive changes (a cheap ``*.tgz`` stat signature), and the wizard's
"Restart analysis" button clears it explicitly.

PURE: no Qt, no LLM. Stored as ``<project_root>/.dcdc_finder_cache.json``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .list_parse import IcRow

log = logging.getLogger(__name__)

_CACHE_NAME = ".dcdc_finder_cache.json"

__all__ = ["board_signature", "load", "save", "clear"]


def _cache_path(project_root) -> Optional[Path]:
    if not project_root:
        return None
    return Path(project_root) / _CACHE_NAME


def board_signature(project_root) -> str:
    """A cheap board fingerprint — the first ``*.tgz``'s name + size + mtime — so
    the cache auto-invalidates when the ODB archive is replaced. ``""`` when the
    root is unknown or carries no archive (then the cache never matches and the
    finder simply re-runs)."""
    if not project_root:
        return ""
    try:
        tgzs = sorted(Path(project_root).glob("*.tgz"))
        if not tgzs:
            return ""
        st = tgzs[0].stat()
        return f"{tgzs[0].name}:{st.st_size}:{int(st.st_mtime)}"
    except Exception:  # noqa: BLE001 — signature is best-effort
        return ""


def load(project_root) -> Optional[list]:
    """The cached :class:`IcRow` list when present AND the board signature still
    matches; ``None`` when missing, stale (board changed), or corrupt."""
    p = _cache_path(project_root)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("signature") != board_signature(project_root):
            return None  # board changed under the cache → treat as absent
        rows = data.get("rows") or []
        return [IcRow(refdes=r["refdes"], part=r["part"],
                      kind=r["kind"], rail=r["rail"]) for r in rows]
    except Exception:  # noqa: BLE001
        log.debug("dcdc finder cache load failed", exc_info=True)
        return None


def save(project_root, rows) -> None:
    """Persist the parsed IcRows with the current board signature. Best-effort —
    a write failure (read-only dir, etc.) is swallowed; the wizard still works,
    it just won't have a cache next time."""
    p = _cache_path(project_root)
    if p is None:
        return
    try:
        payload = {
            "signature": board_signature(project_root),
            "rows": [{"refdes": r.refdes, "part": r.part,
                      "kind": r.kind, "rail": r.rail} for r in rows],
        }
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("dcdc finder cache save failed", exc_info=True)


def clear(project_root) -> None:
    """Delete the cache (the 'Restart analysis' action). No-op when absent."""
    p = _cache_path(project_root)
    try:
        if p is not None and p.is_file():
            p.unlink()
    except Exception:  # noqa: BLE001
        log.debug("dcdc finder cache clear failed", exc_info=True)
