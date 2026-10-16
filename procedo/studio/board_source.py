"""Shared, dependency-free selection of the project board archive."""

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


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
    except (OSError, ValueError) as exc:
        # Best-effort config override; fall through to the *.tgz glob below.
        log.debug("odb_archive override unreadable (%s); falling back to glob", exc)
    try:
        tgz = sorted(root.glob("*.tgz"))
    except OSError:
        return None
    return tgz[0] if tgz else None
