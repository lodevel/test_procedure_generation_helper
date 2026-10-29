"""Reading a project for the Studio window, OFF the GUI thread: plain data in, plain data out.

Opening a project on a network share read it on the GUI thread — every test's files stat'ed,
read and hashed for its row, one test after another, then the bundle, the configuration and
the rules again for the footer, the watchers and the task buttons — and the window froze for
seconds with nothing on screen. Everything here is file work: no widget, no Qt object. The
window (``controllers.project_controller``) runs :func:`read` on a worker and paints from what
comes back, as Bench does (``procedo.core.project_load``); a cancelled read raises
:class:`Cancelled` at the next test and what it read is thrown away.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from procedo.core import display_snapshot, file_cache, project_bundle
from procedo.core.paths import exists_quiet, is_dir_quiet

from .. import _project_local_state
from ..llm import validator_loop_settings
from ..pack_ops import pack_parsers
from . import test_sync
from .project_manager import ProjectManager, RuleDoc, TestFolderInfo
from .task_config import TaskConfigManager

#: One test's row: the folder as it is on disk, and which of its tracked files are out of sync.
Row = tuple[TestFolderInfo, tuple[str, ...]]


class Cancelled(Exception):
    """The open was cancelled: what was read so far is thrown away."""


@dataclass(frozen=True)
class Looked:
    """What a folder the user picked turned out to be."""

    there: bool  # the folder answers (False too for a share that is down)
    root: Path | None = None  # the project it names: itself, or the project of a test folder


@dataclass(frozen=True)
class Footer:
    """What the footer shows of the project: its rules, their documents, its bundle, its
    validator."""

    rules_root: Path | None
    rule_docs: list[RuleDoc]
    bundle_name: str
    validator_on: bool
    validator_attempts: int


@dataclass(frozen=True)
class Watched:
    """What the window's watchers are armed on (None: not there) and their first baselines:
    the bundle's identity and the install record's time (``has_stamp`` False: no record)."""

    config_file: Path | None
    config_dir: Path | None
    root: Path | None
    bundle_dir: Path | None
    signature: tuple | None
    has_stamp: bool
    stamp_mtime: int | None


@dataclass
class Opened:
    """A project read from disk, ready to be shown (:func:`read`)."""

    rows: list[Row]
    rules_root: Path | None  # the rules folder for the project; None: no rules
    footer: Footer
    watched: Watched


def look(pm: ProjectManager, path: Path) -> Looked:
    """Whether *path* is there and which project it names (``ProjectManager.project_root_of``).
    On a share that is down the first look takes ~20 s: never on the GUI thread."""
    if not is_dir_quiet(path):
        return Looked(there=False)
    return Looked(there=True, root=pm.project_root_of(path))


def read(
    pm: ProjectManager,
    tasks: TaskConfigManager,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[str], None] = lambda _text: None,
) -> Opened:
    """What the window shows of the open project (``pm.project_root``, set already): its tests'
    rows, its rules, its footer, what its watchers look at; its task configuration is loaded
    into *tasks* (the window announces it once shown). The project's engine is started
    meanwhile — its first ask resolves the venv and spawns the process, seconds on a share —
    so the window's own asks find it warm."""
    root = pm.project_root
    warming = threading.Thread(target=lambda: _warm(root), name="project-read-engine", daemon=True)
    warming.start()
    try:
        progress("Reading the tests…")
        rows = test_rows(pm, cancelled, progress)
        progress("Reading the configuration…")
        rules_root = pm.rules_location()
        validator_loop_settings.forget(root)  # an open reads its configuration afresh
        tasks.load(root)
        opened = Opened(rows, rules_root, footer_facts(pm, rules_root), watched(root))
    finally:
        warming.join()
    return opened


def _warm(root: Path) -> None:
    """Start the project's engine, if this PC has its environment; when it has none, the
    window's display asks are refused at once (``display_snapshot.environment_missing``)."""
    if display_snapshot.look_for_environment(root):
        pack_parsers.is_available(root)


def blank(pm: ProjectManager) -> Opened:
    """No project open (``pm.project_root`` None): what the window shows then. Reads nothing."""
    return Opened([], None, footer_facts(pm, None), watched(None))


def test_rows(
    pm: ProjectManager,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[str], None] = lambda _text: None,
) -> list[Row]:
    """Every test's row, read several tests at a time (on a share each test is a dozen round
    trips, answered slowly but several at once); "Reading the tests · N of M" as each lands.
    The Tests column paints from it."""
    patterns = pm.get_equipment_patterns()
    folders = pm.test_folders()
    rows: list[Row] = [(TestFolderInfo(folder, folder.name), ()) for folder in folders]
    done, said = itertools.count(1), threading.Lock()

    def read_one(index: int) -> None:
        if cancelled():
            raise Cancelled
        folder = folders[index]
        rows[index] = (TestFolderInfo.of(folder), test_sync.stale_artifacts(folder, patterns))
        with said:  # counted and said in one step: the count shown only ever grows
            progress(f"Reading the tests · {next(done)} of {len(folders)}")

    file_cache.in_parallel(read_one, range(len(folders)))
    return rows


def footer_facts(pm: ProjectManager, rules_root: Path | None) -> Footer:
    """What the footer shows of the open project with *rules_root* as its rules."""
    root = pm.project_root
    return Footer(
        rules_root=rules_root,
        rule_docs=pm.rule_docs_in(rules_root),
        bundle_name=bundle_name(root),
        validator_on=validator_loop_settings.is_enabled(root),
        validator_attempts=validator_loop_settings.max_attempts(root),
    )


def bundle_name(project_root: Path | None) -> str:
    """The project's active bundle as Studio names it (``baseline 2.8.1``), "" when none."""
    bundle = project_bundle.read_active_bundle(Path(project_root)) if project_root else None
    return f"{bundle.id} {bundle.version}" if bundle else ""


def watched(root: Path | None) -> Watched:
    """What the window's watchers look at in the project *root* (None: none) — its
    ``config.json`` and ``config/``, the root itself, its ``bundle/`` — and their baselines."""
    if root is None:
        return Watched(None, None, None, None, None, False, None)
    root = Path(root)
    config_dir = root / "config" if exists_quiet(root / "config") else None
    config_file = (
        config_dir / "config.json"
        if config_dir is not None and exists_quiet(config_dir / "config.json")
        else None
    )
    there = root if exists_quiet(root) else None
    stamp = _project_local_state.install_record(there)
    try:
        mtime = stamp.stat().st_mtime_ns if stamp is not None else None
    except OSError:
        mtime = None
    return Watched(
        config_file=config_file,
        config_dir=config_dir,
        root=there,
        bundle_dir=root / "bundle" if exists_quiet(root / "bundle") else None,
        signature=display_snapshot.bundle_key(root),
        has_stamp=stamp is not None,
        stamp_mtime=mtime,
    )
