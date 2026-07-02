"""
Main Window - Primary application window.

Implements the main UI structure from Section 9.
"""

from pathlib import Path
from typing import Optional
import json
import logging
import os
import threading
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QStatusBar, QMenuBar, QMenu, QToolBar, QMessageBox, QLabel, QDockWidget,
    QFileDialog, QDialog, QTextBrowser
)
from PySide6.QtCore import Qt, Signal, Slot, QFileSystemWatcher
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QCursor

from .core import (
    ArtifactManager, SessionState, ProjectManager, ArtifactType,
    CodeValidator
)
from .core.task_config import TaskConfigManager
from .llm import (
    LLMBackend,
    LLMRequest, LLMTask,
    OpenCodeConfig,
    LLMWorker,
)
from .llm.server_manager import OpenCodeServerManager
from .llm.backend_factory import (
    BackendFactory, BackendConfig,
    BACKEND_TYPE_OPENCODE, BACKEND_TYPE_NONE
)
from .tabs import (
    WorkspaceTab, TextOnlyTab, TextJsonTab, JsonCodeTab, TraceabilityTab
)
from .dock import DockWidget
from .dialogs import SettingsDialog, load_settings

log = logging.getLogger(__name__)


class ClickableLabel(QLabel):
    """Label that emits clicked signal and shows pointer cursor."""
    clicked = Signal()
    
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)



class MainWindow(QMainWindow):
    """
    Main application window.
    
    Contains:
    - Tab widget for main panels (Workspace, JSON, Code, Text, Traceability)
    - Dock widget for assistant panels (Chat, Session, Findings, Raw)
    - Menu bar and toolbar
    - Status bar
    """
    
    # Thread-safe UI marshal: a daemon (health probe / recover) emits a
    # callable here and Qt queues the slot onto THIS object's (UI) thread.
    # QTimer.singleShot(0, fn) from a non-Qt daemon thread creates the timer
    # in the CALLING thread, so the callback can be lost; a signal/slot is
    # the canonical cross-thread marshal (AutoConnection -> QueuedConnection).
    _ui_call = Signal(object)
    
    def __init__(
        self,
        parent=None,
        project_root: Optional[Path] = None,
        rules_root: Optional[Path] = None,
        test_name: Optional[str] = None,
        test_dir: Optional[Path] = None,
        llm_backend: Optional[str] = None,
        llm_profile: Optional[str] = None,
    ):
        super().__init__(parent)
        # Cross-thread UI marshal: queue daemon-posted callables onto the UI
        # thread (AutoConnection -> QueuedConnection from a worker thread).
        self._ui_call.connect(self._run_ui_call)
        log.debug("MainWindow.__init__ starting")
        self.setWindowTitle("Workflow Editor")
        self.setMinimumSize(1200, 700)
        
        # Store CLI arguments
        self._cli_project_root = project_root
        self._cli_rules_root = rules_root
        self._cli_test_name = test_name
        self._cli_test_dir = test_dir
        self._cli_llm_backend = llm_backend
        self._cli_llm_profile = llm_profile
        self._modern_workspace_layout = os.environ.get("TPG_APP_LAYOUT") == "modern_workspace"

        # Initialize managers
        log.debug("Initializing managers...")
        self.project_manager = ProjectManager()
        # Make the CLI override sticky on the project manager so every
        # subsequent detect_rules_root() call (test-open, project-open
        # from menu) honors it instead of falling back to the project-
        # relative auto-detect.
        if rules_root is not None:
            self.project_manager.cli_rules_root_override = rules_root
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
        self._config_watcher.fileChanged.connect(self._on_config_file_changed)
        self._config_watcher.directoryChanged.connect(self._on_config_dir_changed)
        self._watched_config_path: Optional[Path] = None
        self._watched_config_dir: Optional[Path] = None
        
        # Initialize task config manager pointing at the repo-shared
        # fallback. ``reload(project_root)`` is called below whenever a
        # project is loaded (CLI or interactive), switching the manager
        # into project-mode and triggering the legacy
        # ``tab_contexts.json`` → ``config.json:workflows`` migration.
        fallback_path = Path(__file__).parent.parent / "config" / "tab_contexts.json"
        self.task_config_manager = TaskConfigManager(fallback_path=fallback_path)
        
        # Initialize LLM
        self._settings = load_settings()
        log.debug(f"Settings loaded: {list(self._settings.keys())}")
        self._init_llm_backend()
        
        # Prompt builder and response parser
        # Setup UI
        self._setup_menu()
        self._setup_central_widget()
        self._setup_workspace_dock()
        self._setup_dock()
        self._setup_status_bar()
        
        # Apply settings
        self._apply_settings()

        # Setup keyboard shortcuts
        self._setup_shortcuts()

        # Disable tabs and dock until a test is loaded
        self.tab_widget.setEnabled(False)
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
        # root can route through _init_llm_backend / _restart_server_for_project,
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
        if self._server_manager is not None:
            # Pre-warm the server in the BACKGROUND so the first chat
            # doesn't lag (cold WSL boot + opencode serve). _prewarm_server
            # rebuilds the derived launch config for the current project,
            # sets working_directory BEFORE start() (C3 race), registers the
            # live-targeting cleanup hook (C2), and starts on a daemon thread.
            self._prewarm_server()

        # Begin polling server liveness so a crash mid-session is detected,
        # auto-recovered, and reflected in the status indicator (not just the
        # model picker). Started once the UI is up so the timer has an event
        # loop to run on.
        self._start_server_health_poll()
    
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

                # Detect rules. Honor the CLI override when one was passed
                # in; otherwise fall through to the project-relative
                # auto-detection inside detect_rules_root.
                self.project_manager.detect_rules_root(self._cli_rules_root)
                self._update_project_rules_indicators()
            
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
            if test_dir_to_open and test_dir_to_open.exists():
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

    
    def _stop_manager_async(self, manager):
        """Retire + stop a RETIRED-bound server manager off the UI thread.

        We arm the manager's permanent ``_retired`` flag synchronously HERE,
        before the stop thread runs (so a still-pending prewarm daemon on it
        refuses to launch -> no orphan), then tear the process down. That
        teardown (stop(), which also re-sets the flag) can block up to the
        startup timeout if the manager is mid-boot, so it runs on a daemon
        thread and the UI never freezes. The manager is single-use after this;
        callers build a FRESH OpenCodeServerManager for the new server.
        """
        if manager is None:
            return

        # Arm the permanent retirement guard SYNCHRONOUSLY, here on the
        # calling (UI) thread, BEFORE spawning the stop thread. stop() also
        # sets it (idempotent), but the teardown it does runs on the daemon
        # thread and can lag. A stale prewarm thread that races into the old
        # manager start() must see _retired==True at the swap instant, or it
        # spawns the orphan we are swapping away from. Only the guard flag is
        # armed now; the teardown stays deferred off the UI thread.
        manager._retired = True

        def _stop():
            try:
                manager.stop()
            except Exception:
                log.debug('async server manager stop() failed', exc_info=True)

        threading.Thread(target=_stop, daemon=True).start()

    def _init_llm_backend(self):
        """Initialize LLM backend infrastructure.
        
        Creates server manager (for OpenCode) and backend factory.
        Each tab will create its own backend via the factory.
        """
        # C2 manager-swap: retire the OLD manager before replacing it so a
        # re-init (e.g. Settings Save) never orphans the pre-warmed server.
        # stop() runs on a DAEMON THREAD: it can block up to the startup
        # timeout if the old manager is mid-boot, and that must never freeze
        # the UI. stop() sets the manager's permanent _retired flag
        # synchronously here (before the thread even runs), so a stale prewarm
        # daemon on the old manager refuses to launch -> no orphan. The fresh
        # manager built below is a NEW object (a retired one never restarts).
        _old_server_manager = getattr(self, '_server_manager', None)
        if _old_server_manager is not None:
            log.info('Retiring previous OpenCode server manager (swap)...')
            self._stop_manager_async(_old_server_manager)
        # Initialize server manager (will be None if not using OpenCode)
        self._server_manager: Optional[OpenCodeServerManager] = None
        
        # Build backend configuration
        config = self._build_backend_config()
        
        # Create server manager for OpenCode (shared across tabs)
        if config.backend_type == BACKEND_TYPE_OPENCODE:
            log.info("Creating OpenCode server manager...")
            self._server_manager = OpenCodeServerManager(config.opencode)
            # Don't start yet - will start on first backend creation
        
        # Create factory for tabs to use
        self._backend_factory = BackendFactory(config, self._server_manager)

        # Re-arm auto-recovery on EVERY manager swap: a fresh manager must
        # never inherit a retired predecessor's give-up count (the Settings
        # Save path reaches here without _reset_server_recovery), and this
        # also clears any _server_recovering left set by a now-stale recover
        # thread. The reset in _restart_server_for_project is now redundant
        # but harmless.
        self._reset_server_recovery()
        
        # C2: pre-warm the NEW manager on a daemon thread (set its launch
        # config first so it never launches from safe_wsl_cwd with the
        # wrong config). Only on a swap; the initial __init__ call defers
        # to showEvent (no project root yet), gated on _cli_args_processed.
        if getattr(self, '_cli_args_processed', False) \
                and self._server_manager is not None:
            self._prewarm_server()
        
        log.info(f"Backend infrastructure initialized: type={config.backend_type}")
        
        # Update LLM status display
        self._update_llm_status()
        
        # Update all tab contexts with new factory (if tabs are already initialized)
        self._update_all_tab_contexts()
    
    def _prewarm_server(self):
        """Set the current manager's launch config + pre-warm it (C1/C2/C3).

        Rebuilds the derived launch dir for the CURRENT project, assigns it
        to ``working_directory`` BEFORE start() fires (C3 race), registers a
        single stable cleanup hook that always targets the LIVE
        ``self._server_manager`` (C2 -- never a stale bound reference), and
        starts the server on a daemon thread so the UI never blocks.
        """
        sm = getattr(self, '_server_manager', None)
        if sm is None:
            return
        try:
            from .dialogs.settings_dialog import (
                build_launch_config, ensure_master_config)
            from .authoring.tool_folders import build_skill_tools_universe
            _pr = getattr(self.project_manager, 'project_root', None)
            _seed = (_pr / 'opencode.json') if _pr else None
            ensure_master_config(seed_from=_seed)
            # Compute the gate universe FIRST: if discovery throws, the except
            # fires before opencode.json is written -> never registered-but-ungated.
            _uni = build_skill_tools_universe(_pr)
            # Mint the run_skill HMAC secret ONCE per process (skip if already set
            # so a re-prewarm on the same config reuses it); pass it into the launch
            # config so the run_skill MCP block + the token-mint share one secret.
            if not getattr(sm.config, 'run_skill_secret', None):
                import secrets as _secrets
                sm.config.run_skill_secret = _secrets.token_hex()
            sm.config.working_directory = str(build_launch_config(
                _pr, run_skill_secret=sm.config.run_skill_secret))
            sm.config.skill_tools = _uni
        except Exception:
            log.warning('failed to build launch config for pre-warm',
                        exc_info=True)
        # Register the exit cleanup hook ONCE; it reads self._server_manager
        # live so it always stops whatever manager is current (C2).
        if not getattr(self, '_server_cleanup_registered', False):
            import atexit
            atexit.register(self._stop_current_server)
            self._server_cleanup_registered = True
        import threading as _pw_threading
        _pw_threading.Thread(target=sm.start, daemon=True).start()
    
    def _stop_current_server(self):
        """Stop whatever server manager is CURRENT (stable cleanup target).

        Bound once via atexit in _prewarm_server; reads self._server_manager
        at call time so a manager swap (Settings Save) can't leave a stale
        reference uncleaned (C2).
        """
        sm = getattr(self, '_server_manager', None)
        if sm is not None:
            try:
                sm.stop()
            except Exception:
                log.debug('current server manager stop() failed',
                          exc_info=True)
    
    def _restart_server_for_project(self):
        """On project change, retire the old server and stand up a FRESH one
        for the new project (Q2 auto-restart).

        A manager is single-use once retired (its ``_retired`` flag is
        permanent), so we can NOT stop+prewarm the same object — we delegate to
        ``_init_llm_backend``, which retires the old manager OFF the UI thread
        (no freeze, no orphan), builds a brand-new OpenCodeServerManager +
        factory, rewires the tab contexts, and pre-warms it (the prewarm
        rebuilds the derived launch config for the new project so its MCP
        blocks take effect). No-op when no manager / the UI hasn't been shown
        yet (showEvent does the initial pre-warm).
        """
        sm = getattr(self, '_server_manager', None)
        if sm is None or not getattr(self, '_cli_args_processed', False):
            return
        # A manual restart (Restart backend / project switch) clears any
        # auto-recovery give-up state so the fresh manager is auto-recoverable.
        self._reset_server_recovery()
        self._init_llm_backend()
    
    def _build_backend_config(self) -> BackendConfig:
        """Build backend config from settings.
        
        Returns:
            BackendConfig for the configured backend type
        """
        backend_type = self._settings.get("llm_backend", "opencode")
        log.info(f"Building backend config: {backend_type}")
        
        # Load custom output format
        custom_output_format = self._settings.get("custom_output_format", "")
        
        # Load common LLM parameters
        common_llm = self._settings.get("common_llm", {})
        
        if backend_type == "opencode":
            config_dict = self._settings.get("opencode", {})
            log.debug(f"OpenCode config: {config_dict}")
            opencode_config = OpenCodeConfig(
                server_port=config_dict.get("port", 4096),
                server_hostname=config_dict.get("host", "127.0.0.1"),
                model=config_dict.get("model") or None,
                wsl_path=config_dict.get("wsl_path") or "wsl",
                startup_timeout=config_dict.get("startup_timeout", 60.0),
                request_timeout=common_llm.get("request_timeout", 120.0),
                fold_system_into_prompt=config_dict.get("fold_system_into_prompt", False),
            )
            return BackendConfig(
                backend_type=BACKEND_TYPE_OPENCODE,
                opencode=opencode_config,
                custom_prompts={},  # Deprecated: now handled by TaskConfigManager
                custom_output_format=custom_output_format,
            )
        else:
            return BackendConfig.create_disabled()
    
    @property
    def backend_factory(self) -> BackendFactory:
        """Get the backend factory for creating per-tab backends.
        
        Returns:
            BackendFactory instance
        """
        return self._backend_factory
    
    def _update_all_tab_contexts(self):
        """Update all tab contexts with the current backend factory."""
        if not hasattr(self, '_backend_factory'):
            return
        
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'tab_context'):
                tab.tab_context.update_backend_factory(self._backend_factory)
        
        log.info("Backend factory propagated to all tab contexts")
    
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
    
    def _cancel_all_llm_workers(self):
        """Cancel any running LLM workers across all tabs."""
        for tab in self._get_llm_tabs():
            if hasattr(tab, '_worker') and tab._worker:
                tab._worker.cancel()
                log.debug(f"Cancelled LLM worker for {tab.__class__.__name__}")

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
        
        # Iterate through all tabs and refresh labels if supported
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            # Rebuild dynamic buttons if supported (for text_json, json_code tabs)
            if hasattr(tab, 'rebuild_llm_buttons'):
                try:
                    tab.rebuild_llm_buttons()
                    log.debug(f"Rebuilt LLM buttons for {tab.__class__.__name__}")
                except Exception as e:
                    log.error(f"Error rebuilding buttons in {tab.__class__.__name__}: {e}")
            elif hasattr(tab, 'refresh_button_labels'):
                try:
                    tab.refresh_button_labels()
                    log.debug(f"Refreshed button labels for {tab.__class__.__name__}")
                except Exception as e:
                    log.error(f"Error refreshing labels in {tab.__class__.__name__}: {e}")
            # Rebuild validator buttons (Phase 2/3 registry-driven). Tabs
            # build them once at __init__ when the manager is still in
            # fallback mode — without this rebuild, opening a project
            # would never surface the project's configured validators.
            if hasattr(tab, 'rebuild_validator_buttons'):
                try:
                    tab.rebuild_validator_buttons()
                    log.debug(f"Rebuilt validator buttons for {tab.__class__.__name__}")
                except Exception as e:
                    log.error(f"Error rebuilding validator buttons in {tab.__class__.__name__}: {e}")

    
    def _setup_menu(self):
        """Setup the menu bar."""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        new_project_action = QAction("&New Project...", self)
        new_project_action.setShortcut("Ctrl+N")
        new_project_action.triggered.connect(self._on_new_project)
        file_menu.addAction(new_project_action)
        
        open_action = QAction("&Open Project...", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self._on_open_project)
        file_menu.addAction(open_action)
        self._open_recent_menu = QMenu("Open &Recent", self)
        self._open_recent_menu.aboutToShow.connect(self._rebuild_open_recent_menu)
        file_menu.addMenu(self._open_recent_menu)
        
        file_menu.addSeparator()
        
        # Save current (context-aware label updated in _on_tab_changed)
        self.save_action = QAction("&Save", self)
        self.save_action.setShortcut(QKeySequence.Save)
        self.save_action.triggered.connect(self._on_save)
        file_menu.addAction(self.save_action)
        
        save_all_action = QAction("Save &All", self)
        save_all_action.setShortcut("Ctrl+Shift+S")
        save_all_action.triggered.connect(self._on_save_all)
        file_menu.addAction(save_all_action)
        
        export_menu = file_menu.addMenu("&Export")
        export_menu.setToolTip("Export the current procedure to Markdown or Word.")

        export_md_action = QAction("Procedure to &Markdown…", self)
        export_md_action.setToolTip(
            "Export the current procedure as a Markdown (.md) document."
        )
        export_md_action.triggered.connect(self._on_export_markdown)
        export_menu.addAction(export_md_action)

        export_word_action = QAction("Procedure to &Word…", self)
        export_word_action.setToolTip(
            "Export the current procedure as a Word (.docx) document."
        )
        export_word_action.triggered.connect(self._on_export_word)
        export_menu.addAction(export_word_action)
        
        file_menu.addSeparator()
        
        settings_action = QAction("Se&ttings...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._on_settings)
        file_menu.addAction(settings_action)

        project_config_action = QAction("Project &Configuration...", self)
        project_config_action.setToolTip(
            "Edit this project's config.json: bundle, equipment profiles/"
            "patterns, templates, imaging, workflows."
        )
        project_config_action.triggered.connect(self._on_project_configuration)
        file_menu.addAction(project_config_action)

        template_manager_action = QAction("&Template Manager...", self)
        template_manager_action.setToolTip(
            "Manage saved customer-configuration templates (reusable bundle + "
            "equipment + workflow presets)."
        )
        template_manager_action.triggered.connect(self._on_template_manager)
        file_menu.addAction(template_manager_action)

        bundle_library_action = QAction("Bundle &Library...", self)
        bundle_library_action.setToolTip(
            "Manage installed bundles; \"Import into Project\" targets the "
            "open project."
        )
        bundle_library_action.triggered.connect(self._on_bundle_library)
        file_menu.addAction(bundle_library_action)

        scenarios_action = QAction("&Scenarios...", self)
        scenarios_action.setToolTip(
            "Create / load / delete named test scenarios (subsets of this "
            "project's tests). Scenarios are run from the main app."
        )
        scenarios_action.triggered.connect(self._on_scenarios)
        file_menu.addAction(scenarios_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Edit menu
        edit_menu = menubar.addMenu("&Edit")

        find_action = QAction("&Find…", self)
        find_action.setShortcut(QKeySequence.Find)  # Ctrl+F
        find_action.setToolTip(
            "Open the find bar on the active tab's text editor "
            "(or the leftmost editor if focus is elsewhere)."
        )
        find_action.triggered.connect(self._on_find)
        edit_menu.addAction(find_action)

        replace_action = QAction("&Replace…", self)
        replace_action.setShortcut(QKeySequence("Ctrl+H"))
        replace_action.setToolTip(
            "Open the find/replace bar on the active tab's text editor."
        )
        replace_action.triggered.connect(self._on_replace)
        edit_menu.addAction(replace_action)

        edit_menu.addSeparator()

        self.mark_sync_action = QAction("Mark Artifacts In &Sync", self)
        self.mark_sync_action.setToolTip("Acknowledge that procedure.json and test.py are coherent")
        self.mark_sync_action.triggered.connect(self._on_sync_indicator_clicked)
        edit_menu.addAction(self.mark_sync_action)

        # View menu
        view_menu = menubar.addMenu("&View")
        
        # Workspace toggle (will be set after dock creation)
        self.toggle_workspace_action = QAction("Show &Workspace", self)
        self.toggle_workspace_action.setShortcut("Ctrl+Shift+E")
        self.toggle_workspace_action.setCheckable(True)
        self.toggle_workspace_action.setChecked(True)
        view_menu.addAction(self.toggle_workspace_action)
        
        # Assistant dock toggle
        self.toggle_dock_action = QAction("Show &Assistant", self)
        self.toggle_dock_action.setShortcut("Ctrl+Shift+A")
        self.toggle_dock_action.setCheckable(True)
        self.toggle_dock_action.setChecked(True)
        view_menu.addAction(self.toggle_dock_action)
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        syntax_action = QAction("&DSL Syntax Reference", self)
        syntax_action.setShortcut("F1")
        syntax_action.triggered.connect(self._on_syntax_reference)
        help_menu.addAction(syntax_action)

        export_kw_action = QAction("Word Export &Keywords...", self)
        export_kw_action.triggered.connect(self._on_export_keywords)
        help_menu.addAction(export_kw_action)

        about_action = QAction("&About", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

        # Packages menu — install skill / wizard / report / grammar
        # packages (shared project_services flow; same dialog as the main app).
        packages_menu = menubar.addMenu("&Packages")
        install_pkg_action = QAction("&Install Package...", self)
        install_pkg_action.triggered.connect(self._on_install_package)
        packages_menu.addAction(install_pkg_action)

        # Skills menu (authoring skill-chat); logic lives in
        # authoring.skill_menu so this mixed-EOL file gains only a call.
        from .authoring.skill_menu import install_skills_menu
        install_skills_menu(self)
    
    # ------------------------------------------------------------------
    #  Export (File → Export → Markdown / Word)
    # ------------------------------------------------------------------

    def _current_procedure_text(self):
        """Return the live procedure text from the active editor.

        Reads ``text_editor.toPlainText()`` directly (never the artifact
        cache) so unsaved edits are exported. Tries the active tab first,
        then the text/json tab which always owns procedure_text.
        """
        candidates = [
            self.tab_widget.currentWidget(),
            getattr(self, "text_json_tab", None),
            getattr(self, "text_only_tab", None),
        ]
        for tab in candidates:
            editor = getattr(tab, "text_editor", None)
            if editor is not None:
                txt = editor.toPlainText()
                if txt and txt.strip():
                    return txt
        return None

    def _on_install_package(self):
        """Packages -> Install Package: the shared project_services dialog."""
        from project_services.install_helper_dialog import InstallPackageDialog
        InstallPackageDialog(self).exec()

    def _export_default_path(self, suffix: str) -> Path:
        """Default save location/name for an export with *suffix* (e.g. '.md')."""
        root = getattr(self.project_manager, "project_root", None)
        if root is not None:
            stem = Path(root).name or "procedure"
            return Path(root) / f"{stem}{suffix}"
        return Path.home() / f"procedure{suffix}"

    def _on_export_markdown(self):
        """File -> Export -> Markdown: the SELECTED tests' text concatenated, or a
        quick single-procedure dump when no project is open."""
        root = getattr(self.project_manager, "project_root", None)
        if root is None:
            self._export_markdown_single()
            return
        folders = self.project_manager.enumerate_test_folders()
        if not folders:
            QMessageBox.information(self, "Export to Markdown",
                                    "This project has no test folders.")
            return
        from .dialogs.test_selection_dialog import TestSelectionDialog
        sel = TestSelectionDialog(folders, require="text", parent=self)
        if sel.exec() != QDialog.Accepted:
            return
        selected = sel.selected_folders()
        if not selected:
            QMessageBox.information(self, "Export to Markdown", "No tests selected.")
            return
        default = self._export_default_path(".md")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Tests to Markdown", str(default),
            "Markdown (*.md);;All Files (*)")
        if not path:
            return
        try:
            parts = []
            for f in selected:
                txt = (f.path / "procedure_text.md").read_text(encoding="utf-8")
                parts.append(f"# {f.name}\n\n{txt.strip()}\n")
            Path(path).write_text("\n\n".join(parts), encoding="utf-8")
        except Exception as e:
            log.exception("Markdown export failed")
            QMessageBox.critical(self, "Export Failed",
                                 f"Could not write Markdown:\n{e}")
            return
        self.status_bar.showMessage(
            f"Exported {len(selected)} test(s) to Markdown -> {path}", 5000)

    def _export_markdown_single(self):
        """Quick dump of the open procedure's text (used when no project is open)."""
        text = self._current_procedure_text()
        if not text:
            QMessageBox.information(
                self, "Export to Markdown",
                "There is no procedure text to export.\n"
                "Open or author a procedure first.")
            return
        default = self._export_default_path(".md")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Procedure to Markdown", str(default),
            "Markdown (*.md);;All Files (*)")
        if not path:
            return
        try:
            from .core.procedure_export import write_markdown
            write_markdown(text, Path(path))
        except Exception as e:
            log.exception("Markdown export failed")
            QMessageBox.critical(self, "Export Failed",
                                 f"Could not write Markdown:\n{e}")
            return
        self.status_bar.showMessage(f"Exported Markdown -> {path}", 5000)

    def _on_export_word(self):
        """File -> Export -> Word: the FULL report (.docx) over the SELECTED tests
        (same engine as the main app), or a quick single-procedure dump when no
        project is open."""
        root = getattr(self.project_manager, "project_root", None)
        if root is None:
            self._export_word_single()
            return
        folders = self.project_manager.enumerate_test_folders()
        if not folders:
            QMessageBox.information(self, "Export to Word",
                                    "This project has no test folders.")
            return
        from .dialogs.test_selection_dialog import TestSelectionDialog
        sel = TestSelectionDialog(folders, require="json", parent=self)
        if sel.exec() != QDialog.Accepted:
            return
        selected = sel.selected_folders()
        if not selected:
            QMessageBox.information(self, "Export to Word", "No tests selected.")
            return
        from project_services.report_export import (
            create_default_sidecar, save_sidecar, sidecar_path_for)
        from project_services.word_export_dialog import WordExportDialog
        from .core.full_report import (
            export_full_report, has_active_bundle, FullReportError)

        if not has_active_bundle(root):
            if QMessageBox.question(
                self, "No active bundle",
                "This project has no active bundle, so the report will contain "
                "metadata only — no test steps or expected results.\n\n"
                "Export anyway?") != QMessageBox.StandardButton.Yes:
                return

        export_folder = Path(root) / "exports"
        export_folder.mkdir(parents=True, exist_ok=True)
        sidecar = create_default_sidecar()
        dlg = WordExportDialog(sidecar, Path(root), export_folder,
                               default_docx_name="test_report.docx", parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        output_path = dlg.get_output_path()
        sidecar = dlg.get_sidecar()
        from PySide6.QtWidgets import QApplication, QProgressDialog
        prog = QProgressDialog("Generating board images...", "Cancel", 0, 0, self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)

        def _img_progress(done, total):
            prog.setMaximum(max(total, 1))
            prog.setValue(done)
            QApplication.processEvents()
            return not prog.wasCanceled()

        try:
            save_sidecar(sidecar_path_for(output_path), sidecar)
            export_full_report(Path(root), [f.path for f in selected],
                               output_path, sidecar=sidecar, progress=_img_progress)
        except FullReportError as e:
            QMessageBox.warning(self, "Export to Word", str(e))
            return
        except ImportError as e:
            QMessageBox.warning(
                self, "Word Export Unavailable",
                "The full report needs the 'docxtpl' package in this "
                f"environment.\n\n{e}")
            return
        except Exception as e:
            log.exception("Full report export failed")
            QMessageBox.critical(self, "Export Failed",
                                 f"Could not generate the Word report:\n{e}")
            return
        finally:
            prog.close()
        self.status_bar.showMessage(
            f"Exported {len(selected)}-test report -> {output_path}", 5000)

    def _export_word_single(self):
        """Quick dump of the open procedure as a plain .docx (used when no project)."""
        text = self._current_procedure_text()
        if not text:
            QMessageBox.information(
                self, "Export to Word",
                "There is no procedure text to export.\n"
                "Open or author a procedure first.")
            return
        default = self._export_default_path(".docx")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Procedure to Word", str(default),
            "Word Document (*.docx);;All Files (*)")
        if not path:
            return
        from .core.procedure_export import write_word, WordExportUnavailable
        try:
            write_word(text, Path(path), title=None)
        except WordExportUnavailable as e:
            QMessageBox.warning(self, "Word Export Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Word export failed")
            QMessageBox.critical(self, "Export Failed",
                                 f"Could not write Word document:\n{e}")
            return
        self.status_bar.showMessage(f"Exported Word -> {path}", 5000)

    def _setup_central_widget(self):
        """Setup central widget with tabs."""
        # Tab widget (no container needed)
        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("workflowTabs")
        self._previous_tab_index = 0
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        
        # Create tabs (workspace moved to dock)
        # Text-only is a lighter-context sibling of Text-JSON that edits
        # the same procedure_text artifact.
        self.text_only_tab = TextOnlyTab(self)
        self.text_json_tab = TextJsonTab(self)
        self.json_code_tab = JsonCodeTab(self)
        self.traceability_tab = TraceabilityTab(self)

        # Connect artifact_saved signals so per-tab save buttons
        # trigger the sync check and status indicator update
        for tab in (self.text_only_tab, self.text_json_tab, self.json_code_tab, self.traceability_tab):
            tab.artifact_saved.connect(self._on_tab_artifact_saved)

        # Add tabs in workflow order: Text → Text-JSON → JSON-Code → Traceability
        self.tab_widget.addTab(self.text_only_tab, "Text")
        self.tab_widget.addTab(self.text_json_tab, "Text-JSON")
        self.tab_widget.addTab(self.json_code_tab, "JSON-Code")
        self.tab_widget.addTab(self.traceability_tab, "Traceability")
        
        if self._modern_workspace_layout:
            central = QWidget()
            central.setObjectName("mainCentral")
            layout = QVBoxLayout(central)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(10)
            layout.addWidget(self.tab_widget)
            self.setCentralWidget(central)
        else:
            self.setCentralWidget(self.tab_widget)
    
    def _setup_workspace_dock(self):
        """Setup workspace as a left sidebar dock."""
        # Create workspace widget
        self.workspace_widget = WorkspaceTab(self)
        self.workspace_widget.test_opened.connect(self._on_test_opened)
        self.workspace_widget.test_deleted.connect(self._on_test_deleted)
        # Right-click "Mark Procedure In Sync" on the loaded test routes
        # through the existing acknowledgment flow (handles in-memory
        # session_state + disk write atomically, pops the confirmation).
        self.workspace_widget.request_mark_loaded_in_sync.connect(
            self._on_sync_indicator_clicked,
        )
        
        # Create dock widget
        self.workspace_dock = QDockWidget("Workspace", self)
        self.workspace_dock.setWidget(self.workspace_widget)
        if self._modern_workspace_layout:
            self.workspace_dock.setMinimumWidth(280)
        
        # Dock configuration
        self.workspace_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.workspace_dock.setFeatures(
            QDockWidget.DockWidgetClosable |    # Can close
            QDockWidget.DockWidgetMovable       # Can move
        )
        
        # Add to main window
        self.addDockWidget(Qt.LeftDockWidgetArea, self.workspace_dock)
        
        # Default size
        self.workspace_dock.setMinimumWidth(280 if self._modern_workspace_layout else 200)
        self.workspace_dock.resize(300 if self._modern_workspace_layout else 250, self.height())
        
        # Connect toggle action
        self.toggle_workspace_action.setChecked(self.workspace_dock.isVisible())
        self.toggle_workspace_action.triggered.connect(self._on_toggle_workspace)
        self.workspace_dock.visibilityChanged.connect(self._on_workspace_visibility_changed)
    
    def _setup_dock(self):
        """Setup the dock widget."""
        self.dock = DockWidget(self)
        if self._modern_workspace_layout:
            self.dock.setMinimumWidth(320)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        
        # Update context limit based on backend config
        self._update_context_limit()
        
        # Connect chat signals
        self.dock.chat_panel.message_sent.connect(self._on_chat_message)
        self.dock.chat_panel.reset_requested.connect(self._on_reset_session)
        self.dock.chat_panel.cancel_requested.connect(self._on_cancel_llm)
        self.dock.chat_panel.restart_requested.connect(self._on_restart_backend)
        self.dock.chat_panel.compact_requested.connect(self._on_compact_session)
        
        # Connect toggle action
        self.toggle_dock_action.setChecked(self.dock.isVisible())
        self.toggle_dock_action.triggered.connect(self._on_toggle_dock)
        self.dock.visibilityChanged.connect(self._on_dock_visibility_changed)
    
    def _update_context_limit(self):
        """Update chat panel's 'Tokens: X/Y' indicator from common_llm.context_window.

        This is purely a UI display value (the model's total context window)
        so the operator can see how much budget has been consumed. It is
        not sent to the LLM. Falls back to the legacy ``max_tokens`` key
        for projects/users on older settings files.
        """
        if not hasattr(self, 'dock') or not hasattr(self.dock, 'chat_panel'):
            return

        common_llm = self._settings.get("common_llm", {}) if hasattr(self, '_settings') else {}
        context_limit = common_llm.get(
            "context_window", common_llm.get("max_tokens", 16384)
        )
        self.dock.chat_panel.set_context_limit(context_limit)
    
    def _setup_status_bar(self):
        """Setup the status bar."""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # Create container for two-row layout
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # First row: Test name + Artifact indicators
        row1 = QWidget()
        row1_layout = QHBoxLayout(row1)
        row1_layout.setContentsMargins(0, 0, 0, 0)
        row1_layout.setSpacing(8)
        
        self.test_label = QLabel("No test loaded")
        row1_layout.addWidget(self.test_label)
        
        row1_layout.addWidget(QLabel(" | "))
        
        self.text_indicator = QLabel("Text ⚪")
        row1_layout.addWidget(self.text_indicator)
        
        self.json_indicator = QLabel("JSON ⚪")
        row1_layout.addWidget(self.json_indicator)
        
        self.code_indicator = QLabel("Code ⚪")
        row1_layout.addWidget(self.code_indicator)
        
        row1_layout.addWidget(QLabel(" | "))
        
        self.sync_indicator = ClickableLabel("Sync ✅")
        self.sync_indicator.setToolTip("JSON ↔ Code coherence status. Click to acknowledge sync.")
        self.sync_indicator.clicked.connect(self._on_sync_indicator_clicked)
        row1_layout.addWidget(self.sync_indicator)
        
        layout.addWidget(row1)
        
        # Second row: Project/Rules indicators
        row2 = QWidget()
        row2_layout = QHBoxLayout(row2)
        row2_layout.setContentsMargins(0, 0, 0, 0)
        row2_layout.setSpacing(8)
        
        self.project_indicator = ClickableLabel("Project: None")
        self.project_indicator.clicked.connect(self._on_project_indicator_clicked)
        row2_layout.addWidget(self.project_indicator)
        
        self.rules_indicator = ClickableLabel("Rules: None")
        self.rules_indicator.clicked.connect(self._on_rules_indicator_clicked)
        row2_layout.addWidget(self.rules_indicator)
        
        layout.addWidget(row2)
        
        self.status_bar.addWidget(container)
        
        # LLM status
        self.llm_status = QLabel("")
        self.status_bar.addPermanentWidget(self.llm_status)

        # Always-visible OpenCode server health signal (running / down).
        # A crashed mid-session server otherwise gave no at-a-glance cue —
        # the user only found out when the model picker said "unreachable".
        # _refresh_server_indicator (driven by a periodic poll) keeps this
        # current and triggers auto-recovery when it reads down.
        self.server_indicator = QLabel("")
        self.server_indicator.setToolTip(
            "OpenCode server health. A down server auto-recovers in the "
            "background.")
        self.status_bar.addPermanentWidget(self.server_indicator)
    
    def _update_project_rules_indicators(self):
        """Update project and rules indicators in status bar."""
        if self.project_manager is None:
            self.project_indicator.setText("Project: None")
            self.rules_indicator.setText("Rules: None")
            return
        
        # Update project indicator - show full path
        project_root = self.project_manager.project_root
        if project_root:
            self.project_indicator.setText(f"Project: {project_root}")
        else:
            self.project_indicator.setText("Project: None")
        
        # Update rules indicator - show full path
        rules_root = self.project_manager.rules_root
        if rules_root:
            self.rules_indicator.setText(f"Rules: ✅ {rules_root}")
        else:
            self.rules_indicator.setText("Rules: ❌ None")

        # Live-refresh the syntax reference if it's open (project/bundle changed).
        from .dialogs.syntax_reference import SyntaxReferenceDialog
        SyntaxReferenceDialog.refresh_if_open(rules_root)

        # Reload the Text tab's board-netlist for the current project — the panel
        # loads on tab-show, which can precede project setup (e.g. Text is now the
        # default tab), so re-trigger it once project_root is known.
        text_tab = getattr(self, "text_only_tab", None)
        if text_tab is not None and hasattr(text_tab, "_maybe_load_netlist"):
            text_tab._maybe_load_netlist()
    
    def _on_project_indicator_clicked(self):
        """Handle click on project indicator."""
        # Same as opening project from File menu
        self._on_open_project()
    
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
            self.status_bar.showMessage(f"Rules loaded from: {rules_path}", 3000)
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
            shortcut.activated.connect(lambda idx=i: self.tab_widget.setCurrentIndex(idx))
    
    def _update_status_indicators(self):
        """Update artifact status indicators in status bar."""
        if self.artifact_manager is None:
            circle = "\u26aa"  # White circle
            self.text_indicator.setText(f"Text {circle}")
            self.json_indicator.setText(f"JSON {circle}")
            self.code_indicator.setText(f"Code {circle}")
            self.sync_indicator.setText("Sync \u26aa")
            self.sync_indicator.setToolTip("No test loaded")
            self._update_project_rules_indicators()
            return
        
        # Check if artifacts exist
        text_ok = bool(self.artifact_manager.procedure_text.content and self.artifact_manager.procedure_text.content.strip())
        json_ok = bool(self.artifact_manager.procedure_json.content and self.artifact_manager.procedure_json.content.strip())
        code_ok = bool(self.artifact_manager.test_code.content and self.artifact_manager.test_code.content.strip())
        
        circle = "\u26aa"  # White circle
        check = "✅"  # Green check
        
        self.text_indicator.setText(f"Text {check if text_ok else circle}")
        self.json_indicator.setText(f"JSON {check if json_ok else circle}")
        self.code_indicator.setText(f"Code {check if code_ok else circle}")
        
        # Update sync indicator
        self._update_sync_indicator()
        
        # Also update project/rules indicators
        self._update_project_rules_indicators()
    
    def _update_menu_state(self):
        """Update menu items based on artifact availability."""
        pass
    
    def _update_sync_indicator(self):
        """Update the JSON\u2194Code sync indicator in the status bar."""
        if not self.session_state:
            self.sync_indicator.setText("Sync \u26aa")
            self.sync_indicator.setToolTip("No test loaded")
            return
        
        if self.session_state.artifacts_in_sync:
            self.sync_indicator.setText("Sync \u2705")
            self.sync_indicator.setToolTip(
                "procedure.json and test.py are in sync.\n"
                "Click to view status."
            )
        else:
            self.sync_indicator.setText("Sync \u26a0\ufe0f")
            self.sync_indicator.setToolTip(
                "procedure.json and test.py may be out of sync!\n"
                "One was modified without the other.\n"
                "Click to acknowledge sync."
            )
    
    def _on_sync_indicator_clicked(self):
        """Handle click on the sync indicator."""
        if not self.session_state:
            return
        
        if self.session_state.artifacts_in_sync:
            QMessageBox.information(
                self,
                "Artifacts In Sync",
                "procedure.json and test.py are currently marked as in sync."
            )
            return
        
        result = QMessageBox.question(
            self,
            "Acknowledge Sync",
            "procedure.json and test.py are currently marked as OUT OF SYNC.\n\n"
            "One was modified without the other during this session.\n\n"
            "Are you sure both artifacts are now coherent?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if result == QMessageBox.Yes:
            self.session_state.artifacts_in_sync = True
            self.session_state.artifact_hashes = self.artifact_manager.compute_hashes()
            self.session_state.save()
            self._update_sync_indicator()
            self.workspace_widget.refresh()
            self.status_bar.showMessage("Artifacts marked as in sync", 2000)

    def _check_artifact_coherence(self) -> bool:
        """Check if JSON and Code artifacts are in sync and warn if not.
        
        Returns:
            True if the user cancelled (caller should abort), False otherwise.
        """
        if not self.session_state or self.session_state.artifacts_in_sync:
            return False
        
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("\u26a0\ufe0f Artifacts Out of Sync")
        msg.setText(
            "procedure.json and test.py may be out of sync!\n\n"
            "One was modified without the other. The test code may not\n"
            "match the procedure definition."
        )
        
        mark_sync_btn = msg.addButton("They are in sync \u2714", QMessageBox.AcceptRole)
        continue_btn = msg.addButton("Continue anyway", QMessageBox.DestructiveRole)
        go_back_btn = msg.addButton("Go back and review", QMessageBox.RejectRole)
        # Qt under-sizes stylesheet-styled QMessageBox buttons for long
        # labels (the text rect collapses to ~text width with no slack),
        # clipping the last glyphs. Widen each from its own font metrics.
        for _b in (mark_sync_btn, continue_btn, go_back_btn):
            _b.setMinimumWidth(_b.fontMetrics().horizontalAdvance(_b.text()) + 56)
        msg.setDefaultButton(go_back_btn)
        
        msg.exec()
        clicked = msg.clickedButton()
        
        if clicked == mark_sync_btn:
            self.session_state.artifacts_in_sync = True
            self.session_state.artifact_hashes = self.artifact_manager.compute_hashes()
            self.session_state.save()
            self._update_sync_indicator()
            self.workspace_widget.refresh()
            return False
        elif clicked == go_back_btn:
            return True  # User wants to go back and review
        else:
            # Continue anyway \u2014 leave out-of-sync flag
            return False
    
    # ==================== Event Handlers ====================
    
    def _on_tab_changed(self, index: int):
        """Handle tab change."""
        # Only sync the PREVIOUSLY active tab (the one being deactivated).
        # We must not sync all non-current tabs, as they may hold stale
        # content for shared artifacts (e.g. procedure.json).
        if hasattr(self, '_previous_tab_index'):
            prev_tab = self.tab_widget.widget(self._previous_tab_index)
            if prev_tab is not None and prev_tab != self.tab_widget.widget(index):
                if hasattr(prev_tab, 'sync_editors_to_artifacts'):
                    prev_tab.sync_editors_to_artifacts()
                if hasattr(prev_tab, 'on_deactivated'):
                    prev_tab.on_deactivated()
        
        self._previous_tab_index = index
        
        tab = self.tab_widget.widget(index)
        if hasattr(tab, 'on_activated') and self.artifact_manager is not None:
            tab.on_activated()
        
        # Switch chat context to current tab's TabContext
        if hasattr(self, 'dock') and hasattr(tab, 'tab_context'):
            self.dock.chat_panel.switch_context(tab.tab_context)
            self.dock.session_viewer.switch_context(tab.tab_context)
            self.dock.raw_viewer.switch_context(tab.tab_context)
            # Findings are per-test (session_state), no context switch needed
            
            # If this tab has a running LLM worker, restore in-flight UI
            worker = getattr(tab, '_worker', None)
            if worker and worker.isRunning():
                self.dock.chat_panel.add_thinking_message()
                self.dock.chat_panel.set_llm_active(True)
                # Restore all accumulated streaming text so far
                if worker.accumulated_thinking:
                    self.dock.chat_panel.append_thinking_text(
                        worker.accumulated_thinking
                    )
                if worker.accumulated_response:
                    self.dock.chat_panel.append_response_text(
                        worker.accumulated_response
                    )
                # Disconnect any stale connections before reconnecting
                # to avoid duplicate text from multiple connections
                try:
                    worker.thinking_chunk.disconnect(
                        self.dock.chat_panel.append_thinking_text
                    )
                except (RuntimeError, TypeError):
                    pass
                try:
                    worker.text_chunk.disconnect(
                        self.dock.chat_panel.append_response_text
                    )
                except (RuntimeError, TypeError):
                    pass
                # Reconnect streaming signals to the restored thinking widget
                worker.thinking_chunk.connect(
                    self.dock.chat_panel.append_thinking_text
                )
                worker.text_chunk.connect(
                    self.dock.chat_panel.append_response_text
                )
        
        # Update save action label to be context-aware
        tab_name = self.tab_widget.tabText(index)
        self.save_action.setText(f"&Save {tab_name}")
        
        # Update menu state based on current artifacts
        self._update_menu_state()
    
    def _on_test_opened(self, path: Path):
        """Handle test folder being opened."""
        log.info(f"Opening test: {path}")
        
        # Check for unsaved changes before loading a new test
        if self.artifact_manager and self._check_unsaved_changes():
            return  # User cancelled
        
        # Check artifact coherence before switching away
        if self.artifact_manager and self._check_artifact_coherence():
            return  # User wants to review
        
        # Save current session state (includes validation_issues) before switching
        if hasattr(self, 'session_state') and self.session_state and self.session_state._file_path:
            try:
                self.session_state.save()
            except Exception as e:
                log.warning(f"Failed to save session state before switching tests: {e}")
        
        # Initialize managers for this test
        self.artifact_manager = ArtifactManager()
        self.artifact_manager.set_test_dir(path)
        self.artifact_manager.load_all()  # Load existing files from disk
        self.artifact_manager.set_exclusion_patterns(
            self.project_manager.get_equipment_patterns()
        )
        
        # Initialize session state (empty, not with path)
        self.session_state = SessionState()
        self.session_state.set_file_path(path)
        self.session_state.load()  # Load existing session data from disk if it exists

        # Check for external edits since last session
        self._check_for_external_changes()
        
        # ChatHistoryManager removed: chat history is now per-tab only
        
        # Update tab contexts with real managers (fixes None reference issue)
        if hasattr(self.text_only_tab, 'tab_context'):
            self.text_only_tab.tab_context.update_managers(self.artifact_manager, self.session_state)
        if hasattr(self.text_json_tab, 'tab_context'):
            self.text_json_tab.tab_context.update_managers(self.artifact_manager, self.session_state)
        if hasattr(self.json_code_tab, 'tab_context'):
            self.json_code_tab.tab_context.update_managers(self.artifact_manager, self.session_state)
        
        # Point findings panel at the new session state (per-test findings)
        self.dock.findings_panel.set_session(self.session_state)
        
        log.debug(f"Artifacts exist - JSON: {self.artifact_manager.procedure_json.exists_on_disk}, "
                  f"Code: {self.artifact_manager.test_code.exists_on_disk}, "
                  f"Text: {self.artifact_manager.procedure_text.exists_on_disk}")
        
        # Update status
        self.test_label.setText(f"Test: {path.name}")
        
        # Highlight the opened test in workspace
        self.workspace_widget.set_opened_test(path)
        
        # Detect rules (result stored in project_manager.rules_root)
        self.project_manager.detect_rules_root()
        
        # Enable tabs and dock now that a test is loaded
        self.tab_widget.setEnabled(True)
        self.dock.setEnabled(True)
        
        # Refresh tabs
        self.text_only_tab.load_content()
        self.text_json_tab.load_content()
        self.json_code_tab.load_content()
        self.traceability_tab.refresh()

        # Parser availability may have changed since last refresh (e.g. the
        # user edited config.json externally, or a parser variant file was
        # added/removed). Re-evaluate visibility here so the Quick Parse
        # button state is consistent per-test.
        self.text_json_tab.refresh_parser_button()
        self.text_only_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()
        
        # Refresh dock panels with session data
        self.dock.refresh_session()
        
        # Update status indicators
        self._update_status_indicators()
        self._update_menu_state()
        
        # Refresh session viewer
        self.dock.refresh_session()
        
        # Switch to appropriate tab only on first test load.
        # When switching between tests, preserve the user's current tab.
        if not hasattr(self, '_has_opened_test'):
            self._has_opened_test = True
            # Default to the Text tab (text-only authoring view).
            self.tab_widget.setCurrentWidget(self.text_only_tab)
        
        # The _on_tab_changed handler will call switch_context automatically
        # So we don't need to explicitly call it here - it's handled by the tab change event
    
    def _on_test_deleted(self, path: Path):
        """Handle a test folder being deleted."""
        log.info(f"Test deleted: {path}")
        
        # If the deleted test was the currently opened one, clear everything
        if self.artifact_manager and self.artifact_manager.test_dir == path:
            self.artifact_manager = None
            self.session_state = None
            
            # Clear editors
            self.text_only_tab.text_editor.clear()
            self.text_json_tab.text_editor.clear()
            self.text_json_tab.json_editor.clear()
            self.json_code_tab.json_editor.clear()
            self.json_code_tab.code_editor.clear()
            
            # Disable tabs and dock
            self.tab_widget.setEnabled(False)
            self.dock.setEnabled(False)
            
            # Reset status
            self.test_label.setText("No test loaded")
            self._update_status_indicators()
            
            # Clear opened test highlight
            self.workspace_widget.set_opened_test(None)
        
        self.status_bar.showMessage(f"Deleted test: {path.name}", 3000)
    
    def _update_llm_status(self):
        """Update status bar with LLM backend information."""
        # Check if UI is initialized yet
        if not hasattr(self, 'llm_status'):
            return
        
        if not hasattr(self, '_backend_factory') or self._backend_factory is None:
            self.llm_status.setText("LLM: None")
            return
        
        backend_type = self._backend_factory.backend_type
        
        if backend_type == BACKEND_TYPE_OPENCODE:
            server_status = "ready" if self._server_manager and self._server_manager.is_available() else "not started"
            self.llm_status.setText(f"LLM: OpenCode ({server_status})")
        else:
            self.llm_status.setText("LLM: Disabled")
        # Keep the always-visible server signal in sync on every LLM-status
        # refresh too (cheap; uses cached process state, no HTTP).
        self._refresh_server_indicator()
    
    def _start_server_health_poll(self):
        """Poll OUR OpenCode server's liveness on a timer and auto-recover a
        crash.

        prewarm only fires on app-start / project-switch, so a server that
        CRASHED mid-session was never noticed or relaunched — the user just hit
        an 'unreachable' picker and had to restart the whole app. This timer
        closes that gap: every few seconds it checks real liveness and, when
        the server is down (and a manager exists, not retired), relaunches it on
        a daemon thread via ensure_running(). The indicator is refreshed each
        tick so the 'running / down' signal stays honest.
        """
        from PySide6.QtCore import QTimer
        if getattr(self, '_server_health_timer', None) is not None:
            return
        self._server_health_timer = QTimer(self)
        self._server_health_timer.setInterval(5000)
        self._server_health_timer.timeout.connect(self._on_server_health_tick)
        self._server_health_timer.start()

    # Stop auto-recovering after this many consecutive failed relaunches so a
    # crash-looping server doesn't relaunch every tick forever (log-spam + CPU).
    _MAX_SERVER_RECOVERY_ATTEMPTS = 3

    def _reset_server_recovery(self):
        """Re-arm auto-recovery: clear the consecutive-failure counter (and any
        in-flight flag). Called on every MANUAL recovery action (Restart
        backend, project switch, settings Start server) so a user intervention
        always lifts a prior give-up state."""
        self._server_recovery_attempts = 0
        self._server_recovering = False
        # Off-thread health-probe state (read via getattr defaults elsewhere):
        # the last probe verdict for the cheap indicator refresh, and the
        # single-probe-in-flight gate so ticks never stack daemons.
        self._last_server_alive = None
        self._health_probe_inflight = False

    def _on_server_health_tick(self):
        """One liveness poll, fully OFF the UI thread.

        is_alive does a process poll AND a synchronous ~2s /health GET, so
        it must NEVER run on the UI thread (it would freeze the Qt event
        loop every 5s tick). Instead we spawn a daemon that probes is_alive
        and marshals the verdict back to the UI thread via _post_to_ui. One
        probe at a time (gated by _health_probe_inflight) so ticks never
        stack daemons while a slow /health is still outstanding.
        """
        sm = getattr(self, '_server_manager', None)
        if sm is None:
            self._refresh_server_indicator()
            return
        # One probe in flight at a time; the next tick re-checks.
        if getattr(self, '_health_probe_inflight', False):
            return
        self._health_probe_inflight = True

        def _probe():
            try:
                alive = sm.is_alive  # process poll + ~2s /health, OFF the UI thread
            except Exception:
                log.debug('server health probe failed', exc_info=True)
                alive = False
            self._post_to_ui(lambda: self._on_health_probe_result(sm, alive))

        try:
            threading.Thread(target=_probe, daemon=True).start()
        except Exception:
            # Spawn failed after the in-flight flag was set — clear it so the
            # next tick can probe again (never wedge the poll permanently).
            self._health_probe_inflight = False
            log.debug('server health probe thread spawn failed', exc_info=True)

    def _on_health_probe_result(self, sm, alive):
        """UI-thread continuation of one health probe.

        Runs on the UI thread (marshalled by _post_to_ui), so every read/write
        of the recovery state (_server_recovering / _server_recovery_attempts /
        _last_server_alive) and the manager-identity guard happen on ONE thread.
        The manager swap (_init_llm_backend) is also UI-thread, so they
        serialise — no lock needed, no stale-thread clobber.
        """
        # A swap (Restart backend / project switch / Settings Save) may have
        # installed a NEW manager while this probe was in flight. Drop the stale
        # verdict — it must not drive the new manager's indicator or recovery.
        if sm is not getattr(self, '_server_manager', None):
            return
        # Clear AFTER the stale-manager guard: only the CURRENT manager's probe
        # owns the in-flight flag (a stale probe must not unblock the next tick).
        self._health_probe_inflight = False
        self._last_server_alive = alive
        self._refresh_server_indicator(alive)
        if alive or getattr(sm, '_retired', False):
            self._server_recovering = False
            # Server is back (or retired): a recovery succeeded — clear the
            # consecutive-failure count so auto-recovery is armed for the next
            # crash.
            self._server_recovery_attempts = 0
            return
        # Server is DOWN. After N consecutive failed auto-recoveries we GIVE
        # UP — a crash-looping server would otherwise relaunch every tick
        # forever (log-spam + CPU at the bench). The indicator then tells the
        # user to act; a manual Restart backend / Start server re-arms us.
        if getattr(self, '_server_recovery_attempts', 0) >= self._MAX_SERVER_RECOVERY_ATTEMPTS:
            return
        # One recovery in flight at a time (gated by _server_recovering).
        if getattr(self, '_server_recovering', False):
            return
        self._server_recovering = True
        log.warning('OpenCode server detected down; auto-recovering...')
        self.status_bar.showMessage('OpenCode server down — recovering…', 4000)
        try:
            threading.Thread(target=lambda: self._recover(sm), daemon=True).start()
        except Exception:
            # Thread.start() raised after the in-flight flag was set —
            # clear it so auto-recovery is not permanently disabled.
            self._server_recovering = False
            log.debug('server auto-recovery thread spawn failed', exc_info=True)

    def _recover(self, sm):
        """Daemon body: relaunch the server, then probe the result — both
        OFF the UI thread. ensure_running() is a blocking relaunch and is_alive
        is a ~2s HTTP probe, so neither may touch the UI thread. The verdict is
        marshalled back to _on_recover_done on the UI thread, which owns ALL
        recovery-state mutation and the manager-identity guard."""
        try:
            sm.ensure_running()
        except Exception:
            log.debug('server auto-recovery failed', exc_info=True)
        try:
            ok = sm.is_alive  # post-relaunch verdict, OFF the UI thread
        except Exception:
            log.debug('post-recovery health probe failed', exc_info=True)
            ok = False
        self._post_to_ui(lambda: self._on_recover_done(sm, ok))

    def _on_recover_done(self, sm, ok):
        """UI-thread continuation of one recovery attempt — the SINGLE place
        recovery state is mutated after a relaunch.

        A swap may have retired THIS sm and installed a new manager while we
        relaunched. Only the verdict for the CURRENT manager may touch the
        shared recovery flags, else a stale thread clobbers the new manager's
        state (bogus failure count -> premature give-up; or a spurious
        recovering=False that breaks the single-in-flight gate). The guard is
        re-validated HERE, on the UI thread, AFTER the blocking relaunch+probe.
        """
        if sm is not getattr(self, '_server_manager', None):
            return
        self._server_recovering = False
        # is_alive (process + /health) so a relaunch that leaves the server
        # still wedged counts as a failed attempt toward the give-up cap.
        if not ok:
            self._server_recovery_attempts = (
                getattr(self, '_server_recovery_attempts', 0) + 1)
        else:
            self._server_recovery_attempts = 0
        self._last_server_alive = ok
        self._refresh_server_indicator(ok)

    @Slot(object)
    def _run_ui_call(self, fn):
        """UI-thread slot for the _ui_call signal: just invoke the marshalled
        callable. Runs on this QObject's (the UI) thread because the signal
        was emitted from another thread (queued connection)."""
        fn()

    def _post_to_ui(self, fn):
        """Marshal a callable onto the UI thread via the _ui_call signal: the
        canonical thread-safe cross-thread dispatch. The off-thread
        probe/recover daemons have NO Qt event loop, so QTimer.singleShot(0, fn)
        from them would create the timer in the daemon thread and the callback
        could be lost (probe/recover would wedge forever). Emitting a signal is
        thread-safe and Qt queues the slot onto the receiver's (UI) thread.
        Isolated as a seam so tests can run continuations inline."""
        self._ui_call.emit(fn)

    def _refresh_server_indicator(self, alive=None):
        """Update the always-visible 'OpenCode: running / down' status label.

        ``alive`` may be passed by the poll (avoids a redundant process poll);
        otherwise we read it. When no OpenCode manager is configured the label
        is blank (the backend isn't OpenCode, so 'down' would be misleading).
        Colour follows a success / error palette.
        """
        if not hasattr(self, 'server_indicator'):
            return
        sm = getattr(self, '_server_manager', None)
        if sm is None:
            self.server_indicator.setText('')
            self.server_indicator.setToolTip('')
            return
        if alive is None:
            # Use the cached verdict from the last off-thread probe — NEVER
            # call is_alive/health_check here (that is blocking HTTP and this
            # runs on the UI thread). None (no probe yet) is treated as down.
            alive = getattr(self, '_last_server_alive', None)
        if getattr(self, '_server_recovering', False) and not alive:
            self.server_indicator.setText('OpenCode: recovering…')
            self.server_indicator.setStyleSheet('color: #b8860b;')  # amber
            self.server_indicator.setToolTip('Relaunching the OpenCode server…')
            return
        if alive:
            self.server_indicator.setText('OpenCode: running')
            self.server_indicator.setStyleSheet('color: #2e7d32;')  # green
            self.server_indicator.setToolTip('OpenCode server is running.')
        elif getattr(self, '_server_recovery_attempts', 0) >= self._MAX_SERVER_RECOVERY_ATTEMPTS:
            # Auto-recovery gave up after repeated failures — tell the user to
            # act (Restart backend re-arms auto-recovery).
            self.server_indicator.setText(
                'OpenCode: down — auto-restart failed (use Restart backend)')
            self.server_indicator.setStyleSheet('color: #c62828;')  # red
            self.server_indicator.setToolTip(
                'Auto-recovery gave up after repeated failures. Use Restart backend to try again.')
        else:
            self.server_indicator.setText('OpenCode: down')
            self.server_indicator.setStyleSheet('color: #c62828;')  # red
            self.server_indicator.setToolTip(
                'OpenCode server is down — auto-recovery will relaunch it.')

    def _on_reset_session(self):
        """Reset all LLM sessions (clears conversation history and rules cache)."""
        # Cancel any in-flight work
        self._cancel_all_llm_workers()
        
        # Reset all tab contexts (clear messages, reset _first_interaction flag, reset backend)
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'tab_context'):
                tab.tab_context.reset_conversation()
                tab.tab_context.reset_backend()
                log.info(f"{tab.__class__.__name__} tab context reset")
        
        # Clear chat panel UI and re-switch to current tab's context
        if hasattr(self, 'dock') and hasattr(self.dock, 'chat_panel'):
            # Get current tab's context before clearing
            current_tab = self.tab_widget.currentWidget()
            current_tab_context = None
            if hasattr(current_tab, 'tab_context'):
                current_tab_context = current_tab.tab_context
            
            # Re-switch to current tab's context (which is now empty after reset)
            self.dock.chat_panel.switch_context(current_tab_context)
            log.info("Chat panel UI cleared and switched to current tab context")
            
            # Add system message to chat indicating reset
            self.dock.chat_panel.add_system_message(
                "🔄 Session reset - starting fresh. Rules will be sent on next interaction."
            )
        
        self._update_llm_status()
    
    def _on_cancel_llm(self):
        """Handle LLM cancellation request."""
        # Check current tab for a running per-tab worker
        current_tab = self.tab_widget.currentWidget()
        worker = getattr(current_tab, '_worker', None)
        if worker and worker.isRunning():
            log.info("User requested LLM cancellation (per-tab worker)")
            worker.cancel()

            # Immediate UI feedback
            self.dock.chat_panel.set_llm_active(False)
            self.dock.chat_panel.remove_thinking_message()
            self.dock.chat_panel.add_system_message("Request cancelled by user")
            self.status_bar.showMessage("LLM request cancelled", 3000)

    def _on_restart_backend(self):
        """Restart the shared OpenCode server to recover a hung / unresponsive
        backend without relaunching the editor. Cancels any in-flight request,
        then retires the old manager and stands up a FRESH one (the retired
        manager is single-use); the next send re-uses the restarted server."""
        sm = self._server_manager
        if sm is None:
            self.dock.chat_panel.add_system_message(
                "No restartable backend server is configured.")
            return
        # Cancel any in-flight (possibly hung) request first (best-effort).
        try:
            self._on_cancel_llm()
        except Exception:
            log.exception("cancel before backend restart failed")
        self.dock.chat_panel.add_message("system", "Restarting backend…")
        self.status_bar.showMessage("Restarting backend…", 3000)

        # A manager is single-use after stop() (its _retired flag is
        # permanent), so we can NOT stop+start the SAME object. Stand up a
        # FRESH manager instead: _restart_server_for_project retires the old
        # one off the UI thread (no freeze, no orphan) and pre-warms the new
        # one on a daemon thread, so this returns immediately.
        self._restart_server_for_project()

    def _on_compact_session(self):
        """Manually compact the active tab's LLM session so a long
        conversation keeps fitting in the context window. Runs the compact
        request (a network POST to the OpenCode server) off the UI thread;
        the readout reflects the freed context on the next turn (the server
        reports the reduced total then), and we refresh it now best-effort."""
        current_tab = self.tab_widget.currentWidget()
        tab_context = getattr(current_tab, "tab_context", None)
        if tab_context is None:
            self.dock.chat_panel.add_system_message(
                "No active session to compact.")
            return
        backend = tab_context.backend
        self.dock.chat_panel.add_message("system", "Compacting session…")
        self.status_bar.showMessage("Compacting session…", 3000)

        def _done(ok):
            if ok:
                self.dock.chat_panel.add_system_message(
                    "🗜️ Session compacted — context will shrink on the next reply.")
            else:
                self.dock.chat_panel.add_system_message(
                    "Compaction is unavailable for this backend (or the request failed).")
            # Re-render the readout from the latest known total (best-effort;
            # the real reduction lands with the next turn's reported total).
            try:
                self.dock.chat_panel._update_context_label()
            except Exception:
                log.exception("context-label refresh after compact failed")

        def _compact():
            ok = False
            try:
                ok = bool(backend.compact())
            except Exception:
                log.exception("session compact failed")
            # Marshal the UI feedback back onto the UI thread via the
            # thread-safe _ui_call signal (this runs on a daemon with no Qt
            # event loop, so QTimer.singleShot from here could be lost).
            self._post_to_ui(lambda: _done(ok))

        threading.Thread(target=_compact, daemon=True).start()

    def _on_toggle_workspace(self):
        """Toggle workspace dock visibility."""
        if self.workspace_dock.isVisible():
            self.workspace_dock.hide()
        else:
            self.workspace_dock.show()
    
    def _on_workspace_visibility_changed(self, visible: bool):
        """Update action when workspace visibility changes."""
        self.toggle_workspace_action.blockSignals(True)
        self.toggle_workspace_action.setChecked(visible)
        self.toggle_workspace_action.blockSignals(False)
    
    def _on_toggle_dock(self):
        """Toggle dock visibility."""
        if self.dock.isVisible():
            self.dock.hide()
        else:
            self.dock.show()
    
    def _on_dock_visibility_changed(self, visible: bool):
        """Update action when dock visibility changes."""
        self.toggle_dock_action.blockSignals(True)
        self.toggle_dock_action.setChecked(visible)
        self.toggle_dock_action.blockSignals(False)
    
    def _on_new_project(self):
        """Handle new project creation."""
        # Shared, bundle-backed New Project — the SAME dialog the main app
        # uses: creates dirs + venv + bundle ref + starter test, so
        # editor-made projects are openable/runnable by the main app.
        # project_services is reachable via the GUI venv's editable install
        # (embedded) or the __main__ walk-up bootstrap (standalone).
        from project_services.new_project_dialog import NewProjectDialog
        from project_services import config_manager

        dialog = NewProjectDialog(self)
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
        self.project_manager.set_project_root(project_path)
        from project_services.app_settings import add_recent_project
        add_recent_project(str(project_path))

        # Now initialize the UI with the new project
        self.workspace_widget._load_test_list()
        self.workspace_widget.new_test_btn.setEnabled(True)

        # Detect rules (will prompt user if not found)
        self.project_manager.detect_rules_root()

        # Switch task configurations to the freshly-created project so
        # any workflow saves land in the new ``config.json``. The
        # registered reload callback refreshes button labels.
        self.task_config_manager.reload(self.project_manager.project_root)

        # Update status bar indicators
        self._update_project_rules_indicators()
        self.text_json_tab.refresh_parser_button()
        self.text_only_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()
        self._watch_project_config()
        
        # Show workspace dock if hidden
        if self.workspace_dock.isHidden():
            self.workspace_dock.show()
        self._restart_server_for_project()
        
        # Show success message
        QMessageBox.information(
            self,
            "Project Created",
            f"Project created successfully at:\n{project_path}\n\n"
            "You can now create test folders using the Workspace tab."
        )
    
    def _refresh_after_open(self):
        """Common UI refresh after a project root is set (open / recent)."""
        self.workspace_widget._load_test_list()
        self.workspace_widget.new_test_btn.setEnabled(True)
        self.project_manager.detect_rules_root()
        self.task_config_manager.reload(self.project_manager.project_root)
        self._update_project_rules_indicators()
        self.text_json_tab.refresh_parser_button()
        self.text_only_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()
        self._watch_project_config()
        if self.workspace_dock.isHidden():
            self.workspace_dock.show()
        self._restart_server_for_project()

    def _rebuild_open_recent_menu(self):
        # Repopulate from the SHARED recent list (same app_settings the main
        # app writes), so recents are unified across both apps.
        from project_services.app_settings import load_app_settings
        self._open_recent_menu.clear()
        recent = load_app_settings().get("recent_projects", [])
        if not recent:
            placeholder = self._open_recent_menu.addAction("No recent projects")
            placeholder.setEnabled(False)
            return
        for path_str in recent:
            action = self._open_recent_menu.addAction(path_str)
            action.triggered.connect(
                lambda checked=False, p=path_str: self._on_open_recent(p)
            )

    def _on_open_recent(self, path_str):
        """Open a project from the recent list."""
        from project_services.app_settings import add_recent_project
        path = Path(path_str)
        if not path.is_dir():
            QMessageBox.warning(
                self, "Project Not Found",
                f"The project folder no longer exists:\n{path_str}")
            return
        if self.project_manager.set_project_root(path):
            add_recent_project(str(path))
            self._refresh_after_open()

    def _on_open_project(self):
        """Handle open project action."""
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Project Root",
            str(Path.home()),
        )
        
        if not path:
            return
        
        project_path = Path(path)
        
        # Try to set as project root
        if self.project_manager.set_project_root(project_path):
            from project_services.app_settings import add_recent_project
            add_recent_project(str(project_path))
            self._refresh_after_open()
        else:
            # Maybe user selected a test folder directly?
            detected_root = self.project_manager.detect_project_from_test_folder(project_path)
            if detected_root:
                self.project_manager.set_project_root(detected_root)
                self.workspace_widget._load_test_list()
                self.workspace_widget.new_test_btn.setEnabled(True)
                self.project_manager.detect_rules_root()

                # Switch task configurations to the detected project root.
                self.task_config_manager.reload(self.project_manager.project_root)

                # Update status bar indicators
                self._update_project_rules_indicators()
                self.text_json_tab.refresh_parser_button()
                self.text_only_tab.refresh_parser_button()
                self.json_code_tab.refresh_code_parser_button()
                self._watch_project_config()
                
                # Show workspace dock
                if self.workspace_dock.isHidden():
                    self.workspace_dock.show()
                self._restart_server_for_project()
            else:
                QMessageBox.warning(
                    self,
                    "Invalid Project",
                    "Selected folder does not appear to be a valid project root.\n\n"
                    "A valid project should contain a 'tests/' or 'config/' folder."
                )

    def _watch_project_config(self) -> None:
        """Watch the active project's config.json + its parent dir.

        Removes any previous watches before adding the new ones. Safe
        to call repeatedly (e.g. after switching projects). The parent
        directory watch is the safety net for rmtree+copytree commits
        where the fileChanged signal fires on delete and we miss the
        subsequent recreate (Codex Q2).
        """
        config_dir = self.project_manager.get_config_dir()
        new_path = (config_dir / "config.json") if config_dir else None

        if self._watched_config_path is not None:
            self._config_watcher.removePath(str(self._watched_config_path))
            self._watched_config_path = None
        if self._watched_config_dir is not None:
            self._config_watcher.removePath(str(self._watched_config_dir))
            self._watched_config_dir = None

        if new_path and new_path.exists():
            self._config_watcher.addPath(str(new_path))
            self._watched_config_path = new_path
        # Watch the parent dir regardless of file presence — survives
        # the brief delete-recreate window of ProjectConfigDialog commits.
        if config_dir is not None and config_dir.exists():
            self._config_watcher.addPath(str(config_dir))
            self._watched_config_dir = config_dir

    def _on_config_file_changed(self, path: str) -> None:
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
        self._handle_config_change()
        p = Path(path)
        if p.exists() and str(p) not in self._config_watcher.files():
            self._config_watcher.addPath(str(p))

    def _on_config_dir_changed(self, path: str) -> None:
        """Directory-level watch fires when config.json is created/
        recreated by an external writer (e.g. ProjectConfigDialog's
        rmtree+copytree commit). Re-arms the file watch and triggers
        the same hot-reload as a direct file change. Phase 4.6.2."""
        if self._watched_config_path is None:
            return
        # If the file watch dropped during a rmtree window, re-add it
        # now that the directory event tells us the file is back.
        file_str = str(self._watched_config_path)
        if (self._watched_config_path.exists()
                and file_str not in self._config_watcher.files()):
            self._config_watcher.addPath(file_str)
            self._handle_config_change()

    def _handle_config_change(self) -> None:
        """Single funnel for hot-reload work — called by both the
        file-change and directory-change paths so logic stays in one
        place."""
        self.text_json_tab.refresh_parser_button()
        self.text_only_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()
        try:
            project_root = self.project_manager.project_root
            if project_root is not None:
                self.task_config_manager.reload(project_root)
        except Exception:  # never let the watcher die on a load error
            log.exception("Hot-reload of TaskConfigManager failed")
        # Also refresh the chat panel's validator-status indicator so
        # auto-correct checkbox + dot reflect the new state.
        try:
            current = self.tab_widget.currentWidget()
            ctx = getattr(current, "tab_context", None)
            if ctx is not None:
                self.dock.chat_panel._refresh_validator_ui_for_context(ctx)
        except Exception:
            log.debug("post-config-change chat-panel refresh failed", exc_info=True)
    
    def _on_save(self):
        """Save artifacts managed by the current tab.
        
        Delegates to the tab's save_all_artifacts() which properly syncs
        editor content, writes to disk, resets dirty flags, and updates
        status labels.
        """
        if not self.artifact_manager:
            return
        
        current_tab = self.tab_widget.currentWidget()
        
        if hasattr(current_tab, 'save_all_artifacts'):
            current_tab.save_all_artifacts()
        else:
            # Fallback for tabs without editors
            self.artifact_manager.save_all()

        # Check if JSON/Code pair coherence is broken
        self._check_sync_hashes()

        self._update_status_indicators()
        # Refresh workspace test list to update artifact indicators
        self.workspace_widget.refresh()
        self.status_bar.showMessage("Saved", 2000)

    def _check_sync_hashes(self):
        """Compare current artifact content hashes against the last-acknowledged baseline.

        If any canonical artifact (procedure.json, test.py) changed since the
        user last acknowledged sync, mark artifacts as out-of-sync.  Never
        auto-restores in-sync — only user acknowledgment does that.
        """
        if not self.artifact_manager or not self.session_state:
            return
        stored = self.session_state.artifact_hashes
        if not stored:
            # First save or legacy session — seed baseline, assume in-sync
            self.session_state.artifact_hashes = self.artifact_manager.compute_hashes()
            self.session_state.save()
            return
        current = self.artifact_manager.compute_hashes()
        if current != stored:
            if self.session_state.artifacts_in_sync:
                self.session_state.artifacts_in_sync = False
                self.session_state.save()
                log.info("Artifacts marked out of sync: content hashes differ from acknowledged baseline")

    def _check_for_external_changes(self):
        """Detect files edited outside the workflow editor since the last acknowledgment.

        Compares stored hashes (from .llm_session.json) against current disk
        content.  If any canonical artifact changed, marks artifacts out of sync.
        """
        if not self.artifact_manager or not self.session_state:
            return
        stored = self.session_state.artifact_hashes
        if not stored:
            # First open or legacy session — seed hashes without warning
            self.session_state.artifact_hashes = self.artifact_manager.compute_hashes()
            self.session_state.save()
            return
        changed = self.artifact_manager.check_external_changes(stored)
        if changed:
            names = ", ".join(changed)
            log.info(f"External changes detected in: {names}")
            self.session_state.artifacts_in_sync = False
            self.session_state.save()
    
    def _on_tab_artifact_saved(self):
        """Handle artifact_saved signal from any tab's per-button save.
        
        Ensures the sync state and UI indicators are updated regardless of
        whether the user used Ctrl+S or a per-tab save button.
        """
        self._check_sync_hashes()
        self._update_status_indicators()
        self.workspace_widget.refresh()
    
    def _on_save_all(self):
        """Save all dirty artifacts across all tabs.
        
        Syncs only the current tab's editors (to avoid stale content from
        inactive tabs overwriting shared artifacts), then saves all dirty
        artifacts via the ArtifactManager, and reloads all tabs so their
        editors and dirty flags reflect the saved state.
        """
        if not self.artifact_manager:
            return
        
        # Sync only the current tab — inactive tabs may hold stale content
        # for shared artifacts like procedure.json
        current_tab = self.tab_widget.currentWidget()
        if hasattr(current_tab, 'sync_editors_to_artifacts'):
            current_tab.sync_editors_to_artifacts()
        
        # Save all dirty artifacts via artifact manager (single source of truth)
        self.artifact_manager.save_all()
        
        # Reload all tabs so editors + dirty flags reflect saved state
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'load_content'):
                tab.load_content()
        
        if self.session_state:
            self.session_state.save()
        
        # Check if artifacts changed from the acknowledged baseline
        self._check_sync_hashes()
        
        # Update indicators after save
        self._update_status_indicators()
        # Refresh workspace test list to update artifact indicators
        self.workspace_widget.refresh()
        self.status_bar.showMessage("All saved", 2000)
    
    def _check_unsaved_changes(self) -> bool:
        """Check for unsaved changes and prompt user.
        
        Syncs only the current tab's editors, then checks artifact dirty state.
        We must NOT sync inactive tabs because their editors may hold stale
        content for shared artifacts (e.g. procedure.json) and would overwrite
        the artifact manager's correct state.
        
        Returns:
            True if the user cancelled (caller should abort), False otherwise
        """
        if not self.artifact_manager:
            return False
        
        # Sync only the CURRENT tab to catch un-saved editor changes.
        # Inactive tabs may have stale content for shared artifacts.
        current_tab = self.tab_widget.currentWidget()
        if hasattr(current_tab, 'sync_editors_to_artifacts'):
            current_tab.sync_editors_to_artifacts()
        
        dirty = []
        if self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_JSON):
            dirty.append("procedure.json")
        if self.artifact_manager.is_dirty(ArtifactType.TEST_CODE):
            dirty.append("test.py")
        if self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_TEXT):
            dirty.append("procedure_text.md")
        
        if not dirty:
            return False
        
        result = QMessageBox.question(
            self,
            "Unsaved Changes",
            f"You have unsaved changes in:\n  \u2022 " + "\n  \u2022 ".join(dirty) +
            "\n\nSave before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save
        )
        
        if result == QMessageBox.Save:
            self._on_save_all()
            return False
        elif result == QMessageBox.Cancel:
            return True  # User cancelled
        else:
            return False  # Discard
    
    def _on_settings(self):
        """Open settings dialog."""
        # Pass project_root so the Validator tab can read/write the
        # project's ``validator_loop`` config section. ``None`` is fine
        # when no project is open — the Validator tab self-disables.
        dialog = SettingsDialog(
            self.task_config_manager,
            self,
            project_root=self.project_manager.project_root,
            server_manager=self._server_manager,
        )
        if dialog.exec():
            self._settings = dialog.get_settings()
            self._init_llm_backend()
            self._apply_settings()
            try:
                self.text_only_tab.reload_netlist()
            except Exception:
                pass
            # Refresh button labels + validator buttons after settings change
            self.refresh_all_button_labels()
            # Also refresh the chat panel's validator-status indicator
            # + auto-correct checkbox so the master toggle in Settings →
            # Validator takes effect immediately (Phase 4.6).
            try:
                current = self.tab_widget.currentWidget()
                ctx = getattr(current, "tab_context", None)
                if ctx is not None:
                    self.dock.chat_panel._refresh_validator_ui_for_context(ctx)
            except Exception:
                log.debug("post-settings validator UI refresh failed", exc_info=True)

    def _on_project_configuration(self):
        """File -> Project Configuration: edit this project's config.json via the
        shared ProjectConfigDialog (the same dialog the main app uses).
        """
        root = getattr(self.project_manager, "project_root", None)
        if not root:
            QMessageBox.information(
                self, "Project Configuration",
                "Open or create a project first.")
            return
        from project_services.config_manager_dialog import ProjectConfigDialog
        from project_services.project_model import ProjectModel
        origin = ProjectModel.get_active_config_from_path(root)
        dlg = ProjectConfigDialog(self, project_path=root, project_config_origin=origin)
        if dlg.exec() != QDialog.Accepted:
            return
        # Config / bundle may have changed -> refresh capability gating + rules.
        self.task_config_manager.reload(root)
        self._update_project_rules_indicators()
        self.text_json_tab.refresh_parser_button()
        self.text_only_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()

    def _on_template_manager(self):
        """File -> Template Manager: manage saved customer-config templates via
        the shared TemplateManagerDialog.
        """
        from project_services.config_manager_dialog import TemplateManagerDialog
        from project_services.project_model import ProjectModel
        root = getattr(self.project_manager, "project_root", None)
        origin = ProjectModel.get_active_config_from_path(root) if root else None
        TemplateManagerDialog(self, project_path=root, project_config_origin=origin).exec()

    def _on_bundle_library(self):
        """File -> Bundle Library: manage installed bundles via the shared
        BundleLibraryDialog. When a project is open, "Import into Project"
        targets it.
        """
        from project_services.bundle_library_dialog import BundleLibraryDialog
        root = getattr(self.project_manager, "project_root", None)
        BundleLibraryDialog(self, project_path=root).exec()
        # Library changes may alter which bundle resolves -> refresh gating.
        self._update_project_rules_indicators()
        self.text_json_tab.refresh_parser_button()
        self.text_only_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()

    def _on_scenarios(self):
        """File -> Scenarios: author named test scenarios via the shared
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
        from project_services.scenario_dialog import ScenarioManagerDialog
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
        current = self.tab_widget.currentWidget()
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
        rules_root = (self.project_manager.rules_root
                      if self.project_manager is not None else None)
        SyntaxReferenceDialog.show_reference(rules_root, self)

    def _on_export_keywords(self):
        """Show the Word-export template variable reference.

        Single-sourced from the main app's canonical
        ``test_procedure_gui/help/word_export_keywords.md`` (the editor
        exports the SAME report, so the variables are identical). Resolved
        via the installed package first, then the monorepo-relative path;
        bare-editor installs without the main app get a pointer instead of
        a stale duplicated copy.
        """
        md = None
        try:
            from test_procedure_gui import help as _help_pkg  # type: ignore
            _p = Path(_help_pkg.__file__).parent / "word_export_keywords.md"
            md = _p.read_text(encoding="utf-8") if _p.exists() else None
        except Exception:
            md = None
        if md is None:
            _p = (Path(__file__).resolve().parents[3]
                  / "src" / "test_procedure_gui" / "help"
                  / "word_export_keywords.md")
            try:
                md = _p.read_text(encoding="utf-8") if _p.exists() else None
            except Exception:
                md = None
        if md is None:
            QMessageBox.information(
                self, "Word Export Keywords",
                "The template-variable reference ships with the main "
                "application: open it there via Help \u2192 Documentation (F1) "
                "\u2192 Word Export Keywords.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Word Export Keywords")
        dlg.resize(760, 600)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser(dlg)
        browser.setOpenExternalLinks(True)
        browser.document().setMarkdown(md)
        lay.addWidget(browser)
        dlg.exec()

    def _on_about(self):
        """Show about dialog."""
        QMessageBox.about(
            self,
            "About Workflow Editor",
            "LLM Workflow Editor\n\n"
            "A tool for creating and managing structured test procedures "
            "with LLM assistance.\n\n"
            "Version 0.1.0"
        )
    
    def _on_chat_message(self, message: str):
        """Handle chat message from user."""
        # Set intent based on current tab
        if self.session_state:
            current_tab = self.tab_widget.currentWidget()
            if current_tab == self.text_only_tab:
                self.session_state.intent = "Help write a clear, complete procedure text"
            elif current_tab == self.text_json_tab:
                self.session_state.intent = "Help write correct procedure text to generate valid JSON"
            elif current_tab == self.json_code_tab:
                self.session_state.intent = "Help generate correct test code from JSON procedure"
            elif hasattr(self, 'traceability_tab') and current_tab == self.traceability_tab:
                self.session_state.intent = "Help verify traceability between procedure and code"
            else:
                self.session_state.intent = "General assistance with test procedure development"
        
        # Get current active tab
        current_tab = self.tab_widget.currentWidget()
        
        # Check if chat is enabled for this tab
        tab_id = getattr(current_tab, 'tab_id', None)
        if tab_id and hasattr(self, 'task_config_manager') and self.task_config_manager:
            chat_config = self.task_config_manager.get_chat_config(tab_id)
            if not chat_config.enabled:
                self.dock.chat_panel.add_message(
                    "system", "Chat is disabled for this tab. Enable it in Settings → Chat."
                )
                return
        
        try:
            current_tab._run_task_async(LLMTask.AD_HOC_CHAT, user_message=message)
        except Exception as e:
            log.error(f"Error executing chat task: {e}", exc_info=True)
            QMessageBox.critical(self, "Task Error", f"Failed to execute task: {str(e)}")
    
    def _play_notification_sound(self) -> None:
        """Play a system notification sound when the LLM finishes."""
        log.debug("Playing notification sound...")
        try:
            import winsound
        except ImportError:
            log.warning("winsound not available (non-Windows platform)")
            return
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            log.debug("Notification sound played (MessageBeep)")
        except Exception:
            try:
                winsound.Beep(1000, 200)
                log.debug("Notification sound played (Beep fallback)")
            except Exception as e:
                log.warning(f"Sound playback failed: {e}")

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
            self.tab_widget.setCurrentWidget(tab)
    
    def open_test(self, path: Path):
        """Open a test folder programmatically."""
        self._on_test_opened(path)
    
    def set_project_root(self, path: Path):
        """Set project root programmatically."""
        self.workspace_widget.set_project_root(path)
    
    def closeEvent(self, event):
        """Handle window close."""
        # Save session state (validation_issues are already in session_state)
        if hasattr(self, 'session_state') and self.session_state:
            try:
                self.session_state.save()
            except Exception as e:
                log.warning(f"Failed to save session state on close: {e}")
        
        # Check for unsaved changes (syncs editors + prompts)
        if self._check_unsaved_changes():
            event.ignore()
            return
        
        # Check artifact coherence before closing
        if self._check_artifact_coherence():
            event.ignore()
            return
        
        # Cancel any running LLM workers
        self._cancel_all_llm_workers()
        
        # Stop all tab backends
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'tab_context') and tab.tab_context._backend:
                log.debug(f"Stopping backend for {tab.__class__.__name__}")
                tab.tab_context._backend.stop()
        
        # Stop server manager if exists
        if hasattr(self, '_server_manager') and self._server_manager:
            log.info("Stopping OpenCode server manager...")
            self._server_manager.stop()
        
        event.accept()
