"""What a Call step's tooltip says of its function (the JSON+Code tab's step list): the project's
functions read by its TOOLS engine (the ``function_signatures`` op), never run."""

from __future__ import annotations

import logging
from pathlib import Path

from . import pack_parsers

log = logging.getLogger(__name__)


def function_signatures(project_root: Path) -> dict[str, str]:
    """Each of the project's functions by its Call name (``analysis.rise_time``): its signature,
    declared units and first docstring line; none, logged, when the engine cannot say (no venv,
    a wheel without the op)."""
    try:
        out = pack_parsers._engine_call(
            project_root, {"op": "function_signatures", "project": str(project_root)}
        )
    except Exception as exc:  # noqa: BLE001 — a tooltip, never an error
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not out.get("ok"):
        log.info("function signatures of %s: %s", project_root, out.get("error"))
        return {}
    return dict(out.get("functions") or {})
