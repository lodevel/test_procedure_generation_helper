"""Project/session controller carved out of the Studio workspace.

Owns the project lifecycle: the new/open/recent project flows plus the
task-#39 live-refresh plumbing (config.json + bundle watchers, the
edge-triggered change detectors, the debounced reload funnel and the
``handle_config_change`` hot-reload funnel). Method bodies were moved
verbatim from ``main_window.StudioWorkspace`` (``self`` -> ``mw``); the
workspace's menus, footer link and watcher signals connect straight to
them.

The watcher/funnel slice is deliberately MODULE-LEVEL functions over
the passed window (not ``ProjectController`` methods):
``tests/test_bundle_hot_reload.py`` drives them on a bare
``_WatcherHarness`` carrying only the watcher state, so these bodies
must bind late through the object they are handed and never through a
stored controller attribute.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from procedo.core import app_settings, display_snapshot
from procedo.ui.new_project_dialog import NewProjectDialog
from procedo.core.off_gui import run_off_gui
from procedo.core.paths import exists_quiet
from procedo.engine_client import engine

from .. import _project_local_state

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

log = logging.getLogger(__name__)


class ProjectController(QObject):
    """Project new/open/recents flows for the Studio workspace (a QObject: a worker's answer
    lands on its slots)."""

    def __init__(self, window: StudioWorkspace) -> None:
        super().__init__(window)
        self._mw = window
        self._recent_asked: Optional[str] = None

    def on_new_project(self):
        """Handle new project creation."""
        self._forget_recent()
        mw = self._mw
        # Shared, bundle-backed New Project — the SAME dialog the main app
        # uses: creates dirs + venv + bundle ref + starter test, so
        # editor-made projects are openable/runnable by the main app.
        dialog = NewProjectDialog(mw)
        if dialog.exec() != QDialog.Accepted:
            return

        # The dialog seeded the template and wrote the identity (apply_project_settings).
        project_path = Path(dialog.project_location) / dialog.project_name

        # Point the editor's navigation model at the freshly-created project.
        mw.project_manager.set_project_root(project_path)
        app_settings.add_recent_project(str(project_path))

        # Now initialize the UI with the new project
        mw.workspace_widget._load_test_list()
        mw.workspace_widget.new_test_btn.setEnabled(True)

        # Detect rules (will prompt user if not found)
        mw.project_manager.detect_rules_root()

        # Switch task configurations to the freshly-created project so
        # any workflow saves land in the new ``config.json``. The
        # registered reload callback refreshes button labels.
        mw.task_config_manager.reload(mw.project_manager.project_root)

        # Update the footer (project, rules)
        mw._update_project_rules_indicators()
        refresh_parser_buttons(mw)
        watch_project_config(mw)

        # Show the Workspace column if hidden
        mw.toggle_workspace_action.setChecked(True)
        mw.llm_server.restart_for_project()

        # Show success message
        QMessageBox.information(
            mw,
            "Project Created",
            f"Project created successfully at:\n{project_path}\n\n"
            "You can now create test folders using the Workspace tab.",
        )

    def refresh_after_open(self):
        """Common UI refresh after a project root is set (open / recent)."""
        mw = self._mw
        mw.workspace_widget._load_test_list()
        mw.workspace_widget.new_test_btn.setEnabled(True)
        mw.project_manager.detect_rules_root()
        mw.task_config_manager.reload(mw.project_manager.project_root)
        mw._update_project_rules_indicators()
        refresh_parser_buttons(mw)
        watch_project_config(mw)
        mw.toggle_workspace_action.setChecked(True)
        mw.llm_server.restart_for_project()

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

    def _forget_recent(self) -> None:
        """A recent still being looked for no longer opens; its footer line goes."""
        if self._recent_asked is not None:
            self._recent_asked = None
            self._mw.footer.show_message("")

    def on_open_recent(self, path_str):
        """Open a project from the recent list. Its folder is looked for off the GUI thread
        (on a share that is down that takes ~20 s); only the last one asked for opens."""
        self._recent_asked = path_str
        self._mw.footer.show_message(f"Looking for {path_str}…")
        run_off_gui(
            lambda: (path_str, _folder_there(path_str)),
            self._on_recent_checked,
            name="recent-project-check",
        )

    def _on_recent_checked(self, answer) -> None:
        path_str, there = answer
        if path_str != self._recent_asked:
            return  # another recent was asked for since
        self._recent_asked = None
        mw = self._mw
        mw.footer.show_message("")
        if not there:
            QMessageBox.warning(
                mw,
                "Project Not Found",
                f"The project folder does not exist or cannot be reached:\n{path_str}",
            )
            return
        path = Path(path_str)
        if mw.project_manager.set_project_root(path):
            app_settings.add_recent_project(str(path))
            self.refresh_after_open()

    def on_open_project(self):
        """Handle open project action."""
        self._forget_recent()
        mw = self._mw
        path = QFileDialog.getExistingDirectory(
            mw,
            "Select Project Root",
            str(Path.home()),
        )

        if not path:
            return

        project_path = Path(path)

        # Try to set as project root
        if mw.project_manager.set_project_root(project_path):
            app_settings.add_recent_project(str(project_path))
            self.refresh_after_open()
        else:
            # Maybe user selected a test folder directly?
            detected_root = mw.project_manager.detect_project_from_test_folder(project_path)
            if detected_root:
                mw.project_manager.set_project_root(detected_root)
                mw.workspace_widget._load_test_list()
                mw.workspace_widget.new_test_btn.setEnabled(True)
                mw.project_manager.detect_rules_root()

                # Switch task configurations to the detected project root.
                mw.task_config_manager.reload(mw.project_manager.project_root)

                # Update the footer (project, rules)
                mw._update_project_rules_indicators()
                refresh_parser_buttons(mw)
                watch_project_config(mw)

                # Show the Workspace column
                mw.toggle_workspace_action.setChecked(True)
                mw.llm_server.restart_for_project()
            else:
                QMessageBox.warning(
                    mw,
                    "Invalid Project",
                    "Selected folder does not appear to be a valid project root.\n\n"
                    "A valid project should contain a 'tests/' or 'config/' folder.",
                )


def _folder_there(path_str: str) -> bool:
    """*path_str* is a folder; False too when it cannot be reached (a share that is down)."""
    try:
        return Path(path_str).is_dir()
    except OSError:
        return False


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
    config_dir = mw.project_manager.get_config_dir()
    new_path = (config_dir / "config.json") if config_dir else None

    if mw._watched_config_path is not None:
        mw._config_watcher.removePath(str(mw._watched_config_path))
        mw._watched_config_path = None
    if mw._watched_config_dir is not None:
        mw._config_watcher.removePath(str(mw._watched_config_dir))
        mw._watched_config_dir = None

    if new_path and exists_quiet(new_path):
        mw._config_watcher.addPath(str(new_path))
        mw._watched_config_path = new_path
    # Watch the parent dir regardless of file presence — survives
    # the brief delete-recreate window of ProjectConfigDialog commits.
    if config_dir is not None and exists_quiet(config_dir):
        mw._config_watcher.addPath(str(config_dir))
        mw._watched_config_dir = config_dir

    # Task #39: arm the bundle-dir watches and seed the change
    # detectors so the first event compares against a baseline
    # instead of always firing.
    watch_project_bundle(mw)
    bundle_signature_changed(mw)
    bundle_stamp_changed(mw)


def watch_project_bundle(mw) -> None:
    """Watch <project>/bundle plus the project root itself (task #39).

    A bundle import atomically swaps the whole bundle/ directory
    (os.replace), which drops any watch on the old dir — the
    project-root watch catches the swap so we can re-arm. Mirrors
    the main app's display_snapshot_watcher pattern. Safe to call
    repeatedly; removes previous watches first.
    """
    for old in (mw._watched_bundle_dir, mw._watched_project_root):
        if old is not None:
            mw._config_watcher.removePath(str(old))
    mw._watched_bundle_dir = None
    mw._watched_project_root = None

    root = getattr(mw.project_manager, "project_root", None)
    if root is None:
        return
    root = Path(root)
    if exists_quiet(root):
        mw._config_watcher.addPath(str(root))
        mw._watched_project_root = root
    bundle_dir = root / "bundle"
    if exists_quiet(bundle_dir):
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
