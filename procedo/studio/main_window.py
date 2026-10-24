"""
Main Window - Primary application window.

Implements the main UI structure from Section 9.
"""

from pathlib import Path
from typing import Optional
import logging
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QMenuBar, QMessageBox, QFileDialog,
)
from PySide6.QtCore import QFileSystemWatcher, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut

from procedo.core import library_dialog
from procedo.core.config_manager_dialog import ProjectConfigDialog, TemplateManagerDialog
from procedo.core.install_helper_dialog import InstallPackageDialog
from procedo.core import project_seeding
from procedo.core.paths import exists_quiet
from procedo.core.scenario_dialog import ScenarioManagerDialog

from .core import ArtifactManager, SessionState, ProjectManager
from .core.task_config import TaskConfigManager
from .llm.backend_factory import BackendFactory
from .tabs import (
    WorkspaceTab, TextOnlyTab, TextJsonTab, JsonCodeTab, TraceabilityTab
)
from .dock import DockWidget
from .dialogs import SettingsDialog
from .core.studio_settings import load_settings
from . import menus, shell
from .controllers import (
    AssistantController, DocumentController, ExportController, LLMServerController,
    ProjectController, project_controller,
)

log = logging.getLogger(__name__)


class StudioWorkspace(QWidget):
    """
    Procedo Studio's whole content, embeddable: it never assumes it owns the
    window it sits in (Bench may host it beside its run mode).

    Contains (laid out by ``shell``):
    - the header band: the open test, its Text / JSON / Code and Sync pills,
      the OpenCode pill and menu, Export, Skill chat, Save all
    - the four views (Text, Text-JSON, JSON-Code, Traceability) behind a
      segmented switch
    - the Workspace (tests) and Assistant (Chat, Findings, Raw)
      columns
    - the status line: project, rules, bundle, validator
    - ``menu_bar``, for the window that hosts it to show
    """
    
    def __init__(
        self,
        parent=None,
        project_root: Optional[Path] = None,
        test_name: Optional[str] = None,
        test_dir: Optional[Path] = None,
        llm_backend: Optional[str] = None,
        llm_profile: Optional[str] = None,
    ):
        super().__init__(parent)
        log.debug("StudioWorkspace.__init__ starting")

        # Store CLI arguments
        self._cli_project_root = project_root
        self._cli_test_name = test_name
        self._cli_test_dir = test_dir
        self._cli_llm_backend = llm_backend
        self._cli_llm_profile = llm_profile

        # Controllers carved out of this god-class: each owns one cohesive
        # slice of behavior; the menus, the shell and the signals below
        # connect straight to them.
        self._project_controller = ProjectController(self)
        self._document_controller = DocumentController(self)
        self.llm_server = LLMServerController(self)
        self._assistant_controller = AssistantController(self)
        self._export_controller = ExportController(self)

        # Initialize managers
        log.debug("Initializing managers...")
        self.project_manager = ProjectManager()
        self.artifact_manager: Optional[ArtifactManager] = None
        self.session_state: Optional[SessionState] = None
        # self.chat_history: Optional[ChatHistoryManager] = None

        # File watcher for project's config.json — drives live refresh of
        # parser-button visibility when the parent app's Config dialog (or
        # any external editor) writes the file.
        # We watch BOTH the file and its parent directory: ProjectConfigDialog
        # commits via rmtree+copytree, which deletes the file before
        # recreating it. The fileChanged signal fires on delete but
        # we can't re-add the watch while the file is missing — the
        # directoryChanged signal catches the subsequent recreate so
        # we re-arm the file watch then (Codex Phase-4 review MEDIUM Q2).
        self._config_watcher = QFileSystemWatcher(self)
        self._config_watcher.fileChanged.connect(
            lambda path: project_controller.on_config_file_changed(self, path))
        self._config_watcher.directoryChanged.connect(
            lambda path: project_controller.on_config_dir_changed(self, path))
        self._watched_config_path: Optional[Path] = None
        self._watched_config_dir: Optional[Path] = None
        # Task #39: also watch <project>/bundle (+ the project root, to
        # re-arm when an import atomically swaps the whole bundle dir)
        # so a bundle imported by the MAIN APP while this editor is open
        # goes live immediately. One import produces a burst of
        # directory events -> debounced into a single reload.
        self._watched_bundle_dir: Optional[Path] = None
        self._watched_project_root: Optional[Path] = None
        self._bundle_signature: Optional[tuple] = None
        self._bundle_stamp_mtime: Optional[int] = None
        self._bundle_change_timer = QTimer(self)
        self._bundle_change_timer.setSingleShot(True)
        self._bundle_change_timer.setInterval(300)
        self._bundle_change_timer.timeout.connect(
            lambda: project_controller.on_bundle_change_debounced(self))
        
        # Initialize task config manager pointing at the repo-shared
        # fallback. ``reload(project_root)`` is called below whenever a
        # project is loaded (CLI or interactive), switching the manager
        # into project-mode and triggering the legacy
        # ``tab_contexts.json`` → ``config.json:workflows`` migration.
        fallback_path = Path(__file__).parents[2] / "config" / "tab_contexts.json"
        self.task_config_manager = TaskConfigManager(fallback_path=fallback_path)
        
        # Initialize LLM
        self._settings = load_settings()
        log.debug(f"Settings loaded: {list(self._settings.keys())}")
        self.llm_server.init_backend()
        
        # Prompt builder and response parser
        # Setup UI
        self.menu_bar = QMenuBar()
        menus.setup_menu_bar(self)
        self._setup_central_widget()
        self._setup_workspace_dock()
        self._setup_dock()
        shell.build_frame(self)
        
        # Apply settings
        self._apply_settings()

        # Setup keyboard shortcuts
        self._setup_shortcuts()

        # Disable the views and the Assistant until a test is loaded
        self.view_column.setEnabled(False)
        self.dock.setEnabled(False)

        # Refresh button labels whenever the active project's workflow
        # config switches (after reload(project_root) lifts a project's
        # ``config.json:workflows`` overrides). Tabs are constructed
        # above so the callback can safely iterate them.
        self.task_config_manager.register_reload_callback(self.refresh_all_button_labels)

    def showEvent(self, event):
        """Handle window show event - process CLI arguments after UI is ready."""
        super().showEvent(event)
        
        # Process CLI arguments on first show only
        if hasattr(self, '_cli_args_processed'):
            return
        # Mark in-progress (re-entrancy guard, the hasattr check above) but
        # keep the prewarm gate FALSE while CLI args load: setting a project
        # root can route through init_backend / restart_for_project,
        # whose prewarm is gated on _cli_args_processed. Flip to True only AFTER
        # processing so the single explicit prewarm below is the only one (no
        # latent double-prewarm: two managers racing the same launch dir).
        self._cli_args_processed = False
        
        # Process CLI arguments to load project/test
        self._process_cli_arguments()
        self._cli_args_processed = True

        # The editor OWNS its OpenCode config (the project's opencode.json
        # is a relic): launch OpenCode from an editor-controlled dir, seeded
        # ONCE from the current project's opencode.json.
        if self.llm_server.server_manager is not None:
            # Pre-warm the server in the BACKGROUND so the first chat
            # doesn't lag (cold WSL boot + opencode serve). prewarm
            # rebuilds the derived launch config for the current project,
            # sets working_directory BEFORE start() (C3 race), registers the
            # live-targeting cleanup hook (C2), and starts on a daemon thread.
            self.llm_server.prewarm()

        # Begin polling server liveness so a crash mid-session is detected,
        # auto-recovered, and reflected in the status indicator (not just the
        # model picker). Started once the UI is up so the timer has an event
        # loop to run on.
        self.llm_server.start_health_poll()
    
    def _process_cli_arguments(self):
        """Process command-line arguments to load project and/or test."""
        try:
            # Step 1: Set project root (if provided)
            if self._cli_project_root:
                log.info(f"Loading project from CLI arg: {self._cli_project_root}")
                if not self.project_manager.set_project_root(self._cli_project_root):
                    log.error(f"Failed to set project root: {self._cli_project_root}")
                    return

                # Switch task configurations to the CLI-supplied project.
                self.task_config_manager.reload(self.project_manager.project_root)

                # Update workspace widget
                self.workspace_widget._load_test_list()
                self.workspace_widget.new_test_btn.setEnabled(True)

                # The project's rules (its active bundle's)
                self.project_manager.detect_rules_root()
                self._update_project_rules_indicators()
                # Task #39: arm the config + bundle watchers for the
                # CLI-supplied project (the main-app launch path).
                project_controller.watch_project_config(self)
            
            # Step 2: Determine which test to open
            test_dir_to_open = None
            
            if self._cli_test_dir:
                # Direct test path (highest priority)
                test_dir_to_open = self._cli_test_dir
                log.info(f"Opening test from --test-dir: {test_dir_to_open}")
            elif self._cli_project_root and self._cli_test_name:
                # Test name under project root
                test_dir_to_open = self._cli_project_root / "tests" / self._cli_test_name
                log.info(f"Opening test from --test-name: {test_dir_to_open}")
            
            # Step 3: Open the test if determined
            if test_dir_to_open and exists_quiet(test_dir_to_open):
                # Ensure project is set if not already
                if not self.project_manager.project_root and self._cli_project_root:
                    self.project_manager.set_project_root(self._cli_project_root)
                
                # Open the test
                self.workspace_widget.test_opened.emit(test_dir_to_open)
                log.info(f"Test opened from CLI args: {test_dir_to_open}")
            elif test_dir_to_open:
                log.warning(f"Test directory does not exist: {test_dir_to_open}")
        
        except Exception as e:
            log.error(f"Error processing CLI arguments: {e}", exc_info=True)

    @property
    def backend_factory(self) -> BackendFactory:
        """Get the backend factory for creating per-tab backends.
        
        Returns:
            BackendFactory instance
        """
        return self.llm_server.backend_factory
    
    def _get_llm_tabs(self) -> list:
        """Get list of tabs that have TabContext.
        
        Returns:
            List of tab widgets that support LLM operations
        """
        tabs = []
        if hasattr(self, 'text_only_tab'):
            tabs.append(self.text_only_tab)
        if hasattr(self, 'text_json_tab'):
            tabs.append(self.text_json_tab)
        if hasattr(self, 'json_code_tab'):
            tabs.append(self.json_code_tab)
        return tabs
    
    def refresh_all_button_labels(self):
        """
        Refresh button labels in all tabs.
        
        This method should be called after:
        - Changing button labels via settings dialog
        - Resetting labels to defaults
        - Loading a new task configuration
        
        Tabs inherit from BaseTab which automatically queries task_config_manager
        via main_window.task_config_manager property.
        """
        log.info("Refreshing button labels across all tabs")
        
        # Iterate through all tabs and rebuild their buttons; a view without an
        # action bar (Traceability) has none.
        for i in range(self.view_stack.count()):
            tab = self.view_stack.widget(i)
            if tab.task_buttons is not None:
                try:
                    tab.task_buttons.rebuild()
                    log.debug(f"Rebuilt LLM buttons for {tab.__class__.__name__}")
                except Exception as e:
                    log.error(f"Error rebuilding buttons in {tab.__class__.__name__}: {e}")
            # Rebuild validator buttons (Phase 2/3 registry-driven). Tabs
            # build them once at __init__ when the manager is still in
            # fallback mode — without this rebuild, opening a project
            # would never surface the project's configured validators.
            if tab.validator_buttons is not None:
                try:
                    tab.validator_buttons.rebuild()
                    log.debug(f"Rebuilt validator buttons for {tab.__class__.__name__}")
                except Exception as e:
                    log.error(f"Error rebuilding validator buttons in {tab.__class__.__name__}: {e}")

    def _on_install_package(self):
        """Packages -> Install Package: the shared procedo.core dialog."""
        InstallPackageDialog(self).exec()

    def _setup_central_widget(self):
        """The four views behind the segmented switch (``shell.build_view_column``)."""
        self._previous_tab_index = 0

        # Create the views (the test list is the Workspace column)
        # Text-only is a lighter-context sibling of Text-JSON that edits
        # the same procedure_text artifact.
        self.text_only_tab = TextOnlyTab(self)
        self.text_json_tab = TextJsonTab(self)
        self.json_code_tab = JsonCodeTab(self)
        self.traceability_tab = TraceabilityTab(self)

        # Connect artifact_saved signals so per-tab save buttons
        # trigger the sync check and status indicator update; an edit
        # repaints the header's pills and Save all
        views = (self.text_only_tab, self.text_json_tab, self.json_code_tab, self.traceability_tab)
        for tab in views:
            tab.artifact_saved.connect(self._document_controller.on_tab_artifact_saved)
            tab.content_changed.connect(lambda: shell.refresh_header(self))

        # Views in workflow order: Text → Text-JSON → JSON-Code → Traceability
        shell.build_view_column(self, list(views))

    def _setup_workspace_dock(self):
        """The test list: the Workspace column (``shell.build_frame`` lays it out)."""
        # Create workspace widget
        self.workspace_widget = WorkspaceTab(self)
        self.workspace_widget.test_opened.connect(self._document_controller.on_test_opened)
        self.workspace_widget.test_deleted.connect(self._document_controller.on_test_deleted)
        # Right-click "Mark Procedure In Sync" on the loaded test routes
        # through the existing acknowledgment flow (handles in-memory
        # session_state + disk write atomically, pops the confirmation).
        self.workspace_widget.request_mark_loaded_in_sync.connect(
            self._document_controller.on_sync_indicator_clicked,
        )
    
    def _setup_dock(self):
        """The Assistant column (``shell.build_frame`` lays it out)."""
        self.dock = DockWidget(self)
        
        # Update context limit based on backend config
        self._assistant_controller.update_context_limit()
        
        # Connect chat signals
        self.dock.chat_panel.message_sent.connect(self._assistant_controller.on_chat_message)
        self.dock.chat_panel.reset_requested.connect(self._assistant_controller.on_reset_session)
        self.dock.chat_panel.cancel_requested.connect(self._assistant_controller.on_cancel_llm)
        self.dock.chat_panel.compact_requested.connect(self._assistant_controller.on_compact_session)
    
    def _update_project_rules_indicators(self):
        """Repaint the header (the test and its project) and the footer (project,
        rules, bundle, validator); refresh what follows the project (syntax
        reference, board netlist)."""
        shell.refresh_header(self)
        shell.refresh_footer(self)

        # Live-refresh the syntax reference if it's open (project/bundle changed).
        from .dialogs.syntax_reference import SyntaxReferenceDialog
        SyntaxReferenceDialog.refresh_if_open(
            self.project_manager.rules_root, shell.bundle_name(self.project_manager.project_root))

        # Reload the Text tab's board-netlist for the current project — the panel
        # loads on tab-show, which can precede project setup (e.g. Text is now the
        # default tab), so re-trigger it once project_root is known.
        text_tab = getattr(self, "text_only_tab", None)
        if text_tab is not None and hasattr(text_tab, "_maybe_load_netlist"):
            text_tab._maybe_load_netlist()
    
    def _on_rules_indicator_clicked(self):
        """Handle click on rules indicator."""
        # Browse for rules folder
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Rules Folder",
            str(Path.home()) if not self.project_manager.project_root else str(self.project_manager.project_root),
        )
        
        if not path:
            return
        
        rules_path = Path(path)
        
        # Set rules root
        if self.project_manager.set_rules_root(rules_path):
            self._update_project_rules_indicators()
            self.footer.show_message(f"Rules loaded from: {rules_path}", 3000)
        else:
            QMessageBox.warning(
                self,
                "Invalid Rules Folder",
                f"The selected folder does not contain any .md files.\\n\\nSelected: {rules_path}"
            )
    
    def _apply_settings(self):
        """Apply settings to the UI."""
        # Apply editor settings would go here
        pass
    
    def _setup_shortcuts(self):
        """Setup keyboard shortcuts."""
        # Tab switching (Ctrl+1-4 for new 4-tab structure)
        for i in range(4):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{i+1}"), self)
            shortcut.activated.connect(lambda idx=i: self.view_stack.setCurrentIndex(idx))
    
    def _update_status_indicators(self):
        """Repaint the header (test, Text/JSON/Code and Sync pills, Save all)
        and the footer."""
        self._update_project_rules_indicators()
    
    def _update_menu_state(self):
        """Update menu items based on artifact availability."""
        pass
    
    # ==================== Event Handlers ====================
    
    def _on_settings(self):
        """Open settings dialog."""
        # Pass project_root so the Validator tab can read/write the
        # project's ``validator_loop`` config section. ``None`` is fine
        # when no project is open — the Validator tab self-disables.
        dialog = SettingsDialog(
            self.task_config_manager,
            self,
            project_root=self.project_manager.project_root,
            server_manager=self.llm_server.server_manager,
        )
        if dialog.exec():
            _old_settings = self._settings
            self._settings = dialog.get_settings()
            # Only respawn the OpenCode server when a launch-affecting setting
            # changed. A per-request change (model, timeouts, fold-system) is
            # applied to the LIVE config in place so the running server AND the
            # conversation's session_id (server-side history + accumulated MCP
            # tool results) survive. The model is a per-request override, never
            # baked into the server launch, so switching it must NOT restart —
            # this matches the web plugin, and is the only valid behaviour when
            # opencode is shared/remote and cannot be restarted.
            if self.llm_server.settings_require_restart(_old_settings, self._settings):
                self.llm_server.init_backend()
            else:
                self.llm_server.apply_settings_live()
            self._apply_settings()
            try:
                self.text_only_tab.reload_netlist()
            except Exception:
                log.debug("netlist reload after settings change failed", exc_info=True)
            # Refresh button labels + validator buttons after settings change
            self.refresh_all_button_labels()
            # Also refresh the chat panel's validator-status indicator
            # + auto-correct checkbox so the master toggle in Settings →
            # Validator takes effect immediately (Phase 4.6).
            try:
                current = self.view_stack.currentWidget()
                ctx = getattr(current, "tab_context", None)
                if ctx is not None:
                    self.dock.chat_panel._refresh_validator_ui_for_context(ctx)
            except Exception:
                log.debug("post-settings validator UI refresh failed", exc_info=True)
            shell.refresh_footer(self)  # the validator's on/off and attempts

    def _on_project_configuration(self):
        """Project -> Project Configuration: edit this project's config.json via the
        shared ProjectConfigDialog (the same dialog the main app uses).
        """
        root = getattr(self.project_manager, "project_root", None)
        if not root:
            QMessageBox.information(
                self, "Project Configuration",
                "Open or create a project first.")
            return
        origin = project_seeding.active_config_name(root)
        dlg = ProjectConfigDialog.for_project(self, project_path=root, project_config_origin=origin)
        if dlg is None:
            return
        dlg.exec()
        # Config / bundle may have changed -> refresh gating + rules. Runs on
        # Cancel too (task #39): the dialog can import a bundle live before
        # being cancelled; project_controller.handle_config_change funnels the reload/regate.
        self._update_project_rules_indicators()
        project_controller.handle_config_change(self)

    def _on_template_manager(self):
        """Project -> Template Manager: manage saved customer-config templates via
        the shared TemplateManagerDialog.
        """
        root = getattr(self.project_manager, "project_root", None)
        origin = project_seeding.active_config_name(root) if root else None
        TemplateManagerDialog(self, project_path=root, project_config_origin=origin).exec()

    def _on_bundle_library(self):
        """Packages -> Bundle Library: the host's Library window, on this PC. When a
        project is open, "Import into this project" targets it.
        """
        root = getattr(self.project_manager, "project_root", None)
        library_dialog.open_library(self, side=library_dialog.LOCAL, project_path=root)
        # Library changes may alter which bundle resolves -> refresh gating.
        # Task #39: an "Import into Project" must go live immediately —
        # project_controller.handle_config_change reloads TaskConfigManager (task buttons /
        # prompt templates via the reload-callback chain) on top of the regate.
        self._update_project_rules_indicators()
        project_controller.handle_config_change(self)

    def _on_scenarios(self):
        """Project -> Scenarios: author named test scenarios via the shared
        ScenarioManagerDialog. The author DEFINES scenarios here; the main app
        RUNS them. Candidate tests are injected so the dialog stays cycle-safe.
        """
        root = getattr(self.project_manager, "project_root", None)
        if not root:
            QMessageBox.information(
                self, "Scenarios", "Open or create a project first.")
            return
        candidates = [
            (i.name, i.has_json and i.has_code)
            for i in self.project_manager.enumerate_test_folders()
            if i.has_json
        ]
        ScenarioManagerDialog(self, project_root=root, candidates=candidates).exec()

    def _on_find(self):
        """Edit → Find. Delegates to the active tab's FindReplaceBar
        if it has one. Each tab's bar already handles the
        focused-editor-vs-leftmost target picking."""
        self._show_find_bar(replace=False)

    def _on_replace(self):
        """Edit → Replace. Same delegation as Find, with the replace
        row expanded."""
        self._show_find_bar(replace=True)

    def _show_find_bar(self, *, replace: bool) -> None:
        current = self.view_stack.currentWidget()
        bar = getattr(current, "find_bar", None)
        if bar is None:
            # Active tab has no find bar (e.g. workspace placeholder); nothing to do.
            return
        if replace:
            bar.show_replace()
        else:
            bar.show_find()
    
    def _on_syntax_reference(self):
        """Open the DSL Syntax Reference (cheat-sheet + full bundle rule docs)."""
        from .dialogs.syntax_reference import SyntaxReferenceDialog
        pm = self.project_manager
        SyntaxReferenceDialog.show_reference(pm.rules_root, self, shell.bundle_name(pm.project_root))

    def _on_export_keywords(self):
        """Show the Word-export template variable reference (dialogs/word_keywords_dialog)."""
        from .dialogs.word_keywords_dialog import show_word_keywords
        show_word_keywords(self)

    def _on_about(self):
        """Show the About box."""
        from .dialogs.about_dialog import AboutDialog
        AboutDialog(self).exec()
    
    # ==================== Public Interface ====================
    
    def switch_to_tab(self, tab_name: str):
        """Switch to a tab by name.

        Supported names: text_only, text_json, json_code, traceability.
        Legacy names (json, code, text) are mapped to the combined tabs.
        """
        name_map = {
            "text_only": self.text_only_tab,
            "text_json": self.text_json_tab,
            "json_code": self.json_code_tab,
            "traceability": self.traceability_tab,
            # Legacy name mappings
            "json": self.text_json_tab,
            "text": self.text_only_tab,
            "code": self.json_code_tab,
        }
        
        tab = name_map.get(tab_name.lower())
        if tab:
            self.view_stack.setCurrentWidget(tab)
    
    def open_test(self, path: Path):
        """Open a test folder programmatically."""
        self._document_controller.on_test_opened(path)
    
    def set_project_root(self, path: Path):
        """Set project root programmatically."""
        self.workspace_widget.set_project_root(path)
    
    def confirm_close(self, event):
        """The hosting window is closing: save the session, ask about unsaved
        work, stop the servers; ``event`` ignored when the user cancels (body
        in controllers/document_controller.py)."""
        self._document_controller.on_close_event(event)


class MainWindow(QMainWindow):
    """Procedo Studio's window: its title, icon and menu bar around the
    ``StudioWorkspace`` (which never assumes it owns this window)."""

    def __init__(self, **workspace_args):
        super().__init__()
        self.setWindowTitle("Procedo Studio")
        self.setWindowIcon(QIcon(str(shell.WINDOW_ICON_SVG)))
        self.setMinimumSize(1200, 700)
        self.workspace = StudioWorkspace(**workspace_args)
        self.setMenuBar(self.workspace.menu_bar)
        self.setCentralWidget(self.workspace)

    def closeEvent(self, event):
        self.workspace.confirm_close(event)
