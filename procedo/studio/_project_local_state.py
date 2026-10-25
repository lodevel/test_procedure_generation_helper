"""Where a project's MACHINE-LOCAL state is — the editor's one question to the host.

Two things the editor reaches for used to sit inside the project folder and be
composed from its path:

* the **media cache** its ODB inspector shares with the main GUI
  (``<project>/.media_cache``), and
* the bundle install record it watches (``<project>/config/.bundle_installed.json``),
  which describes what is in the project's venv (:mod:`procedo.engine_client.project_venv`).

Neither is there since projects moved to a share. Both live under the per-user
app-data root, and only the host's ``procedo.core.project_local_state`` knows
where — so this module asks it, and never composes a path of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from procedo.core import project_local_state


def install_record(project_root: Optional[Path]) -> Optional[Path]:
    """The host's record of what it installed into this project's venv.

    Watched as a belt-and-braces bundle-change signal. It moved out of
    ``<project>/config/`` with the venv it describes and then again into the
    shared pool folder that venv lives in, so composing it from the project would
    name a file nothing ever writes — a watch that can never fire, which is worse
    than no watch. ``None`` without a bundle, and the caller then relies on the
    bundle-dir signature alone.
    """
    if project_root is None:
        return None
    return project_local_state.install_record(project_root)


def media_cache(project_root: Optional[Path]) -> Optional[Path]:
    """This project's rendered-media cache — the SAME folder the main GUI uses.

    ``None`` without a ``project_id``: the ODB panel then renders nothing and shows
    nothing, which is the degradation it already had with no cache.
    """
    if project_root is None:
        return None
    return project_local_state.media_cache_dir(project_root)
