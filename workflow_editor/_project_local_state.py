"""Where a project's MACHINE-LOCAL state is — the editor's one question to the host.

Two things the editor reaches for used to sit inside the project folder and be
composed from its path:

* the **venv** it dispatches every wheel op into (``<project>/.venv``), and
* the **media cache** its ODB inspector shares with the main GUI
  (``<project>/.media_cache``).

Neither is there since projects moved to a share, and neither is the bundle
install record it watches (``<project>/config/.bundle_installed.json``), which
describes what is in that venv. All three live under the per-user
app-data root, and only the host's ``project_services.project_local_state`` knows
where — so this module asks, through the same guarded import :mod:`_host_services`
uses for every other host-backed feature, and never composes a path of its own.

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

Standalone — a checkout with no host on ``sys.path`` — there is nothing to ask, so
every function answers ``None`` and each caller degrades exactly as it already did
when a venv or a cache was simply missing.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

#: The host seam that owns machine-local project state.
HOST_MODULE = "project_services.project_local_state"


class ProjectVenv(NamedTuple):
    """This project's interpreter and its bundle directory, resolved together."""

    python: Path
    bundle_dir: Path


def _local_state():
    """The host seam, or ``None`` standalone — the editor's guarded-import rule."""
    from . import _host_services

    return _host_services.load_optional(HOST_MODULE)


def interpreter(project_root: Path) -> Optional[Path]:
    """Where this project's interpreter would be — no existence check.

    For MESSAGES, not for dispatch: the path is named even when the environment
    has not been built, or was left half-built. ``None`` when there is no host, or
    when the project has no bundle and therefore no environment of its own.
    """
    local_state = _local_state()
    if local_state is None:
        return None
    venv_dir = local_state.venv_dir(project_root)
    return local_state.venv_python(venv_dir) if venv_dir is not None else None


def venv(project_root: Path) -> Optional[ProjectVenv]:
    """This project's interpreter + bundle dir, or ``None`` when there is no venv.

    The host answers, and answers only for a build that FINISHED. Asking whether
    the file exists would have been enough when a venv was a folder this app made
    in one go; with a shared pool, a killed install leaves an interpreter that can
    import half a bundle, and dispatching an op into it is how a wrong answer
    looks exactly like a right one.
    """
    local_state = _local_state()
    if local_state is None:
        return None
    python = local_state.existing_venv_python(project_root)
    if python is None:
        return None
    return ProjectVenv(Path(python), Path(project_root) / "bundle")


def why_no_venv(project_root: Path) -> str:
    """The message a user reads when :func:`venv` answered ``None``.

    The two causes read differently on purpose: a standalone editor can never have
    a project venv, while an embedded one that cannot find it wants the path it
    looked at.
    """
    if _local_state() is None:
        return (
            "This editor is running without the host app, which is the only thing "
            "that knows where a project's machine-local venv lives. "
            "The LLM fallback remains available."
        )
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
    than no watch. ``None`` without a host or without a bundle, and the caller
    then relies on the bundle-dir signature alone.
    """
    if project_root is None:
        return None
    local_state = _local_state()
    return None if local_state is None else local_state.install_record(project_root)


def media_cache(project_root: Optional[Path]) -> Optional[Path]:
    """This project's rendered-media cache — the SAME folder the main GUI uses.

    ``None`` without a host or a ``project_id``: the ODB panel then renders nothing
    and shows nothing, which is the degradation it already had with no cache.
    """
    if project_root is None:
        return None
    local_state = _local_state()
    return None if local_state is None else local_state.media_cache_dir(project_root)
