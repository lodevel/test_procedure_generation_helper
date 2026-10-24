"""The files each Studio process owns in an OpenCode launch dir: their names, owner and
removal.

Every Studio process of the user shares the launch dir (Bench starts one per "Open in
Studio"), so each writes only its own ``opencode-<pid>.<kind>`` files: its server's record
(the orphan sweep reads it; its run_skill MCP server reads its server's port from it), its
derived config and its run_skill gate universe. A second Studio then never rewrites the
first one's config with its own project's paths.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from .mcp_config import win_to_wsl_path

log = logging.getLogger(__name__)

PID_FILE = "pid"  # its server's Linux pid + port (the orphan sweep's record)
CONFIG_FILE = "json"  # its derived opencode.json, the server's OPENCODE_CONFIG
UNIVERSE_FILE = "universe.json"  # its run_skill gate universe
_PREFIX = "opencode-"


def launch_file_path(launch_dir, kind: str, owner: Optional[int] = None) -> Path:
    """The launch file *kind* (``PID_FILE``, ``CONFIG_FILE``, ``UNIVERSE_FILE``) of the Studio
    process *owner* (Windows pid; default THIS one) in *launch_dir*: ``opencode-<owner>.<kind>``."""
    return Path(launch_dir) / f"{_PREFIX}{os.getpid() if owner is None else owner}.{kind}"


def launch_file_owners(launch_dir) -> list[int]:
    """The Studio processes that own a launch file in *launch_dir*, in order."""
    owners = set()
    for path in Path(launch_dir).glob(f"{_PREFIX}*"):
        owner = path.name[len(_PREFIX):].split(".", 1)[0]
        if owner.isdigit():
            owners.add(int(owner))
    return sorted(owners)


def config_wsl_path(launch_dir, owner: Optional[int] = None) -> str:
    """The WSL path of *owner*'s launch config in *launch_dir* (a Windows path): the
    ``OPENCODE_CONFIG`` its server is launched with, and so that server's identity."""
    name = launch_file_path(launch_dir, CONFIG_FILE, owner).name
    return win_to_wsl_path(str(launch_dir)).rstrip("/") + "/" + name


def config_env(launch_dir) -> dict[str, str]:
    """The environment that points a server at THIS process's launch config in *launch_dir*
    (none without a launch dir: OpenCode's own discovery then). Project-config discovery is
    off: OpenCode would merge every ``opencode.json`` from its cwd (the launch dir) up to ``/``
    on top of ``OPENCODE_CONFIG`` (e.g. the launch dir's old shared one, or the relic in its
    parent folder), overriding this config's model and MCP blocks."""
    if not launch_dir:
        return {}
    return {"OPENCODE_CONFIG": config_wsl_path(launch_dir), "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}


def remove_launch_files(launch_dir, owner: Optional[int] = None) -> None:
    """Delete *owner*'s (default THIS process's) launch files in *launch_dir* but its pid
    file: that record goes only once its server is gone (the server manager's stop / sweep)."""
    keep = launch_file_path(launch_dir, PID_FILE, owner).name
    for path in Path(launch_dir).glob(launch_file_path(launch_dir, "*", owner).name):
        if path.name != keep:
            unlink_quietly(path)


def unlink_quietly(path: Path) -> None:
    """Delete *path* if it is there; a failure is logged (launch-dir bookkeeping is best-effort)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.debug(f"Could not remove {path}: {e}")
