"""Project/session controller carved out of the Studio workspace.

Owns the project lifecycle — the new / open / recent project flows, run as ONE attempt at a
time behind the opening page — plus the task-#39 live-refresh plumbing (config.json + bundle
watchers, the edge-triggered change detectors, the debounced reload funnel and the
``handle_config_change`` hot-reload funnel).

**Opening a project without freezing the window** (Bench's ``project_opening``, for Studio).
A project on a network share took seconds to read, all of it on the GUI thread — every test's
files hashed for its row, the bundle read six times, the engine started by the first of ten
availability asks — and the window froze with nothing on screen. Now every read runs on a
worker (``core.project_open``) and the window shows "Opening <name>…" with Cancel
(``procedo.ui.opening_page``). The steps, one path for every way in (the launch on a project,
File > Open, Open Recent, New Project, a wizard's end):

1. **LOOK** (worker) — is the folder there, which project it names (itself, or the project of
   the test folder picked).
2. **Ask** — the open test's unsaved changes, the windows working on the project
   (:func:`close_project_windows`); a Cancel keeps the open project, nothing touched.
3. **Switch** — the root is the new project's, the old test closes, the recent is noted.
4. **READ** (worker) — every test's row, several at a time, and the rules; the project's
   engine is started meanwhile, so the window's own asks find it warm.
5. **Show** — the window built from what was read (:func:`install`).

A failed read and a Cancel at step 4 end the same way: no project open, the window usable.
ONE attempt at a time, worker included: a new open is refused until the last worker has
returned, and a superseded worker's answer is dropped (the attempt token).

The watcher/funnel slice is deliberately MODULE-LEVEL functions over the passed window (not
``ProjectController`` methods): ``tests/test_bundle_hot_reload.py`` drives them on a bare
``_WatcherHarness`` carrying only the watcher state, so these bodies must bind late through the
object they are handed and never through a stored controller attribute.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QWidget

from procedo.core import app_settings, display_snapshot, project_local_state
from procedo.core.off_gui import answer_to, run_off_gui
from procedo.core.paths import exists_quiet
from procedo.engine_client import engine
from procedo.ui.new_project_dialog import NewProjectDialog
from procedo.ui.opening_page import OpeningPage

from .. import _project_local_state
from ..authoring.skill_menu import close_project_windows
from ..core import project_open
from ..llm import validator_loop_settings

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

log = logging.getLogger(__name__)

#: What a refused second open says (the first one's worker has not returned yet).
STILL_OPENING = "Still finishing the last project open — try again in a moment."
#: The page's hint once the project switched: what a Cancel does from there on.
CANCEL_HINT = "Cancel leaves no project open."


@dataclass(eq=False)
class _Attempt:
    """One open: its answers are heard only while it is the current one."""

    path: Path
    reading: bool = False  # past the switch: a Cancel now leaves no project open
    cancelled: threading.Event = field(default_factory=threading.Event)


class ProjectController(QObject):
    """Project new/open/recents flows for the Studio workspace, one attempt at a time behind
    :attr:`page` (a QObject: a worker's answer lands on its slots)."""

    def __init__(self, window: StudioWorkspace) -> None:
        super().__init__(window)
        self._mw = window
        self._attempt: _Attempt | None = None
        self._worker: threading.Thread | None = None  # the last attempt's, until it returns
        self._previous: QWidget | None = None  # what the middle showed before the page
        self.page = OpeningPage()  # placed in the middle by shell.build_frame
        self.page.cancel.clicked.connect(self.cancel)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)  # an exit never shows a half-read project

    @property
    def working(self) -> bool:
        """Whether an attempt is on, or the last one's worker has not returned yet."""
        return self._attempt is not None or (self._worker is not None and self._worker.is_alive())

    # -- the ways in ----------------------------------------------------------------------

    def on_new_project(self):
        """Handle new project creation."""
        if self.refused() or not self._may_leave_project():
            return  # asked before the dialog: a Cancel creates nothing
        mw = self._mw
        # Shared, bundle-backed New Project — the SAME dialog the main app
        # uses: creates dirs + venv + bundle ref + starter test, so
        # editor-made projects are openable/runnable by the main app.
        dialog = NewProjectDialog(mw)
        if dialog.exec() != QDialog.Accepted:
            return

        # The dialog seeded the template and wrote the identity (apply_project_settings).
        project_path = Path(dialog.project_location) / dialog.project_name
        self._start(_Attempt(project_path))
        self._switch(project_path)
        QMessageBox.information(
            mw,
            "Project Created",
            f"Project created successfully at:\n{project_path}\n\n"
            "You can now create test folders using the Workspace tab.",
        )

    def on_open_project(self):
        """File > Open Project: the folder picked (a project, or a test folder of one) opens."""
        if self.refused():
            return
        path = QFileDialog.getExistingDirectory(self._mw, "Select Project Root", str(Path.home()))
        if path:
            self.open(Path(path))

    def on_open_recent(self, path_str):
        """Open a project from the recent list (its folder looked for off the GUI thread: on a
        share that is down that takes ~20 s)."""
        self.open(Path(path_str))

    def open(self, path: Path) -> None:
        """Open the project at *path* (the module docstring: the order)."""
        if self.refused():
            return
        pm = self._mw.project_manager
        attempt = self._start(_Attempt(path))
        self._run(lambda: project_open.look(pm, attempt.path), self._on_looked, "project-check")

    def refresh_after_open(self):
        """Read the open project (its root set already) and show it: the launch on a project,
        a wizard's end; an attempt on continues with the read."""
        if self._attempt is None:
            if self.refused():
                return
            self._start(_Attempt(self._mw.project_manager.project_root))
        self._read()

    def cancel(self) -> None:
        """Abandon the current attempt: before the switch the open project stays; after it, no
        project is open. The worker still running is dropped with its answer."""
        attempt = self._attempt
        if attempt is None:
            return
        attempt.cancelled.set()
        self._finish()
        if attempt.reading:
            self._blank()

    def _may_leave_project(self) -> bool:
        """The open project may be left: the open test may close (its unsaved changes asked
        about, as when another test opens) and the windows working on the project closed
        (:func:`close_project_windows`). False when a question was cancelled."""
        mw = self._mw
        return mw._document_controller.may_leave_test() and close_project_windows(mw)

    # -- the steps --------------------------------------------------------------------------

    def _on_looked(self, answer: tuple) -> None:
        attempt, looked = answer
        if attempt is not self._attempt:
            return
        mw = self._mw
        if isinstance(looked, Exception):
            self._failed(attempt, looked)
        elif not looked.there:
            self._finish()
            QMessageBox.warning(
                mw,
                "Project Not Found",
                f"The project folder does not exist or cannot be reached:\n{attempt.path}",
            )
        elif looked.root is None:
            self._finish()
            _warn_invalid_project(mw)
        elif looked.root != mw.project_manager.project_root and not self._may_leave_project():
            self._finish()
        else:
            self._switch(looked.root)

    def _switch(self, root: Path) -> None:
        """The attempt's project is *root* (a project; the open one may be left): the root is
        set, the old test closes, the recent is noted, then the project is read."""
        mw = self._mw
        leaving = root != mw.project_manager.project_root
        mw.project_manager.use_project_root(root)
        if leaving:
            mw._document_controller.leave_test()
        app_settings.add_recent_project(str(root))
        self._read()

    def _read(self) -> None:
        attempt, mw = self._attempt, self._mw
        pm, tasks = mw.project_manager, mw.task_config_manager
        attempt.reading = True
        self.page.step.setText("Reading the project…")
        self.page.show_hint(CANCEL_HINT)
        progress = self._progress(attempt)
        self._run(
            lambda: project_open.read(pm, tasks, attempt.cancelled.is_set, progress),
            self._on_read,
            "project-read",
        )

    def _on_progress(self, answer: tuple) -> None:
        attempt, text = answer
        if attempt is self._attempt:
            self.page.step.setText(text)

    def _progress(self, attempt: _Attempt) -> Any:
        """What the read says of its progress, shown on the page while *attempt* is on."""
        send = answer_to(self._on_progress)
        return lambda text: send((attempt, text))

    def _on_read(self, answer: tuple) -> None:
        attempt, opened = answer
        if attempt is not self._attempt:
            return
        if isinstance(opened, Exception):
            self._failed(attempt, opened)
        else:
            self._finish()
            install(self._mw, opened)

    def _failed(self, attempt: _Attempt, error: Exception) -> None:
        self._finish()
        if attempt.reading:
            self._blank()
        QMessageBox.critical(
            self._mw,
            "Project Not Opened",
            f"{attempt.path.name} could not be opened:\n\n{error}",
        )

    def _blank(self) -> None:
        """No project open (a cancelled or failed read): the window usable, Open and New both
        work."""
        mw = self._mw
        mw.project_manager.use_project_root(None)
        mw.task_config_manager.load(None)
        install(mw, project_open.blank(mw.project_manager))

    # -- the page ---------------------------------------------------------------------------

    def refused(self) -> bool:
        """One attempt at a time — until the last worker has really returned."""
        if not self.working:
            return False
        self._mw.footer.show_message(STILL_OPENING)
        return True

    def _start(self, attempt: _Attempt) -> _Attempt:
        """Show the page for *attempt*; the window behind it is out of reach meanwhile."""
        mw = self._mw
        self._attempt = attempt
        self._previous = mw.middle.currentWidget()
        self.page.show_attempt(f"Opening {attempt.path.name}…", attempt.path, "")
        mw.middle.setCurrentWidget(self.page)
        for widget in self._out_of_reach():
            widget.setEnabled(False)
        return attempt

    def _finish(self) -> None:
        """The attempt is over, whichever way: the window usable again."""
        mw = self._mw
        self._attempt = None
        if mw.middle.currentWidget() is self.page:  # a test opened meanwhile shows its own view
            mw.middle.setCurrentWidget(self._previous)
        for widget in self._out_of_reach():
            widget.setEnabled(True)

    def _out_of_reach(self) -> list[QWidget]:
        """What may not be used while an attempt runs: the menus (another open, New Project),
        the header (Save all, Export), the Tests column (a test opened from a half-read list),
        the Assistant, the footer's project and rules links."""
        mw = self._mw
        return [
            mw.menu_bar,
            mw.header,
            mw.workspace_column,
            mw.assistant_column,
            mw.project_link,
            mw.rules_link,
        ]

    # -- plumbing ---------------------------------------------------------------------------

    def _run(self, work: Any, slot: Any, name: str) -> None:
        """``work()`` on a worker; ``slot((attempt, result or the exception))`` on this thread."""
        attempt = self._attempt

        def answer() -> tuple:
            try:
                return attempt, work()
            except project_open.Cancelled as exc:
                return attempt, exc
            except Exception as exc:  # shown to the user, never a dead thread
                log.exception("%s failed for %s", name, attempt.path)
                return attempt, exc

        self._worker = run_off_gui(answer, slot, name=name)

    def rebuild_open_recent_menu(self):
        mw = self._mw
        # Repopulate from the SHARED recent list (same app_settings the main
        # app writes), so recents are unified across both apps.
        mw._open_recent_menu.clear()
        recent = app_settings.load_app_settings().get("recent_projects", [])
        if not recent:
            placeholder = mw._open_recent_menu.addAction("No recent projects")
            placeholder.setEnabled(False)
            return
        for path_str in recent:
            action = mw._open_recent_menu.addAction(path_str)
            action.triggered.connect(lambda checked=False, p=path_str: self.on_open_recent(p))


def install(mw, opened: project_open.Opened) -> None:
    """Show the project read (*opened*; its root is the project manager's, its task
    configuration loaded): the Tests column, the rules, the task buttons, the header and
    footer, the pack buttons (the engine is warm), the watchers, the AI server — all from
    what was read. With no project (the root None): the window blank, and usable."""
    pm = mw.project_manager
    mw.workspace_widget.show_project(opened.rows)
    pm.use_rules(opened.rules_root)
    mw.task_config_manager.announce()
    mw.show_project_rules(opened.footer)
    refresh_parser_buttons(mw)
    arm_watches(mw, opened.watched)
    mw.toggle_workspace_action.setChecked(True)
    mw.llm_server.restart_for_project()


def _warn_invalid_project(mw) -> None:
    QMessageBox.warning(
        mw,
        "Invalid Project",
        "Selected folder does not appear to be a valid project root.\n\n"
        "A valid project should contain a 'tests/' or 'config/' folder.",
    )


# --- task-#39 watcher / config-change funnel -------------------------------
# Module-level on purpose: tests drive these on a bare _WatcherHarness (see the
# module docstring), so the bodies bind late through ``mw`` and keep no
# controller state.


def watch_project_config(mw) -> None:
    """Watch the active project's config.json + its parent dir.

    Removes any previous watches before adding the new ones. Safe
    to call repeatedly (e.g. after switching projects). The parent
    directory watch is the safety net for rmtree+copytree commits
    where the fileChanged signal fires on delete and we miss the
    subsequent recreate (Codex Q2).
    """
    arm_watches(mw, project_open.watched(mw.project_manager.project_root))


def arm_watches(mw, watched: project_open.Watched) -> None:
    """:func:`watch_project_config` from *watched* (``project_open.watched``, read already):
    the old watches removed, the new ones added, the change detectors seeded."""
    if mw._watched_config_path is not None:
        mw._config_watcher.removePath(str(mw._watched_config_path))
        mw._watched_config_path = None
    if mw._watched_config_dir is not None:
        mw._config_watcher.removePath(str(mw._watched_config_dir))
        mw._watched_config_dir = None

    if watched.config_file is not None:
        mw._config_watcher.addPath(str(watched.config_file))
        mw._watched_config_path = watched.config_file
    # Watch the parent dir regardless of file presence — survives
    # the brief delete-recreate window of ProjectConfigDialog commits.
    if watched.config_dir is not None:
        mw._config_watcher.addPath(str(watched.config_dir))
        mw._watched_config_dir = watched.config_dir

    # Task #39: arm the bundle-dir watches and seed the change
    # detectors so the first event compares against a baseline
    # instead of always firing.
    _rewatch_bundle(mw, watched.root, watched.bundle_dir)
    mw._bundle_signature = watched.signature
    if watched.has_stamp:
        mw._bundle_stamp_mtime = watched.stamp_mtime


def watch_project_bundle(mw) -> None:
    """Watch <project>/bundle plus the project root itself (task #39).

    A bundle import atomically swaps the whole bundle/ directory
    (os.replace), which drops any watch on the old dir — the
    project-root watch catches the swap so we can re-arm. Mirrors
    the main app's display_snapshot_watcher pattern. Safe to call
    repeatedly; removes previous watches first.
    """
    root = getattr(mw.project_manager, "project_root", None)
    root = Path(root) if root is not None else None
    _rewatch_bundle(
        mw,
        root if root is not None and exists_quiet(root) else None,
        root / "bundle" if root is not None and exists_quiet(root / "bundle") else None,
    )


def _rewatch_bundle(mw, root: Path | None, bundle_dir: Path | None) -> None:
    """Watch *root* and *bundle_dir* (None: not there) instead of what was watched."""
    for old in (mw._watched_bundle_dir, mw._watched_project_root):
        if old is not None:
            mw._config_watcher.removePath(str(old))
    mw._watched_bundle_dir = None
    mw._watched_project_root = None
    if root is not None:
        mw._config_watcher.addPath(str(root))
        mw._watched_project_root = root
    if bundle_dir is not None:
        mw._config_watcher.addPath(str(bundle_dir))
        mw._watched_bundle_dir = bundle_dir


def bundle_signature_changed(mw) -> bool:
    """True when the bundle's identity moved since the last check.

    Signature = the host's ``display_snapshot.bundle_key`` (mtimes of the
    bundle dir, bundle.json, defaults.json and the install record). An
    import swaps the whole dir, so they change; unrelated project-root
    events (test folders created, etc.) leave the signature alone — the
    noise filter that keeps root-watch events from causing reload storms
    (task #39).
    """
    root = getattr(mw.project_manager, "project_root", None)
    sig = None if root is None else display_snapshot.bundle_key(Path(root))
    changed = sig != mw._bundle_signature
    mw._bundle_signature = sig
    return changed


def bundle_stamp_changed(mw) -> bool:
    """True when the host's bundle-install record moved since the last
    check. The stamp is written as the final step of every bundle
    install — a change means an import completed even if the
    bundle-dir events were missed mid-swap (task #39
    belt-and-braces). It sits beside the machine-local venv it
    describes, not in the project, so the host is asked where
    (``_project_local_state.install_record``)."""
    stamp = _project_local_state.install_record(mw._watched_project_root)
    if stamp is None:
        return False
    try:
        mtime: Optional[int] = stamp.stat().st_mtime_ns
    except OSError:
        mtime = None
    changed = mtime != mw._bundle_stamp_mtime
    mw._bundle_stamp_mtime = mtime
    return changed


def on_bundle_change_debounced(mw) -> None:
    """Debounced funnel for bundle changes (task #39): a bundle
    imported into the open project must be live everywhere with no
    reload/reopen — refresh the rules indicators, reload
    TaskConfigManager (the reload-callback chain rebuilds task
    buttons / gating / validator rows) and regate the parser
    buttons via handle_config_change."""
    root = getattr(mw.project_manager, "project_root", None)
    if root is not None:
        project_local_state.forget_resolved(root)  # what was kept of the old bundle is read again
        engine.stop(project_root=root)  # a TOOLS of the old bundle would sit idle
    mw._update_project_rules_indicators()
    handle_config_change(mw)


def on_config_file_changed(mw, path: str) -> None:
    """Refresh parser- and workflow-driven UI when config.json changes.

    Some editors atomic-write (delete + recreate), which silently
    drops the watch — re-add the path defensively after each event.

    Phase 4 hot-reload: when the parent app's ProjectConfigDialog
    commits a workflows edit, reload the TaskConfigManager so the
    running workflow editor's button labels + validator rows
    reflect the change without restart. The reload-callback chain
    fires ``refresh_all_button_labels``, which itself calls
    ``validator_buttons.rebuild()`` on every tab (registered in
    Phase 2/3).
    """
    handle_config_change(mw)
    p = Path(path)
    if exists_quiet(p) and str(p) not in mw._config_watcher.files():
        mw._config_watcher.addPath(str(p))


def on_config_dir_changed(mw, path: str) -> None:
    """Directory-level watch fires when config.json is created/
    recreated by an external writer (e.g. ProjectConfigDialog's
    rmtree+copytree commit). Re-arms the file watch and triggers
    the same hot-reload as a direct file change. Phase 4.6.2.

    Also receives events for <project>/bundle and the project root
    (task #39): an import swaps the bundle dir atomically, dropping
    its watch — re-arm and, when the bundle signature actually
    moved, fire the debounced bundle reload."""
    p = Path(path)
    if p in (mw._watched_bundle_dir, mw._watched_project_root):
        watch_project_bundle(mw)
        if bundle_signature_changed(mw):
            mw._bundle_change_timer.start()
        return
    # Belt-and-braces (task #39): the install stamp lands in
    # config/ as the last step of an import — a changed stamp is a
    # bundle change even when the bundle-dir events were missed.
    if bundle_stamp_changed(mw):
        mw._bundle_change_timer.start()
    if mw._watched_config_path is None:
        return
    # If the file watch dropped during a rmtree window, re-add it
    # now that the directory event tells us the file is back.
    file_str = str(mw._watched_config_path)
    if exists_quiet(mw._watched_config_path) and file_str not in mw._config_watcher.files():
        mw._config_watcher.addPath(file_str)
        handle_config_change(mw)


def refresh_parser_buttons(mw) -> None:
    """Re-evaluate the Quick Parse button of the three text tabs."""
    mw.text_json_tab.refresh_parser_button()
    mw.text_only_tab.refresh_parser_button()
    mw.json_code_tab.refresh_parser_button()


def handle_config_change(mw) -> None:
    """Single funnel for hot-reload work — called by both the
    file-change and directory-change paths so logic stays in one
    place."""
    validator_loop_settings.forget(mw.project_manager.project_root)  # read again from here
    refresh_parser_buttons(mw)
    try:
        project_root = mw.project_manager.project_root
        if project_root is not None:
            mw.task_config_manager.reload(project_root)
    except Exception:  # never let the watcher die on a load error
        log.exception("Hot-reload of TaskConfigManager failed")
    # Also refresh the chat panel's validator-status indicator so
    # auto-correct checkbox + dot reflect the new state.
    try:
        current = mw.view_stack.currentWidget()
        ctx = getattr(current, "tab_context", None)
        if ctx is not None:
            mw.dock.chat_panel._refresh_validator_ui_for_context(ctx)
    except Exception:
        log.debug("post-config-change chat-panel refresh failed", exc_info=True)
