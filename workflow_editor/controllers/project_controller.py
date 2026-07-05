"""Project/session controller carved out of MainWindow.

Owns the project lifecycle: the new/open/recent project flows plus the
task-#39 live-refresh plumbing (config.json + bundle watchers, the
edge-triggered change detectors, the debounced reload funnel and the
``handle_config_change`` hot-reload funnel). Method bodies were moved
verbatim from ``main_window.MainWindow`` (``self`` -> ``mw``);
MainWindow keeps thin delegating methods for every name its tests and
Qt connections pin.

The watcher/funnel slice is deliberately MODULE-LEVEL functions over
the passed window (not ``ProjectController`` methods):
``tests/test_bundle_hot_reload.py`` lifts the corresponding MainWindow
delegates UNBOUND onto a bare ``_WatcherHarness`` carrying only the
watcher state, so these bodies must late-bind through the object they
are handed and never through a stored controller attribute.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from .. import _host_services

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)


class ProjectController:
    """Project new/open/recents flows for MainWindow."""

    def __init__(self, window: MainWindow) -> None:
        self._mw = window

    def on_new_project(self):
        """Handle new project creation."""
        mw = self._mw
        # Shared, bundle-backed New Project — the SAME dialog the main app
        # uses: creates dirs + venv + bundle ref + starter test, so
        # editor-made projects are openable/runnable by the main app.
        from project_services.new_project_dialog import NewProjectDialog
        from project_services import config_manager

        dialog = NewProjectDialog(mw)
        if dialog.exec() != QDialog.Accepted:
            return

        project_path = Path(dialog.project_location) / dialog.project_name

        selected_cfg = dialog.selected_config
        if selected_cfg:
            config_manager.seed_project_from_config(selected_cfg, project_path)
            config_manager.set_last_used_config(selected_cfg)

        config_name = getattr(dialog, "config_name", "") or dialog.project_name
        if config_name:
            config_dir = project_path / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            origin = config_manager.read_config_section(config_dir, "origin")
            origin["config_name"] = config_name
            config_manager.write_config_section(config_dir, "origin", origin)

        # Point the editor's navigation model at the freshly-created project.
        mw.project_manager.set_project_root(project_path)
        _host_services.note_recent_project(str(project_path))

        # Now initialize the UI with the new project
        mw.workspace_widget._load_test_list()
        mw.workspace_widget.new_test_btn.setEnabled(True)

        # Detect rules (will prompt user if not found)
        mw.project_manager.detect_rules_root()

        # Switch task configurations to the freshly-created project so
        # any workflow saves land in the new ``config.json``. The
        # registered reload callback refreshes button labels.
        mw.task_config_manager.reload(mw.project_manager.project_root)

        # Update status bar indicators
        mw._update_project_rules_indicators()
        mw.text_json_tab.refresh_parser_button()
        mw.text_only_tab.refresh_parser_button()
        mw.json_code_tab.refresh_code_parser_button()
        mw._watch_project_config()

        # Show workspace dock if hidden
        if mw.workspace_dock.isHidden():
            mw.workspace_dock.show()
        mw._restart_server_for_project()

        # Show success message
        QMessageBox.information(
            mw,
            "Project Created",
            f"Project created successfully at:\n{project_path}\n\n"
            "You can now create test folders using the Workspace tab."
        )

    def refresh_after_open(self):
        """Common UI refresh after a project root is set (open / recent)."""
        mw = self._mw
        mw.workspace_widget._load_test_list()
        mw.workspace_widget.new_test_btn.setEnabled(True)
        mw.project_manager.detect_rules_root()
        mw.task_config_manager.reload(mw.project_manager.project_root)
        mw._update_project_rules_indicators()
        mw.text_json_tab.refresh_parser_button()
        mw.text_only_tab.refresh_parser_button()
        mw.json_code_tab.refresh_code_parser_button()
        mw._watch_project_config()
        if mw.workspace_dock.isHidden():
            mw.workspace_dock.show()
        mw._restart_server_for_project()

    def rebuild_open_recent_menu(self):
        mw = self._mw
        # Repopulate from the SHARED recent list (same app_settings the main
        # app writes), so recents are unified across both apps.
        aps = _host_services.load_optional("project_services.app_settings")
        mw._open_recent_menu.clear()
        recent = aps.load_app_settings().get("recent_projects", []) if aps else []
        if not recent:
            placeholder = mw._open_recent_menu.addAction("No recent projects")
            placeholder.setEnabled(False)
            return
        for path_str in recent:
            action = mw._open_recent_menu.addAction(path_str)
            action.triggered.connect(
                lambda checked=False, p=path_str: mw._on_open_recent(p)
            )

    def on_open_recent(self, path_str):
        """Open a project from the recent list."""
        mw = self._mw
        path = Path(path_str)
        if not path.is_dir():
            QMessageBox.warning(
                mw, "Project Not Found",
                f"The project folder no longer exists:\n{path_str}")
            return
        if mw.project_manager.set_project_root(path):
            _host_services.note_recent_project(str(path))
            mw._refresh_after_open()

    def on_open_project(self):
        """Handle open project action."""
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
            _host_services.note_recent_project(str(project_path))
            mw._refresh_after_open()
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

                # Update status bar indicators
                mw._update_project_rules_indicators()
                mw.text_json_tab.refresh_parser_button()
                mw.text_only_tab.refresh_parser_button()
                mw.json_code_tab.refresh_code_parser_button()
                mw._watch_project_config()

                # Show workspace dock
                if mw.workspace_dock.isHidden():
                    mw.workspace_dock.show()
                mw._restart_server_for_project()
            else:
                QMessageBox.warning(
                    mw,
                    "Invalid Project",
                    "Selected folder does not appear to be a valid project root.\n\n"
                    "A valid project should contain a 'tests/' or 'config/' folder."
                )


# --- task-#39 watcher / config-change funnel -------------------------------
# Module-level on purpose: MainWindow's thin delegates for these names are
# lifted unbound onto tests' _WatcherHarness (see module docstring), so the
# bodies bind late through ``mw`` and keep no controller state.


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

    if new_path and new_path.exists():
        mw._config_watcher.addPath(str(new_path))
        mw._watched_config_path = new_path
    # Watch the parent dir regardless of file presence — survives
    # the brief delete-recreate window of ProjectConfigDialog commits.
    if config_dir is not None and config_dir.exists():
        mw._config_watcher.addPath(str(config_dir))
        mw._watched_config_dir = config_dir

    # Task #39: arm the bundle-dir watches and seed the change
    # detectors so the first event compares against a baseline
    # instead of always firing.
    mw._watch_project_bundle()
    mw._bundle_signature_changed()
    mw._bundle_stamp_changed()


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
    if root.exists():
        mw._config_watcher.addPath(str(root))
        mw._watched_project_root = root
    bundle_dir = root / "bundle"
    if bundle_dir.exists():
        mw._config_watcher.addPath(str(bundle_dir))
        mw._watched_bundle_dir = bundle_dir


def bundle_signature_changed(mw) -> bool:
    """True when the bundle's identity moved since the last check.

    Signature = mtimes of the bundle dir + bundle.json +
    defaults.json (None per missing entry). An import swaps the
    whole dir, so all three change; unrelated project-root events
    (test folders created, etc.) leave the signature alone — the
    noise filter that keeps root-watch events from causing reload
    storms (task #39, mirrors display_snapshot_watcher's
    bundle-key check).
    """
    root = getattr(mw.project_manager, "project_root", None)
    if root is None:
        sig = None
    else:
        bundle_dir = Path(root) / "bundle"
        parts = []
        for p in (bundle_dir, bundle_dir / "bundle.json",
                  bundle_dir / "defaults.json"):
            try:
                parts.append(p.stat().st_mtime_ns)
            except OSError:
                parts.append(None)
        sig = tuple(parts)
    changed = sig != mw._bundle_signature
    mw._bundle_signature = sig
    return changed


def bundle_stamp_changed(mw) -> bool:
    """True when config/.bundle_installed.json moved since the last
    check. The stamp is written as the final step of every bundle
    install — a change means an import completed even if the
    bundle-dir events were missed mid-swap (task #39
    belt-and-braces)."""
    if mw._watched_config_dir is None:
        return False
    stamp = mw._watched_config_dir / ".bundle_installed.json"
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
    buttons via _handle_config_change."""
    mw._update_project_rules_indicators()
    mw._handle_config_change()


def on_config_file_changed(mw, path: str) -> None:
    """Refresh parser- and workflow-driven UI when config.json changes.

    Some editors atomic-write (delete + recreate), which silently
    drops the watch — re-add the path defensively after each event.

    Phase 4 hot-reload: when the parent app's ProjectConfigDialog
    commits a workflows edit, reload the TaskConfigManager so the
    running workflow editor's button labels + validator rows
    reflect the change without restart. The reload-callback chain
    fires ``refresh_all_button_labels``, which itself calls
    ``rebuild_validator_buttons`` on every tab (registered in
    Phase 2/3).
    """
    mw._handle_config_change()
    p = Path(path)
    if p.exists() and str(p) not in mw._config_watcher.files():
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
        mw._watch_project_bundle()
        if mw._bundle_signature_changed():
            mw._bundle_change_timer.start()
        return
    # Belt-and-braces (task #39): the install stamp lands in
    # config/ as the last step of an import — a changed stamp is a
    # bundle change even when the bundle-dir events were missed.
    if mw._bundle_stamp_changed():
        mw._bundle_change_timer.start()
    if mw._watched_config_path is None:
        return
    # If the file watch dropped during a rmtree window, re-add it
    # now that the directory event tells us the file is back.
    file_str = str(mw._watched_config_path)
    if (mw._watched_config_path.exists()
            and file_str not in mw._config_watcher.files()):
        mw._config_watcher.addPath(file_str)
        mw._handle_config_change()


def handle_config_change(mw) -> None:
    """Single funnel for hot-reload work — called by both the
    file-change and directory-change paths so logic stays in one
    place."""
    mw.text_json_tab.refresh_parser_button()
    mw.text_only_tab.refresh_parser_button()
    mw.json_code_tab.refresh_code_parser_button()
    try:
        project_root = mw.project_manager.project_root
        if project_root is not None:
            mw.task_config_manager.reload(project_root)
    except Exception:  # never let the watcher die on a load error
        log.exception("Hot-reload of TaskConfigManager failed")
    # Also refresh the chat panel's validator-status indicator so
    # auto-correct checkbox + dot reflect the new state.
    try:
        current = mw.tab_widget.currentWidget()
        ctx = getattr(current, "tab_context", None)
        if ctx is not None:
            mw.dock.chat_panel._refresh_validator_ui_for_context(ctx)
    except Exception:
        log.debug("post-config-change chat-panel refresh failed", exc_info=True)
