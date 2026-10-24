"""The project's ODB++ board for the editor: its listing (components + netlist)
and the picture of one location.

The editor only knows the project root, so this module turns it into the
host's terms — the archive (:func:`procedo.core.odb_archive.find`), the
machine-local media cache the main GUI shares, the user's image settings — and
asks the host's :mod:`procedo.core.board_images`, the one client of the ODB
image tool. Everything degrades to empty when there is no archive / no CLI / a
failure — the netlist panel then shows a friendly empty state, never an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from procedo.core import board_images, odb_archive
from procedo.core.app_settings import get_image_generator_settings
from procedo.core.procedure_media import MediaRef

from .. import _project_local_state


def load_board(project_root: Optional[Path]) -> dict:
    """``{components, nets, error}`` for the project's ODB++ archive (via the CLI
    ``--list-nets``, read fresh: the listing cache is the main GUI's, checked
    against the archive only when it opens the project). ``error`` is ``""`` on
    success, else a human-readable reason (no project / no archive at <path> /
    CLI not found / CLI failed: …) so the panel can surface WHY rather than a
    blanket 'no archive'. Never raises; synchronous — run it off the UI thread."""
    empty = {"components": [], "nets": [], "error": ""}
    if project_root is None:
        from ..pack_ops.pack_parsers import NO_PROJECT

        return {**empty, "error": NO_PROJECT}
    tgz = odb_archive.find(project_root)
    if tgz is None:
        return {**empty, "error":
                f"No ODB++ .tgz archive in the project folder:\n{project_root}"}
    return board_images.list_board(tgz, None)


def cached_image_paths(project_root: Optional[Path], refdes: str,
                       pad: Optional[str]) -> tuple:
    """(zoomed, wide) cached PNG paths of the location in the project's MACHINE-LOCAL
    media cache — the same folder the main GUI uses, which is why it is asked of the
    host rather than composed (:mod:`procedo.studio._project_local_state`); a cache
    built here from the project path would stop being shared the moment projects
    moved to a share. Exact: a pin is never answered with its component's picture.
    ``(None, None)`` when not present."""
    if not refdes:
        return (None, None)
    return board_images.cached_image_paths(
        _project_local_state.media_cache(project_root), refdes, pad, exact=True)


def render_target(project_root: Optional[Path], refdes: str,
                  pad: Optional[str]) -> tuple:
    """Render ONE component/pin board image into the SHARED machine-local media
    cache (zoomed + wide, with the user's image settings) and return its
    ``(zoomed, wide)`` cached paths. Cache-first. **Graceful** ``(None, None)``
    when there is no archive / CLI, or on failure. Never touches the GUI's
    ``.odb_checksum`` (so it can't wipe the shared cache).
    Synchronous; run it OFF the UI thread."""
    z, w = cached_image_paths(project_root, refdes, pad)
    if z or w:
        return (z, w)
    cache = _project_local_state.media_cache(project_root)
    tgz = odb_archive.find(project_root) if project_root is not None else None
    if tgz is None or cache is None or not refdes:  # no project_id: nothing to render into
        return (None, None)
    board_images.OdbImageWorker(
        tgz, cache, [MediaRef({"ref": {"component": refdes, "pin": pad}})],
        get_image_generator_settings(project_root), lambda _text: None,
    ).run()
    return cached_image_paths(project_root, refdes, pad)
