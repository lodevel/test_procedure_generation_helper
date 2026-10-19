"""Where a project's MACHINE-LOCAL state is — the editor's one question to the host.

Two things the editor reaches for used to sit inside the project folder and be
composed from its path:

* the **venv** it dispatches every wheel op into (``<project>/.venv``), and
* the **media cache** its ODB inspector shares with the main GUI
  (``<project>/.media_cache``).

Neither is there since projects moved to a share, and neither is the bundle
install record it watches (``<project>/config/.bundle_installed.json``), which
describes what is in that venv. All three live under the per-user
app-data root, and only the host's ``procedo.core.project_local_state`` knows
where — so this module asks it, and never composes a path of its own.

The venv is SHARED: one per bundle, used by every project on it, and bounded by a
limit the user sets. Two consequences for this bridge. An interpreter EXISTING is
no longer proof there is a venv to run — a build that was interrupted leaves one
sitting there unfinished — so :func:`venv` asks the host's ``existing_venv_python``,
which answers only for a finished build, and :func:`interpreter` is kept for saying
where one WOULD be in a message. And the environment can be deleted out from under
an open editor by another window's clean-up, which reads here exactly as it always
did when one was missing: the op refuses and names the LLM fallback.

The venv answer carries the project's **bundle directory** with it
(:class:`ProjectVenv`). The bridge used to recover the project root from the
interpreter (``python.parents[2]``); that expression now names a folder under the
app-data root, so every op would have run with an empty pack registry. Resolving
both from the project root together is what makes that impossible to write again.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

from procedo.core import project_local_state, venv_pool


class ProjectVenv(NamedTuple):
    """This project's interpreter and its bundle directory, resolved together."""

    python: Path
    bundle_dir: Path


def interpreter(project_root: Path) -> Optional[Path]:
    """Where this project's interpreter would be — no existence check.

    For MESSAGES, not for dispatch: the path is named even when the environment
    has not been built, or was left half-built. ``None`` when the project has no
    bundle and therefore no environment of its own.
    """
    venv_dir = project_local_state.venv_dir(project_root)
    return project_local_state.venv_python(venv_dir) if venv_dir is not None else None


def venv(project_root: Path) -> Optional[ProjectVenv]:
    """This project's interpreter + bundle dir, or ``None`` when there is no venv.

    The host answers, and answers only for a build that FINISHED. Asking whether
    the file exists would have been enough when a venv was a folder this app made
    in one go; with a shared pool, a killed install leaves an interpreter that can
    import half a bundle, and dispatching an op into it is how a wrong answer
    looks exactly like a right one.
    """
    python = project_local_state.existing_venv_python(project_root)
    if python is None:
        return None
    return ProjectVenv(Path(python), Path(project_root) / "bundle")


def _engine_token(role: str) -> str:
    """RUN holds instruments, so its claim is a bench holder's, which every Reinstall
    refuses; a TOOLS claim names this app, whose own Reinstall stops it first."""
    return venv_pool.BENCH if role == "run" else venv_pool.engine_token()


def claim_engine(project_root: Path, pid: int, role: str) -> Optional[Path]:
    """Claim this project's venv for an engine process (its pid), so an eviction in any
    window, and a Reinstall as :func:`_engine_token` says, refuses while it runs.
    Returns the folder claimed."""
    return venv_pool.claim_child(project_root, pid, _engine_token(role))


def release_engine(folder: Optional[Path], pid: int, role: str) -> None:
    venv_pool.release(folder, pid, _engine_token(role))


def why_no_venv(project_root: Path) -> str:
    """The message a user reads when :func:`venv` answered ``None``: the path it looked at."""
    python = interpreter(project_root)
    where = f" (expected {python})" if python is not None else ""
    return (
        f"No machine-local venv for {project_root}{where}. "
        "Open the project in the main app, or reinstall its bundle, to build it. "
        "The LLM fallback remains available."
    )


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
