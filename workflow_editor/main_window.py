"""
Main Window - Primary application window.

Implements the main UI structure from Section 9.
"""

from pathlib import Path
from typing import Optional
import logging
import json
import os
from datetime import datetime
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QStatusBar, QMenuBar, QMenu, QToolBar, QMessageBox, QLabel, QDockWidget,
    QFileDialog, QDialog
)
from PySide6.QtCore import Qt, Signal, Slot, QThread, QUrl
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QCursor, QDesktopServices

from .core import (
    ArtifactManager, SessionState, ProjectManager, ArtifactType,
    JsonValidator, CodeValidator
)
from .core.task_config import TaskConfigManager
from .llm import (
    LLMBackend, NoneBackend, OpenCodeBackend, ExternalAPIBackend,
    LLMRequest, LLMResponse, LLMTask, PromptBuilder, ResponseParser,
    OpenCodeConfig, ExternalAPIConfig, LLMProposal
)
from .llm.server_manager import OpenCodeServerManager
from .llm.backend_factory import (
    BackendFactory, BackendConfig,
    BACKEND_TYPE_OPENCODE, BACKEND_TYPE_EXTERNAL_API, BACKEND_TYPE_NONE
)
from .tabs import (
    WorkspaceTab, TextJsonTab, JsonCodeTab, TraceabilityTab
)
from .dock import DockWidget
from .dialogs import SettingsDialog, DiffViewer, CleanDialog, NewProjectDialog, load_settings

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


class LLMWorker(QThread):
    """Worker thread for LLM requests."""
    
    finished = Signal(object)  # LLMResponse
    error = Signal(str)
    
    def __init__(self, backend: LLMBackend, request: LLMRequest, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._request = request
        self._cancelled = False
    
    def cancel(self):
        """Request cancellation."""
        self._cancelled = True
        log.info("LLMWorker: Cancellation requested")
    
    def run(self):
        try:
            log.debug(f"LLMWorker.run() starting - backend={self._backend.__class__.__name__}, task={self._request.task}")
            response = self._backend.send_request(self._request)
            
            # Check if cancelled before emitting
            if self._cancelled:
                log.debug("LLMWorker: Request was cancelled")
                self.error.emit("Request cancelled by user")
                return
            
            log.debug(f"LLMWorker.run() got response - raw_response length={len(response.raw_response)}, success={response.success}")
            if not response.success:
                log.warning(f"LLMWorker.run() response failed - error: {response.error_message}")
            self.finished.emit(response)
        except Exception as e:
            if not self._cancelled:
                log.error(f"LLMWorker.run() exception: {e}", exc_info=True)
                self.error.emit(str(e))



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
        
        # Initialize task config manager
        config_dir = Path(__file__).parent.parent / "config"
        config_path = config_dir / "tab_contexts.json"
        self.task_config_manager = TaskConfigManager(config_path)
        
        # Initialize LLM
        self._settings = load_settings()
        log.debug(f"Settings loaded: {list(self._settings.keys())}")
        self._init_llm_backend()
        
        # Prompt builder and response parser
        self._prompt_builder = PromptBuilder(
            task_config_manager=self.task_config_manager,
            tab_id=None  # Main window uses None for legacy tasks
        )
        self._response_parser = ResponseParser()
        
        # Active LLM worker
        self._llm_worker: Optional[LLMWorker] = None
        
        # Progress animation for LLM status
        self._progress_timer = None
        self._progress_dots = 0
        
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
                temperature=common_llm.get("temperature", 0.2),
                max_tokens=common_llm.get("max_tokens", 16384),
                request_timeout=common_llm.get("request_timeout", 120.0),
                retry_count=config_dict.get("retry_count", 2),
                # api_key loaded from environment, not stored in settings
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
    def llm_backend(self) -> LLMBackend:
        """DEPRECATED: Get a backend instance.
        
        This property is deprecated. New code should use tab_context.backend instead.
        This exists for backward compatibility with legacy run_llm_task() method.
        
        Returns:
            A backend instance (creates one via factory if needed)
        """
        import warnings
        warnings.warn(
            "MainWindow.llm_backend is deprecated. Use tab_context.backend instead.",
            DeprecationWarning,
            stacklevel=2
        )
        # Return first available tab's backend for legacy callers
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'tab_context') and tab.tab_context:
                return tab.tab_context.backend
        # Fallback: create a temporary backend via factory
        if hasattr(self, '_backend_factory') and self._backend_factory:
            return self._backend_factory.create_backend("_legacy")
        # Ultimate fallback
        return NoneBackend()
    
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
        
        # Also cancel the main window's worker if any
        if self._llm_worker and self._llm_worker.isRunning():
            self._llm_worker.cancel()
            log.debug("Cancelled main window LLM worker")
    
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
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        
        # Create tabs (workspace moved to dock)
        # Combined tabs showing paired artifacts
        self.text_json_tab = TextJsonTab(self)
        self.json_code_tab = JsonCodeTab(self)
        self.traceability_tab = TraceabilityTab(self)
        
        # Add tabs in workflow order: Text-JSON → JSON-Code → Traceability
        self.tab_widget.addTab(self.text_json_tab, "Text-JSON")
        self.tab_widget.addTab(self.json_code_tab, "JSON-Code")
        self.tab_widget.addTab(self.traceability_tab, "Traceability")
        
        self.setCentralWidget(self.tab_widget)
    
    def _setup_workspace_dock(self):
        """Setup workspace as a left sidebar dock."""
        # Create workspace widget
        self.workspace_widget = WorkspaceTab(self)
        self.workspace_widget.test_opened.connect(self._on_test_opened)
        
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
        """Update chat panel context limit based on backend config."""
        if not hasattr(self, 'dock') or not hasattr(self.dock, 'chat_panel'):
            return
        
        if not hasattr(self, '_backend_factory') or self._backend_factory is None:
            return
        
        # Get model name from config
        model_name = None
        config = self._backend_factory.config
        if config.backend_type == BACKEND_TYPE_EXTERNAL_API and config.external_api:
            model_name = config.external_api.model
        elif config.backend_type == BACKEND_TYPE_OPENCODE and config.opencode:
            model_name = config.opencode.model
        
        if model_name:
            context_limit = self._extract_context_limit(model_name)
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
        
        # Also update project/rules indicators
        self._update_project_rules_indicators()
    
    def _update_menu_state(self):
        """Update menu items based on artifact availability."""
        pass
    
    # ==================== Event Handlers ====================
    
    def _on_tab_changed(self, index: int):
        """Handle tab change."""
        # Sync and notify deactivating tabs
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if i != index:
                # Sync editor content to artifact_manager before deactivating
                # so the arriving tab sees up-to-date shared artifacts (e.g. JSON)
                if hasattr(tab, 'sync_editors_to_artifacts'):
                    tab.sync_editors_to_artifacts()
                if hasattr(tab, 'on_deactivated'):
                    tab.on_deactivated()
        
        tab = self.tab_widget.widget(index)
        if hasattr(tab, 'on_activated') and self.artifact_manager is not None:
            tab.on_activated()
        
        # Switch chat context to current tab's TabContext
        if hasattr(self, 'dock') and hasattr(tab, 'tab_context'):
            self.dock.chat_panel.switch_context(tab.tab_context)
            self.dock.session_viewer.switch_context(tab.tab_context)
            self.dock.findings_panel.switch_context(tab.tab_context)
            self.dock.raw_viewer.switch_context(tab.tab_context)
        
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
        
        # Initialize managers for this test
        self.artifact_manager = ArtifactManager()
        self.artifact_manager.set_test_dir(path)
        self.artifact_manager.load_all()  # Load existing files from disk
        
        # Initialize session state (empty, not with path)
        self.session_state = SessionState()
        self.session_state.set_file_path(path)
        self.session_state.load()  # Load existing session data from disk if it exists
        
        # ChatHistoryManager removed: chat history is now per-tab only
        
        # Update tab contexts with real managers (fixes None reference issue)
        if hasattr(self.text_json_tab, 'tab_context'):
            self.text_json_tab.tab_context.update_managers(self.artifact_manager, self.session_state)
        if hasattr(self.json_code_tab, 'tab_context'):
            self.json_code_tab.tab_context.update_managers(self.artifact_manager, self.session_state)
        
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
        self.text_json_tab.load_content()
        self.json_code_tab.load_content()
        
        # Refresh dock panels with session data
        self.dock.refresh_session()
        
        # Update status indicators
        self._update_status_indicators()
        self._update_menu_state()
        
        # Refresh session viewer
        self.dock.refresh_session()
        
        # Switch to appropriate tab based on what exists
        if self.artifact_manager.procedure_json.exists_on_disk:
            self.tab_widget.setCurrentWidget(self.json_code_tab)
        elif self.artifact_manager.procedure_text.exists_on_disk:
            self.tab_widget.setCurrentWidget(self.text_json_tab)
        else:
            self.tab_widget.setCurrentWidget(self.text_json_tab)
    
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
        if self._llm_worker and self._llm_worker.isRunning():
            log.info("User requested LLM cancellation")
            self._llm_worker.cancel()
            self._llm_worker.wait(1000)  # Wait up to 1 second
            
            # Update UI
            self.dock.chat_panel.set_llm_active(False)
            self.dock.chat_panel.remove_thinking_message()
            self.dock.chat_panel.add_system_message("Request cancelled by user")
            self.status_bar.showMessage("LLM request cancelled", 3000)
    
    def _extract_context_limit(self, model_name: str) -> int:
        """Extract context limit from model name."""
        import re
        
        # Handle None or empty model name
        if not model_name:
            return 16384  # Safe default
        
        # Try to extract number + k/K pattern (e.g., "16k", "32K", "128k")
        match = re.search(r'(\d+)k', model_name.lower())
        if match:
            return int(match.group(1)) * 1024
        
        # Default fallback based on common models
        if "gpt-4" in model_name.lower():
            return 128000  # GPT-4 Turbo
        elif "gpt-3.5" in model_name.lower():
            return 16384
        
        return 16384  # Safe default
    
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
        
        # Update status bar indicators
        self._update_project_rules_indicators()
        
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
            
            # Update status bar indicators
            self._update_project_rules_indicators()
            
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
                
                # Update status bar indicators
                self._update_project_rules_indicators()
                
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
        
        self._update_status_indicators()
        # Refresh workspace test list to update artifact indicators
        self.workspace_widget.refresh()
        self.status_bar.showMessage("Saved", 2000)
    
    def _on_save_all(self):
        """Save all dirty artifacts across all tabs.
        
        Syncs all tab editors first, then saves via each tab's
        save_all_artifacts() to properly reset dirty flags.
        """
        if not self.artifact_manager:
            return
        
        # Save via each tab (sync + save + reset dirty + update status)
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'save_all_artifacts'):
                tab.save_all_artifacts()
        
        if self.session_state:
            self.session_state.save()
        
        # Update indicators after save
        self._update_status_indicators()
        # Refresh workspace test list to update artifact indicators
        self.workspace_widget.refresh()
        self.status_bar.showMessage("All saved", 2000)
    
    def _check_unsaved_changes(self) -> bool:
        """Check for unsaved changes and prompt user.
        
        Syncs editors first, then checks artifact dirty state.
        
        Returns:
            True if the user cancelled (caller should abort), False otherwise
        """
        if not self.artifact_manager:
            return False
        
        # Sync editors to catch un-saved editor changes
        for tab in self._get_llm_tabs():
            if hasattr(tab, 'sync_editors_to_artifacts'):
                tab.sync_editors_to_artifacts()
        
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
        dialog = SettingsDialog(self.task_config_manager, self)
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
            self.artifact_manager.test_folder,
            self
        )
        
        if deleted:
            # Reload artifacts
            self.artifact_manager.load_all()
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
    
    def _on_cancel_llm_task(self):
        """Cancel current LLM task."""
        if self._llm_worker and self._llm_worker.isRunning():
            self._llm_worker.cancel()
            self.llm_status.setText("Cancelled")
            log.info("LLM task cancelled by user")
    
    def _on_chat_message(self, message: str):
        """Handle chat message from user."""
        # Set intent based on current tab
        if self.session_state:
            current_tab = self.tab_widget.currentWidget()
            if current_tab == self.text_json_tab:
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
        
        # Check if tab has modern async task execution
        if hasattr(current_tab, '_run_task_async'):
            log.info(f"Chat message using MODERN path via {current_tab.__class__.__name__}._run_task_async()")
            try:
                # Use the modern path: delegate to tab's _run_task_async
                current_tab._run_task_async(LLMTask.AD_HOC_CHAT, user_message=message)
            except Exception as e:
                log.error(f"Error in modern async path: {e}", exc_info=True)
                QMessageBox.critical(
                    self,
                    "Task Error",
                    f"Failed to execute task: {str(e)}"
                )
        else:
            # Fallback to legacy path for tabs without _run_task_async
            log.info(f"Chat message using LEGACY path via run_llm_task() for {current_tab.__class__.__name__}")
            self.run_llm_task("ad_hoc_chat", user_message=message)
    
    # ==================== LLM Task Execution ====================
    
    def run_llm_task(self, task_name: str, strict: bool = True, user_message: str = None):
        """
        Run an LLM task.
        
        **DEPRECATED**: This method is deprecated in favor of tab-level _run_task_async().
        It remains for backward compatibility with legacy tabs (json_tab, code_tab, text_tab).
        New code should use _run_task_async() directly from the tab.
        
        TODO: Remove this method after all tabs are migrated to _run_task_async().
        """
        log.warning(
            f"⚠️  DEPRECATED: run_llm_task() called for task '{task_name}'. "
            "This method should be replaced with tab._run_task_async() for better maintainability."
        )
        
        if not self.artifact_manager:
            log.warning("run_llm_task called without artifact_manager")
            QMessageBox.warning(self, "No Test", "Please open a test first.")
            return
        
        if self._llm_worker and self._llm_worker.isRunning():
            log.warning("LLM request already in progress")
            QMessageBox.warning(self, "Busy", "An LLM request is already in progress.")
            return
        
        log.info(f"Running LLM task: {task_name} (strict={strict})")
        
        # Get the task enum
        try:
            task = LLMTask[task_name.upper()]
        except KeyError:
            # Try case-insensitive match
            for t in LLMTask:
                if t.name.lower() == task_name.lower():
                    task = t
                    break
            else:
                self.status_bar.showMessage(f"Unknown task: {task_name}", 3000)
                return
        
        # Build context
        context = self._build_context()
        log.debug(f"Context: {context}")
        
        # Only include artifacts that have local changes or need resync with LLM
        include_json = self.artifact_manager.should_include_in_prompt(ArtifactType.PROCEDURE_JSON)
        include_code = self.artifact_manager.should_include_in_prompt(ArtifactType.TEST_CODE)
        include_text = self.artifact_manager.should_include_in_prompt(ArtifactType.PROCEDURE_TEXT)
        
        # Create LLM request with all needed fields
        request = LLMRequest(
            task=task,
            strict_mode=strict,
            procedure_json=self.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON),
            test_code=self.artifact_manager.get_content(ArtifactType.TEST_CODE),
            procedure_text=self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT),
            include_json=include_json,
            include_code=include_code,
            include_text=include_text,
            rules_content=self._get_rules_content(),
            session_summary=self.session_state.get_summary_for_llm() if self.session_state else "",
            user_message=user_message,
        )
        
        # Mark included artifacts as synced (they're being sent to LLM now)
        if include_json:
            self.artifact_manager.mark_synced(ArtifactType.PROCEDURE_JSON)
        if include_code:
            self.artifact_manager.mark_synced(ArtifactType.TEST_CODE)
        if include_text:
            self.artifact_manager.mark_synced(ArtifactType.PROCEDURE_TEXT)
        
        # Build prompt from request
        prompt = self._prompt_builder.build(request)
        log.debug(f"Prompt built: {len(prompt)} chars")
        
        # Store prompt for chat history
        self._current_prompt = prompt
        
        # Show in chat - always as user message for conversational flow
        if user_message:
            self.dock.chat_panel.add_message("user", user_message, full_prompt=prompt)
        else:
            # Extract task description from instruction (first line after "Task:")
            # Get task instruction from prompt builder
            instruction = PromptBuilder.get_default_prompts().get(task.value, "")
            # Get first line that starts with "Task:"
            for line in instruction.split('\n'):
                line = line.strip()
                if line.startswith('Task:'):
                    task_description = line[5:].strip()  # Remove "Task:" prefix
                    self.dock.chat_panel.add_message("user", task_description, full_prompt=prompt)
                    break
            else:
                # Fallback if no "Task:" line found
                self.dock.chat_panel.add_message("user", task.name.replace('_', ' ').title(), full_prompt=prompt)
        
        # Update status with progress animation
        self.llm_status.setText("LLM: Working")
        self._start_progress_animation()
        self.dock.chat_panel.set_enabled(False)
        
        # Add temporary "thinking" message in chat
        self._thinking_message_widget = None
        self.dock.chat_panel.add_thinking_message()
        
        # Store task info for response handling
        self._current_task = task
        self._strict_mode = strict
        
        # Run in worker thread
        self._llm_worker = LLMWorker(self.llm_backend, request, self)
        self._llm_worker.finished.connect(self._on_llm_finished)
        self._llm_worker.error.connect(self._on_llm_error)
        
        # Enable cancel button while request is active
        self.dock.chat_panel.set_llm_active(True)
        
        self._llm_worker.start()
    
    def _build_context(self) -> dict:
        """Build context for LLM request."""
        return {
            "test_name": self.artifact_manager.test_dir.name if self.artifact_manager and self.artifact_manager.test_dir else "",
            "has_json": self.artifact_manager.procedure_json.exists_on_disk if self.artifact_manager else False,
            "has_code": self.artifact_manager.test_code.exists_on_disk if self.artifact_manager else False,
            "has_text": self.artifact_manager.procedure_text.exists_on_disk if self.artifact_manager else False,
        }
    
    def _get_rules_content(self) -> str:
        """Get concatenated rules content."""
        if not self.project_manager.rules_root:
            return ""
        
        rules = []
        for md_file in self.project_manager.rules_root.glob("*.md"):
            try:
                rules.append(md_file.read_text(encoding='utf-8'))
            except Exception:
                pass
        
        return "\n\n---\n\n".join(rules)
    
    def _save_debug_file(self, raw_response: str, error_type: str) -> str:
        """Save debug file with raw response and truncated prompt for debugging.
        
        Args:
            raw_response: The raw LLM response (or empty string)
            error_type: Type of error (e.g., 'empty_response', 'parse_error')
            
        Returns:
            Path to the saved debug file
        """
        if not self.artifact_manager or not self.artifact_manager.test_dir:
            return ""
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f".llm_debug_{timestamp}.json"
        filepath = self.artifact_manager.test_dir / filename
        
        # Truncate rules in prompt to reduce file size
        prompt = getattr(self, '_current_prompt', '')
        if prompt:
            # Replace rules content with placeholder showing size
            import re
            rules_match = re.search(r'## RULES ##\s*(.*?)\s*(?=## |$)', prompt, re.DOTALL)
            if rules_match:
                rules_content = rules_match.group(1)
                prompt = prompt.replace(rules_content, f"<rules: {len(rules_content)} bytes>")
        
        debug_data = {
            'timestamp': datetime.now().isoformat(),
            'error_type': error_type,
            'task': getattr(self, '_current_task', None).name if hasattr(self, '_current_task') and self._current_task else 'unknown',
            'raw_response': raw_response,
            'prompt_truncated': prompt,
        }
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(debug_data, f, indent=2, ensure_ascii=False)
            
            # Open in default editor
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(filepath)))
            
            return str(filepath)
        except Exception as e:
            log.error(f"Failed to save debug file: {e}")
            return ""
    
    def _start_progress_animation(self):
        """Start animated progress dots for LLM status."""
        from PySide6.QtCore import QTimer
        self._progress_dots = 0
        if self._progress_timer:
            self._progress_timer.stop()
        self._progress_timer = QTimer()
        self._progress_timer.timeout.connect(self._update_progress_animation)
        self._progress_timer.start(500)  # Update every 500ms
    
    def _stop_progress_animation(self):
        """Stop progress animation."""
        if self._progress_timer:
            self._progress_timer.stop()
            self._progress_timer = None
        self.llm_status.setText("")
        
        # Remove thinking message from chat
        self.dock.chat_panel.remove_thinking_message()
    
    def _update_progress_animation(self):
        """Update progress dots animation."""
        self._progress_dots = (self._progress_dots + 1) % 4
        dots = "." * self._progress_dots
        self.llm_status.setText(f"LLM: Working{dots}")
        
        # Update thinking message in chat
        self.dock.chat_panel.update_thinking_message(dots)
    
    @Slot(object)
    def _on_llm_finished(self, response: LLMResponse):
        """Handle LLM response."""
        log.info("LLM task completed")
        log.debug(f"Raw response length: {len(response.raw_response)} chars")
        
        self._stop_progress_animation()
        self.dock.chat_panel.set_enabled(True)
        
        # Disable cancel button
        self.dock.chat_panel.set_llm_active(False)
        
        # Show raw response
        self.dock.raw_viewer.show_response(response.raw_response)
        if self.dock.raw_viewer.should_auto_show():
            self.dock.show_raw()
        
        # Check for context length exceeded
        if response.context_exceeded:
            error_msg = "⚠️ Context length exceeded - session history too long"
            self.dock.chat_panel.add_system_message(error_msg, full_prompt=self._current_prompt)
            reply = QMessageBox.question(
                self,
                "Context Length Exceeded",
                "The conversation context is too long for the model.\n\n"
                "Would you like to reset the session and clear the chat history?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                self._on_reset_session()
            return
        
        # Check for empty response
        if not response.raw_response or len(response.raw_response.strip()) == 0:
            error_msg = "❌ Error: Received empty response from LLM"
            self.dock.chat_panel.add_system_message(error_msg, full_prompt=self._current_prompt)
            debug_file = self._save_debug_file(response.raw_response, "empty_response")
            QMessageBox.critical(
                self,
                "LLM Error",
                f"The LLM returned an empty response.\n\nDebug file saved: {debug_file}"
            )
            log.error("Empty LLM response")
            return
        
        # Parse response
        try:
            parsed = self._response_parser.parse(response.raw_response, self._current_task)
            log.debug(f"Parsed response: has_proposals={parsed.has_proposals}, has_issues={parsed.has_issues}")
        except Exception as e:
            error_msg = f"❌ Error parsing LLM response: {str(e)}"
            self.dock.chat_panel.add_system_message(error_msg, full_prompt=self._current_prompt)
            debug_file = self._save_debug_file(response.raw_response, "parse_error")
            QMessageBox.critical(
                self,
                "Parse Error",
                f"Failed to parse LLM response:\n{str(e)}\n\nDebug file saved: {debug_file}"
            )
            log.error(f"Parse error: {e}", exc_info=True)
            return
        
        # Build comprehensive chat message from all response components
        chat_message_parts = []
        
        # 1. Assistant message
        if parsed.assistant_message:
            chat_message_parts.append(parsed.assistant_message)
        
        # 2. Validation issues
        if parsed.has_issues:
            # Convert ValidationIssue objects to dict format for findings panel
            issues_as_dicts = [
                {
                    "message": issue.message,
                    "severity": issue.severity,
                    "location": issue.location,
                    "code": issue.code,
                    "suggested_fix": issue.suggested_fix
                }
                for issue in parsed.issues
            ]
            self.dock.show_validation_result_from_list(issues_as_dicts)
            
            # Add formatted issues to chat message
            issues_text = self._format_issues_for_chat(parsed.issues)
            chat_message_parts.append(issues_text)
            
            if self._strict_mode and parsed.has_errors:
                self.dock.chat_panel.add_system_message(
                    "Errors found. Fix issues before proceeding.",
                    full_prompt=self._current_prompt
                )
                return
        
        # 3. Proposals summary
        if parsed.has_proposals:
            proposals_summary = self._format_proposals_summary(parsed)
            chat_message_parts.append(proposals_summary)
        
        # 4. Open questions from session delta
        if parsed.session_delta and hasattr(parsed.session_delta, 'open_questions') and parsed.session_delta.open_questions:
            questions_text = self._format_open_questions_for_chat(parsed.session_delta.open_questions)
            chat_message_parts.append(questions_text)
        
        # Display complete message in chat
        if chat_message_parts:
            complete_message = "\n\n".join(chat_message_parts)
            self.dock.chat_panel.add_message(
                "assistant", 
                complete_message, 
                full_prompt=self._current_prompt, 
                full_response=response.raw_response,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens
            )
        
        # Handle session delta
        if parsed.session_delta and self.session_state:
            self.session_state.apply_delta(parsed.session_delta)
            self.session_state.save()
            self.dock.refresh_session()
        
        # Handle proposals - show as text messages in chat, then modal DiffViewer
        if parsed.has_proposals:
            if parsed.procedure_json and parsed.procedure_json.is_valid:
                self._handle_llm_proposal(parsed.procedure_json, "procedure.json", ArtifactType.PROCEDURE_JSON)
            if parsed.test_code and parsed.test_code.is_valid:
                self._handle_llm_proposal(parsed.test_code, "test.py", ArtifactType.TEST_CODE)
            if parsed.procedure_text and parsed.procedure_text.is_valid:
                self._handle_llm_proposal(parsed.procedure_text, "procedure_text.md", ArtifactType.PROCEDURE_TEXT)
        
        self.status_bar.showMessage("LLM task completed", 3000)
    
    def _handle_llm_proposal(self, proposal: LLMProposal, target: str, artifact_type: ArtifactType):
        """Show proposal as text message in chat, then handle with modal DiffViewer."""
        import json
        log.debug(f"Handling proposal for: {target}, mode={proposal.mode}")
        
        # Validate proposal
        is_valid, error = self._response_parser.validate_proposal(
            proposal, 
            target.replace('.json', '_json').replace('.py', '_code').replace('.md', '_text')
        )
        if not is_valid:
            log.warning(f"Invalid proposal for {target}: {error}")
            self.dock.chat_panel.add_system_message(f"Invalid proposal for {target}: {error}")
            return
        
        # Convert content to string
        if isinstance(proposal.content, dict):
            proposed_content = json.dumps(proposal.content, indent=2)
        else:
            proposed_content = str(proposal.content)
        
        # Show FULL content in chat (not truncated)
        self.dock.chat_panel.add_message(
            "assistant",
            f"📄 Proposal for {target}:\n```\n{proposed_content}\n```",
            full_prompt=self._current_prompt,
            full_response=None
        )
        
        # Show modal DiffViewer for accept/reject
        current = self.artifact_manager.get_content(artifact_type)
        accepted, final_content = DiffViewer.show_diff(
            current or "",
            proposed_content,
            f"Review Changes: {target}",
            self
        )
        
        if accepted:
            self.artifact_manager.set_content(artifact_type, final_content)
            self._refresh_current_tab()
            self.dock.chat_panel.add_system_message(f"✓ Applied changes to {target}")
            
            # Add system message to tab context for LLM history
            current_tab = self.tab_widget.currentWidget()
            if hasattr(current_tab, 'tab_context'):
                # Map artifact type to readable name
                artifact_name_map = {
                    ArtifactType.PROCEDURE_TEXT: "procedure_text",
                    ArtifactType.PROCEDURE_JSON: "procedure_json",
                    ArtifactType.TEST_CODE: "test_code"
                }
                artifact_name = artifact_name_map.get(artifact_type, str(artifact_type))
                current_tab.tab_context.add_system_message(
                    f"User accepted the proposal for {artifact_name}",
                    metadata={
                        "action": "accepted",
                        "artifact_type": artifact_name,
                        "timestamp": datetime.now().isoformat()
                    }
                )
        else:
            # Mark artifact as needing resync - LLM's context has the rejected proposal,
            # we need to re-send the actual current content on next request
            self.artifact_manager.mark_needs_resync(artifact_type)
            self.dock.chat_panel.add_system_message(f"✗ Rejected changes to {target}")
            
            # Add system message to tab context for LLM history
            current_tab = self.tab_widget.currentWidget()
            if hasattr(current_tab, 'tab_context'):
                # Map artifact type to readable name
                artifact_name_map = {
                    ArtifactType.PROCEDURE_TEXT: "procedure_text",
                    ArtifactType.PROCEDURE_JSON: "procedure_json",
                    ArtifactType.TEST_CODE: "test_code"
                }
                artifact_name = artifact_name_map.get(artifact_type, str(artifact_type))
                current_tab.tab_context.add_system_message(
                    f"User rejected the proposal for {artifact_name}",
                    metadata={
                        "action": "rejected",
                        "artifact_type": artifact_name,
                        "timestamp": datetime.now().isoformat()
                    }
                )
    
    def _handle_proposal(self, proposal: dict):
        """Handle a single proposal from LLM."""
        target = proposal.get("target")
        content = proposal.get("content")
        
        log.debug(f"Handling proposal for: {target}")
        
        if not target or not content:
            log.warning("Proposal missing target or content")
            return
        
        # Validate proposal
        if not self._response_parser.validate_proposal(proposal, target):
            log.warning(f"Invalid proposal for {target}")
            self.dock.chat_panel.add_system_message(
                f"Invalid proposal for {target} - skipped"
            )
            return
        
        # Determine artifact type
        if target == "procedure.json":
            artifact_type = ArtifactType.PROCEDURE_JSON
            current = self.artifact_manager.get_content(artifact_type)
        elif target == "test.py":
            artifact_type = ArtifactType.TEST_CODE
            current = self.artifact_manager.get_content(artifact_type)
        elif target == "procedure_text.md":
            artifact_type = ArtifactType.PROCEDURE_TEXT
            current = self.artifact_manager.get_content(artifact_type)
        else:
            return
        
        # Check if show diff is enabled
        if self._settings.get("behavior", {}).get("show_diff", True):
            accepted, final_content = DiffViewer.show_diff(
                current or "",
                content,
                f"Apply changes to {target}?",
                self
            )
            
            if accepted:
                self.artifact_manager.set_content(artifact_type, final_content)
                self._refresh_current_tab()
                self.dock.chat_panel.add_system_message(f"Applied changes to {target}")
                
                # Add system message to tab context for LLM history
                current_tab = self.tab_widget.currentWidget()
                if hasattr(current_tab, 'tab_context'):
                    # Map artifact type to readable name
                    artifact_name_map = {
                        ArtifactType.PROCEDURE_TEXT: "procedure_text",
                        ArtifactType.PROCEDURE_JSON: "procedure_json",
                        ArtifactType.TEST_CODE: "test_code"
                    }
                    artifact_name = artifact_name_map.get(artifact_type, str(artifact_type))
                    current_tab.tab_context.add_system_message(
                        f"User accepted the proposal for {artifact_name}",
                        metadata={
                            "action": "accepted",
                            "artifact_type": artifact_name,
                            "timestamp": datetime.now().isoformat()
                        }
                    )
            else:
                self.dock.chat_panel.add_system_message(f"Rejected changes to {target}")
                
                # Add system message to tab context for LLM history
                current_tab = self.tab_widget.currentWidget()
                if hasattr(current_tab, 'tab_context'):
                    # Map artifact type to readable name
                    artifact_name_map = {
                        ArtifactType.PROCEDURE_TEXT: "procedure_text",
                        ArtifactType.PROCEDURE_JSON: "procedure_json",
                        ArtifactType.TEST_CODE: "test_code"
                    }
                    artifact_name = artifact_name_map.get(artifact_type, str(artifact_type))
                    current_tab.tab_context.add_system_message(
                        f"User rejected the proposal for {artifact_name}",
                        metadata={
                            "action": "rejected",
                            "artifact_type": artifact_name,
                            "timestamp": datetime.now().isoformat()
                        }
                    )
        else:
            # Apply directly (not recommended but configurable)
            self.artifact_manager.set_content(artifact_type, content)
            self._refresh_current_tab()
            self.dock.chat_panel.add_system_message(f"Applied changes to {target}")
    
    def _refresh_current_tab(self):
        """Refresh the current tab."""
        current = self.tab_widget.currentWidget()
        if hasattr(current, 'refresh'):
            current.refresh()
    
    @Slot(str)
    def _on_llm_error(self, error: str):
        """Handle LLM error."""
        log.error(f"LLM error: {error}")
        self._stop_progress_animation()
        self.dock.chat_panel.set_enabled(True)
        
        # Disable cancel button
        self.dock.chat_panel.set_llm_active(False)
        
        # Remove thinking message
        self.dock.chat_panel.remove_thinking_message()
        
        # Show error in chat with better formatting
        error_message = f"❌ Error: {error}"
        self.dock.chat_panel.add_system_message(error_message, full_prompt=self._current_prompt)
        
        # Show in status bar
        self.status_bar.showMessage(f"LLM error: {error}", 10000)
        
        # Show error dialog for critical errors
        if "timeout" in error.lower() or "connection" in error.lower() or "no attribute" in error.lower():
            QMessageBox.critical(
                self,
                "LLM Error",
                f"The LLM request failed:\n\n{error}\n\nPlease check your backend configuration and try again."
            )
    
    # ==================== Public Interface ====================
    
    def switch_to_tab(self, tab_name: str):
        """Switch to a tab by name.
        
        Supported names: text_json, json_code, traceability.
        Legacy names (json, code, text) are mapped to the combined tabs.
        """
        name_map = {
            "text_json": self.text_json_tab,
            "json_code": self.json_code_tab,
            "traceability": self.traceability_tab,
            # Legacy name mappings
            "json": self.text_json_tab,
            "text": self.text_json_tab,
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
        # Check for unsaved changes (syncs editors + prompts)
        if self._check_unsaved_changes():
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
    
    def _format_issues_for_chat(self, issues: list) -> str:
        """Format validation issues as a readable text message for chat."""
        if not issues:
            return "No issues found."
        
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")
        
        lines = ["**Validation Findings:**\n"]
        
        if error_count > 0:
            lines.append(f"🔴 {error_count} error(s)")
        if warning_count > 0:
            lines.append(f"🟡 {warning_count} warning(s)")
        
        lines.append("\n")
        
        for idx, issue in enumerate(issues, 1):
            emoji = "🔴" if issue.severity == "error" else "🟡"
            lines.append(f"{emoji} **Issue {idx}**: {issue.message}")
            if issue.location:
                lines.append(f"   📍 Location: {issue.location}")
            if issue.code:
                lines.append(f"   🏷️ Code: {issue.code}")
            if issue.suggested_fix:
                lines.append(f"   💡 Suggested fix: {issue.suggested_fix}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _format_proposals_summary(self, parsed) -> str:
        """Format proposals as a summary for chat display."""
        lines = ["**Proposals:**"]
        
        if parsed.procedure_json and parsed.procedure_json.is_valid:
            lines.append("📄 procedure.json - Ready to review")
        if parsed.test_code and parsed.test_code.is_valid:
            lines.append("📄 test.py - Ready to review")
        if parsed.procedure_text and parsed.procedure_text.is_valid:
            lines.append("📄 procedure_text.md - Ready to review")
        
        return "\n".join(lines)
    
    def _format_open_questions_for_chat(self, questions: list) -> str:
        """Format open questions for chat display."""
        if not questions:
            return ""
        
        lines = ["**Open Questions:**"]
        for q in questions:
            lines.append(f"❓ {q}")
        
        return "\n".join(lines)
