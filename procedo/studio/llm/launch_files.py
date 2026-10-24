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
# What every Studio process shared in the launch dir before each got its own files.
_LEGACY_FILES = ("opencode.pid", "opencode.json", "run_skill_universe.json")


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


def remove_launch_files(launch_dir, owner: Optional[int] = None) -> None:
    """Delete *owner*'s (default THIS process's) launch files in *launch_dir* but its pid
    file: that record goes only once its server is gone (the server manager's stop / sweep)."""
    keep = launch_file_path(launch_dir, PID_FILE, owner).name
    for path in Path(launch_dir).glob(launch_file_path(launch_dir, "*", owner).name):
        if path.name != keep:
            unlink_quietly(path)


def remove_legacy_launch_files(launch_dir) -> None:
    """Delete the files every Studio process shared in *launch_dir* before each got its own
    (OpenCode would also read a stale ``opencode.json`` there as the cwd's project config)."""
    for name in _LEGACY_FILES:
        unlink_quietly(Path(launch_dir) / name)


def unlink_quietly(path: Path) -> None:
    """Delete *path* if it is there; a failure is logged (launch-dir bookkeeping is best-effort)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.debug(f"Could not remove {path}: {e}")
