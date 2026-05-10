"""
Main Window - Primary application window.

Implements the main UI structure from Section 9.
"""

from pathlib import Path
from typing import Optional
import json
import logging
import os
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QStatusBar, QMenuBar, QMenu, QToolBar, QMessageBox, QLabel, QDockWidget,
    QFileDialog, QDialog
)
from PySide6.QtCore import Qt, Signal, Slot, QFileSystemWatcher
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QCursor

from .core import (
    ArtifactManager, SessionState, ProjectManager, ArtifactType,
    JsonValidator, CodeValidator
)
from .core.task_config import TaskConfigManager
from .llm import (
    LLMBackend,
    LLMRequest, LLMTask,
    OpenCodeConfig, ExternalAPIConfig,
    LLMWorker,
)
from .llm.server_manager import OpenCodeServerManager
from .llm.backend_factory import (
    BackendFactory, BackendConfig,
    BACKEND_TYPE_OPENCODE, BACKEND_TYPE_EXTERNAL_API, BACKEND_TYPE_NONE
)
from .tabs import (
    WorkspaceTab, TextOnlyTab, TextJsonTab, JsonCodeTab, TraceabilityTab
)
from .dock import DockWidget
from .dialogs import SettingsDialog, CleanDialog, NewProjectDialog, load_settings

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
        
        # Initialize managers
        log.debug("Initializing managers...")
        self.project_manager = ProjectManager()
        self.artifact_manager: Optional[ArtifactManager] = None
        self.session_state: Optional[SessionState] = None
        # self.chat_history: Optional[ChatHistoryManager] = None

        # File watcher for project's config.json — drives live refresh of
        # parser-button visibility when the parent app's Config dialog (or
        # any external editor) writes the file.
        self._config_watcher = QFileSystemWatcher(self)
        self._config_watcher.fileChanged.connect(self._on_config_file_changed)
        self._watched_config_path: Optional[Path] = None
        
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
        self._cli_args_processed = True
        
        # Process CLI arguments to load project/test
        self._process_cli_arguments()
    
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

                # Detect rules
                self.project_manager.detect_rules_root()
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

    
    def _init_llm_backend(self):
        """Initialize LLM backend infrastructure.
        
        Creates server manager (for OpenCode) and backend factory.
        Each tab will create its own backend via the factory.
        """
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
        
        log.info(f"Backend infrastructure initialized: type={config.backend_type}")
        
        # Update LLM status display
        self._update_llm_status()
        
        # Update all tab contexts with new factory (if tabs are already initialized)
        self._update_all_tab_contexts()
    
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
            )
            return BackendConfig(
                backend_type=BACKEND_TYPE_OPENCODE,
                opencode=opencode_config,
                custom_prompts={},  # Deprecated: now handled by TaskConfigManager
                custom_output_format=custom_output_format,
            )
        elif backend_type == "external_api":
            config_dict = self._settings.get("external_api", {})
            log.debug(f"External API config: {config_dict}")
            model_name = config_dict.get("model", "gpt-4")
            external_api_config = ExternalAPIConfig(
                base_url=config_dict.get("url", "https://api.openai.com/v1"),
                model=model_name,
                api_key=config_dict.get("key") or None,
                temperature=common_llm.get("temperature", 0.2),
                request_timeout=common_llm.get("request_timeout", 120.0),
                retry_count=config_dict.get("retry_count", 2),
            )
            return BackendConfig(
                backend_type=BACKEND_TYPE_EXTERNAL_API,
                external_api=external_api_config,
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
        
        file_menu.addSeparator()
        
        settings_action = QAction("Se&ttings...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._on_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Edit menu
        edit_menu = menubar.addMenu("&Edit")
        
        clean_action = QAction("&Clean Files...", self)
        clean_action.triggered.connect(self._on_clean)
        edit_menu.addAction(clean_action)
        
        edit_menu.addSeparator()
        
        self.mark_sync_action = QAction("Mark Artifacts In &Sync", self)
        self.mark_sync_action.setToolTip("Acknowledge that procedure.json and test.py are coherent")
        self.mark_sync_action.triggered.connect(self._on_sync_indicator_clicked)
        edit_menu.addAction(self.mark_sync_action)

        edit_menu.addSeparator()

        self.validate_action = QAction("&Validate Procedure", self)
        self.validate_action.setShortcut("Ctrl+Shift+V")
        self.validate_action.setToolTip(
            "Run the deterministic validator (R1 text↔JSON, R3 schema, R4 topology) "
            "against the current artifacts and show findings in the dock panel."
        )
        self.validate_action.triggered.connect(self._on_validate_procedure)
        edit_menu.addAction(self.validate_action)

        
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
        
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)
    
    def _setup_central_widget(self):
        """Setup central widget with tabs."""
        # Tab widget (no container needed)
        self.tab_widget = QTabWidget()
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
        
        self.setCentralWidget(self.tab_widget)
    
    def _setup_workspace_dock(self):
        """Setup workspace as a left sidebar dock."""
        # Create workspace widget
        self.workspace_widget = WorkspaceTab(self)
        self.workspace_widget.test_opened.connect(self._on_test_opened)
        self.workspace_widget.test_deleted.connect(self._on_test_deleted)
        
        # Create dock widget
        self.workspace_dock = QDockWidget("Workspace", self)
        self.workspace_dock.setWidget(self.workspace_widget)
        
        # Dock configuration
        self.workspace_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.workspace_dock.setFeatures(
            QDockWidget.DockWidgetClosable |    # Can close
            QDockWidget.DockWidgetMovable       # Can move
        )
        
        # Add to main window
        self.addDockWidget(Qt.LeftDockWidgetArea, self.workspace_dock)
        
        # Default size
        self.workspace_dock.setMinimumWidth(200)
        self.workspace_dock.resize(250, self.height())
        
        # Connect toggle action
        self.toggle_workspace_action.setChecked(self.workspace_dock.isVisible())
        self.toggle_workspace_action.triggered.connect(self._on_toggle_workspace)
        self.workspace_dock.visibilityChanged.connect(self._on_workspace_visibility_changed)
    
    def _setup_dock(self):
        """Setup the dock widget."""
        self.dock = DockWidget(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        
        # Update context limit based on backend config
        self._update_context_limit()
        
        # Connect chat signals
        self.dock.chat_panel.message_sent.connect(self._on_chat_message)
        self.dock.chat_panel.reset_requested.connect(self._on_reset_session)
        self.dock.chat_panel.cancel_requested.connect(self._on_cancel_llm)
        
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

    def _on_validate_procedure(self):
        """Run the deterministic validator on the current on-disk artifacts.

        Independent of the LLM-loop FSM: reads procedure_text / procedure.json
        / test.py from the artifact_manager, runs every applicable round-trip
        (R1 text↔JSON, R3 schema, R4 topology, R2 JSON↔code when inventory is
        available), and pushes the structured findings into the dock panel.
        Both errors and warnings are displayed so the operator sees soft codes
        like ``META_KEY_ORDER`` or ``EXP_PCT_DEGENERATE`` alongside hard errors.
        """
        from .llm.validator_dispatch import validate_current_state

        if not self.artifact_manager:
            self.status_bar.showMessage(
                "Validate Procedure: no test loaded.", 4000,
            )
            return

        text = self.artifact_manager.procedure_text.content or None
        json_str = self.artifact_manager.procedure_json.content or None
        code = self.artifact_manager.test_code.content or None
        project_root = (
            self.project_manager.project_root
            if self.project_manager else None
        )

        outcome = validate_current_state(
            project_root=project_root,
            text=text,
            json_str=json_str,
            code=code,
        )

        from .llm.validator_dispatch import render_validation_outcome_summary
        if outcome.skipped:
            self.dock.show_validation_result_from_list([])
        else:
            self.dock.show_validation_result_from_list(
                [issue.to_dock_dict() for issue in outcome.issues]
            )
        self.status_bar.showMessage(
            render_validation_outcome_summary(outcome), 5000,
        )

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
            if self.artifact_manager.procedure_json.exists_on_disk:
                self.tab_widget.setCurrentWidget(self.json_code_tab)
            elif self.artifact_manager.procedure_text.exists_on_disk:
                self.tab_widget.setCurrentWidget(self.text_json_tab)
            else:
                self.tab_widget.setCurrentWidget(self.text_json_tab)
        
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
        elif backend_type == BACKEND_TYPE_EXTERNAL_API:
            model = "unknown"
            if self._backend_factory.config.external_api:
                model = self._backend_factory.config.external_api.model
            self.llm_status.setText(f"LLM: External API ({model})")
        else:
            self.llm_status.setText("LLM: Disabled")
    
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
        dialog = NewProjectDialog(self)
        
        if dialog.exec() != QDialog.Accepted:
            return
        
        # Get project configuration from dialog
        config = dialog.get_project_config()
        project_path = config["path"]
        create_config = config["create_config"]
        create_readme = config["create_readme"]
        
        # Create project structure
        success = self.project_manager.create_project_structure(
            project_path,
            create_config=create_config,
            create_readme=create_readme
        )
        
        if not success:
            QMessageBox.critical(
                self,
                "Project Creation Failed",
                f"Failed to create project at:\n{project_path}\n\n"
                "Check the logs for more details."
            )
            return
        
        # Project was created and set as current root by create_project_structure
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
        self.json_code_tab.refresh_code_parser_button()
        self._watch_project_config()
        
        # Show workspace dock if hidden
        if self.workspace_dock.isHidden():
            self.workspace_dock.show()
        
        # Show success message
        QMessageBox.information(
            self,
            "Project Created",
            f"Project created successfully at:\n{project_path}\n\n"
            "You can now create test folders using the Workspace tab."
        )
    
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
            self.workspace_widget._load_test_list()
            self.workspace_widget.new_test_btn.setEnabled(True)

            # Detect rules
            self.project_manager.detect_rules_root()

            # Switch task configurations to the newly-opened project — this
            # clears the cache under lock, runs any legacy tab_contexts.json
            # migration, and re-reads the workflows section.
            self.task_config_manager.reload(self.project_manager.project_root)

            # Update status bar indicators
            self._update_project_rules_indicators()
            self.text_json_tab.refresh_parser_button()
            self.json_code_tab.refresh_code_parser_button()
            self._watch_project_config()
            
            # Show workspace dock if hidden
            if self.workspace_dock.isHidden():
                self.workspace_dock.show()
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
                self.json_code_tab.refresh_code_parser_button()
                self._watch_project_config()
                
                # Show workspace dock
                if self.workspace_dock.isHidden():
                    self.workspace_dock.show()
            else:
                QMessageBox.warning(
                    self,
                    "Invalid Project",
                    "Selected folder does not appear to be a valid project root.\n\n"
                    "A valid project should contain a 'tests/' or 'config/' folder."
                )

    def _watch_project_config(self) -> None:
        """Watch the active project's config.json for live parser refresh.

        Removes any previous watch path before adding the new one. Safe
        to call repeatedly (e.g. after switching projects).
        """
        config_dir = self.project_manager.get_config_dir()
        new_path = (config_dir / "config.json") if config_dir else None

        if self._watched_config_path is not None:
            self._config_watcher.removePath(str(self._watched_config_path))
            self._watched_config_path = None

        if new_path and new_path.exists():
            self._config_watcher.addPath(str(new_path))
            self._watched_config_path = new_path

    def _on_config_file_changed(self, path: str) -> None:
        """Refresh parser-driven UI when the project's config.json changes.

        Some editors atomic-write (delete + recreate), which silently
        drops the watch — re-add the path defensively after each event.
        """
        self.text_json_tab.refresh_parser_button()
        self.json_code_tab.refresh_code_parser_button()

        p = Path(path)
        if p.exists() and str(p) not in self._config_watcher.files():
            self._config_watcher.addPath(str(p))
    
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
        )
        if dialog.exec():
            self._settings = dialog.get_settings()
            self._init_llm_backend()
            self._apply_settings()
            # Refresh button labels after settings change
            self.refresh_all_button_labels()
    
    def _on_clean(self):
        """Open clean dialog."""
        if not self.artifact_manager:
            QMessageBox.warning(self, "No Test", "Please open a test first.")
            return
        
        deleted = CleanDialog.clean_test_folder(
            self.artifact_manager.test_dir,
            self
        )
        
        if deleted:
            # Reload artifacts
            self.artifact_manager.load_all()
            self.text_only_tab.load_content()
            self.text_json_tab.load_content()
            self.json_code_tab.load_content()
            self._update_status_indicators()
            # Refresh workspace test list to show updated artifact status
            self.workspace_widget.refresh()
    
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
