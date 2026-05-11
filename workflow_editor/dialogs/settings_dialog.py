"""
Settings Dialog - Application configuration.

Implements Section 12.2 of the spec with unified task management.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QWidget, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox,
    QPushButton, QLabel, QGroupBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QSplitter
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from .. import theme
from ..core.task_config import TaskConfig, TaskConfigManager, ChatConfig
from ..llm.backend_base import LLMTask
from ..theme import muted_text

log = logging.getLogger(__name__)


def get_settings_path() -> Path:
    """Get the settings file path."""
    # Use user's home directory
    home = Path.home()
    settings_dir = home / ".workflow_editor"
    settings_dir.mkdir(exist_ok=True)
    return settings_dir / "settings.json"


def load_settings() -> dict:
    """Load settings from file."""
    path = get_settings_path()
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(settings: dict):
    """Save settings to file."""
    path = get_settings_path()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)



class SettingsDialog(QDialog):
    """
    Settings dialog for application configuration.
    
    Settings are stored in settings.json in user's home directory.
    Task configurations are managed through TaskConfigManager.
    """
    
    def __init__(
        self,
        task_config_manager: TaskConfigManager,
        parent=None,
        project_root=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(700)
        self.setMinimumHeight(600)

        self._settings = load_settings()
        self._task_config_manager = task_config_manager
        # Project root is needed by the Validator tab to read/write
        # ``<project>/config/config.json``'s ``validator_loop`` section.
        # ``None`` means no project is open — the Validator tab's controls
        # then disable themselves with an explanatory tooltip.
        self._project_root = project_root

        # Note: task / chat editing moved to the parent app's
        # ProjectConfigDialog -> Workflows tab (Phase 4). This dialog
        # now owns only LLM backend + validator-loop settings.
        self._setup_ui()
        self._load_values()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Tab widget
        tabs = QTabWidget()
        
        # LLM Backend tab (index 0)
        llm_tab = self._create_llm_tab()
        tabs.addTab(llm_tab, "LLM Backend")

        # Tasks + Chat editing moved to ProjectConfigDialog ->
        # Workflows tab (Phase 4). See the workflow editor's
        # "Configure Workflows…" menu item for the entry point.

        # Validator tab (index 1) - Per-project validator-loop options.
        # Currently hosts only the global retry cap; reserved for future
        # validator-related settings (per-artifact toggles, etc.) without
        # bloating the LLM Backend tab.
        validator_tab = self._create_validator_tab()
        tabs.addTab(validator_tab, "Validator")

        layout.addWidget(tabs)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save)
        self.save_btn.setDefault(True)
        btn_layout.addWidget(self.save_btn)
        
        layout.addLayout(btn_layout)
    
    def _create_llm_tab(self) -> QWidget:
        """Create the LLM settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Backend selection
        backend_group = QGroupBox("LLM Backend")
        backend_layout = QFormLayout(backend_group)
        
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["opencode", "external_api", "none"])
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        backend_layout.addRow("Backend:", self.backend_combo)
        
        layout.addWidget(backend_group)
        
        # Common LLM Parameters
        common_group = QGroupBox("Common Parameters")
        common_layout = QFormLayout(common_group)
        
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(0.2)
        self.temperature.setToolTip("Controls randomness: 0 = deterministic, 2 = very random")
        common_layout.addRow("Temperature:", self.temperature)
        
        self.context_window = QSpinBox()
        self.context_window.setRange(1000, 2_000_000)
        self.context_window.setValue(16384)
        self.context_window.setToolTip(
            "Model's total context window (input + output token budget).\n"
            "Used only by the chat panel's 'Tokens: X/Y' indicator so you "
            "know when to reset the session.\n"
            "Not sent to the LLM. Examples: GPT-4 → 128000, Gemma 4 26B → 131072, Claude 4.7 → 1000000."
        )
        common_layout.addRow("Context Window:", self.context_window)
        
        self.request_timeout = QDoubleSpinBox()
        self.request_timeout.setRange(10.0, 10800.0)
        self.request_timeout.setSingleStep(10.0)
        self.request_timeout.setValue(120.0)
        self.request_timeout.setSuffix(" sec")
        self.request_timeout.setKeyboardTracking(True)
        self.request_timeout.setToolTip("Request timeout in seconds (10-10800). For long generations, use 300-600 seconds.")
        common_layout.addRow("Request Timeout:", self.request_timeout)
        
        layout.addWidget(common_group)
        
        # OpenCode settings
        self.opencode_group = QGroupBox("OpenCode Settings")
        opencode_layout = QFormLayout(self.opencode_group)
        
        self.opencode_port = QSpinBox()
        self.opencode_port.setRange(1024, 65535)
        self.opencode_port.setValue(4096)
        opencode_layout.addRow("Port:", self.opencode_port)
        
        self.opencode_host = QLineEdit()
        self.opencode_host.setPlaceholderText("127.0.0.1")
        opencode_layout.addRow("Host:", self.opencode_host)
        
        self.opencode_model = QLineEdit()
        self.opencode_model.setPlaceholderText("default")
        opencode_layout.addRow("Model:", self.opencode_model)
        
        self.opencode_wsl_path = QLineEdit()
        self.opencode_wsl_path.setPlaceholderText("/mnt/c/...")
        self.opencode_wsl_path.setToolTip("WSL path to OpenCode directory")
        opencode_layout.addRow("WSL Path:", self.opencode_wsl_path)
        
        self.opencode_startup_timeout = QDoubleSpinBox()
        self.opencode_startup_timeout.setRange(10.0, 300.0)
        self.opencode_startup_timeout.setValue(60.0)
        self.opencode_startup_timeout.setToolTip("Timeout for backend startup")
        opencode_layout.addRow("Startup Timeout:", self.opencode_startup_timeout)
        
        layout.addWidget(self.opencode_group)
        
        # External API settings
        self.api_group = QGroupBox("External API Settings")
        api_layout = QFormLayout(self.api_group)
        
        self.api_url = QLineEdit()
        self.api_url.setPlaceholderText("https://api.openai.com/v1")
        self.api_url.setToolTip("Base URL with /v1 suffix. Examples:\n• OpenAI: https://api.openai.com/v1\n• Ollama: http://127.0.0.1:11434/v1")
        api_layout.addRow("API URL:", self.api_url)
        
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-...")
        api_layout.addRow("API Key:", self.api_key)
        
        self.api_model = QLineEdit()
        self.api_model.setPlaceholderText("e.g., gpt-4, qwen3:8b-16k")
        api_layout.addRow("Model:", self.api_model)
        
        self.api_retry_count = QSpinBox()
        self.api_retry_count.setRange(0, 10)
        self.api_retry_count.setValue(2)
        self.api_retry_count.setToolTip("Number of retry attempts for failed requests")
        api_layout.addRow("Retry Count:", self.api_retry_count)
        
        layout.addWidget(self.api_group)
        
        # Test Connection button
        test_btn_layout = QHBoxLayout()
        test_btn_layout.addStretch()
        self.test_connection_btn = QPushButton("Test Connection")
        self.test_connection_btn.clicked.connect(self._on_test_connection)
        test_btn_layout.addWidget(self.test_connection_btn)
        layout.addLayout(test_btn_layout)
        
        layout.addStretch()
        
        return tab
    
    def _on_backend_changed(self, backend: str):
        """Handle backend selection change."""
        self.opencode_group.setVisible(backend == "opencode")
        self.api_group.setVisible(backend == "external_api")
    
    def _on_test_connection(self):
        """Test the LLM backend configuration."""
        backend = self.backend_combo.currentText()
        
        if backend == "none":
            QMessageBox.information(self, "Test Connection", "No backend selected.")
            return
        
        try:
            if backend == "opencode":
                # Test OpenCode backend
                from ..llm.opencode_backend import OpenCodeBackend, OpenCodeConfig
                from ..llm.backend_base import LLMRequest, LLMTask
                
                config = OpenCodeConfig(
                    server_port=self.opencode_port.value(),
                    server_hostname=self.opencode_host.text() or "127.0.0.1",
                    model=self.opencode_model.text() or None,
                )
                backend_obj = OpenCodeBackend(config=config)
                
                # Check availability first
                if not backend_obj.is_available():
                    QMessageBox.warning(
                        self, "Test Connection",
                        f"✗ OpenCode backend is not available\n\nHost: {config.server_hostname}\nPort: {config.server_port}\n\nMake sure OpenCode is running."
                    )
                    return
                
                # If model is specified, test it with a minimal request
                if config.model:
                    # Try to start backend if not running
                    if not backend_obj.is_running:
                        if not backend_obj.start():
                            QMessageBox.warning(
                                self, "Test Connection",
                                f"✗ Could not start OpenCode backend\n\nHost: {config.server_hostname}\nPort: {config.server_port}"
                            )
                            return
                    
                    try:
                        # Make a minimal test request
                        test_request = LLMRequest(
                            task=LLMTask.AD_HOC_CHAT,
                            strict_mode=False,
                            user_message="Respond with just 'OK'",
                        )
                        
                        # Send request with timeout handling
                        import threading
                        result = {"response": None, "error": None}
                        
                        def test_send():
                            try:
                                result["response"] = backend_obj.send_request(test_request)
                            except Exception as e:
                                result["error"] = str(e)
                        
                        thread = threading.Thread(target=test_send)
                        thread.start()
                        thread.join(timeout=15)
                        
                        if thread.is_alive():
                            result["error"] = "Request timed out after 15 seconds"
                        
                        # Clean up
                        if backend_obj.is_running:
                            backend_obj.stop()
                        
                        if result["error"]:
                            QMessageBox.warning(
                                self, "Test Connection",
                                f"✗ Model test failed\n\nHost: {config.server_hostname}\nPort: {config.server_port}\nModel: {config.model}\n\nError: {result['error']}\n\nThe model may not exist or be unavailable."
                            )
                        elif result["response"] and result["response"].error_message:
                            QMessageBox.warning(
                                self, "Test Connection",
                                f"✗ Model test failed\n\nHost: {config.server_hostname}\nPort: {config.server_port}\nModel: {config.model}\n\nError: {result['response'].error_message}\n\nThe model may not exist or be unavailable."
                            )
                        else:
                            QMessageBox.information(
                                self, "Test Connection",
                                f"✓ OpenCode backend and model are working\n\nHost: {config.server_hostname}\nPort: {config.server_port}\nModel: {config.model}"
                            )
                    except Exception as e:
                        # Clean up on error
                        if backend_obj.is_running:
                            backend_obj.stop()
                        raise
                else:
                    # No model specified, just confirm availability
                    QMessageBox.information(
                        self, "Test Connection",
                        f"✓ OpenCode backend is available\n\nHost: {config.server_hostname}\nPort: {config.server_port}\n\nNo model specified - will use OpenCode's default."
                    )
            
            elif backend == "external_api":
                # Test External API backend
                from ..llm.external_api_backend import ExternalAPIBackend, ExternalAPIConfig
                from ..llm.backend_base import LLMRequest, LLMTask

                api_key = self.api_key.text().strip()

                model = self.api_model.text()
                if not model:
                    QMessageBox.warning(
                        self, "Test Connection",
                        "✗ No model specified\n\nEnter a model name (e.g., gpt-4 for OpenAI, qwen3:8b-16k for Ollama)."
                    )
                    return
                
                # Use request timeout from settings
                test_timeout = self.request_timeout.value()
                
                config = ExternalAPIConfig(
                    base_url=self.api_url.text() or "https://api.openai.com/v1",
                    model=model,
                    api_key=api_key or None,
                    request_timeout=test_timeout,
                )
                backend_obj = ExternalAPIBackend(config=config)

                # Start backend
                if not backend_obj.start():
                    QMessageBox.warning(
                        self, "Test Connection",
                        f"✗ Could not start External API backend\n\nURL: {config.base_url}"
                    )
                    return
                
                try:
                    # Make a minimal test request
                    test_request = LLMRequest(
                        task=LLMTask.AD_HOC_CHAT,
                        strict_mode=False,
                        user_message="Respond with just 'OK'",
                    )
                    
                    # Send request with timeout handling
                    import threading
                    result = {"response": None, "error": None}
                    
                    def test_send():
                        try:
                            result["response"] = backend_obj.send_request(test_request)
                        except Exception as e:
                            result["error"] = str(e)
                    
                    thread = threading.Thread(target=test_send)
                    thread.start()
                    thread.join(timeout=test_timeout)
                    
                    if thread.is_alive():
                        result["error"] = f"Request timed out after {test_timeout} seconds"
                    
                    # Clean up
                    if backend_obj.is_running:
                        backend_obj.stop()
                    
                    if result["error"]:
                        QMessageBox.warning(
                            self, "Test Connection",
                            f"✗ API test failed\n\nURL: {config.base_url}\nModel: {config.model}\n\nError: {result['error']}\n\nCheck that the server is running and the URL/model are correct."
                        )
                    elif result["response"] and result["response"].error_message:
                        QMessageBox.warning(
                            self, "Test Connection",
                            f"✗ API test failed\n\nURL: {config.base_url}\nModel: {config.model}\n\nError: {result['response'].error_message}\n\nCheck that the server is running and the URL/model are correct."
                        )
                    else:
                        auth_msg = "With API key" if api_key else "Without authentication (e.g., Ollama)"
                        QMessageBox.information(
                            self, "Test Connection",
                            f"✓ External API backend is working\n\nURL: {config.base_url}\nModel: {config.model}\n{auth_msg}"
                        )
                except Exception as e:
                    # Clean up on error
                    if backend_obj.is_running:
                        backend_obj.stop()
                    raise
        
        except Exception as e:
            QMessageBox.critical(
                self, "Test Connection Failed",
                f"Error testing connection:\n\n{str(e)}"
            )
    



    
    # ------------------------------------------------------------------ #
    # Validator tab (per-project: validator_loop.* settings)              #
    # ------------------------------------------------------------------ #

    def _create_validator_tab(self) -> QWidget:
        """Build the Validator tab.

        Single field today: ``Max validator-correction retries`` —
        persisted per-project under ``validator_loop.max_attempts``.
        Reserved for future fields (per-artifact toggles, etc.).
        Falls back to a disabled state with explanatory tooltip when no
        project is open.
        """
        from ..core.task_config import DEFAULT_MAX_VALIDATOR_ATTEMPTS

        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Top-level enable/disable: operator opt-out (Phase 4.6).
        # When unchecked, the deterministic validator is skipped, the
        # auto-correct checkbox is greyed (no loop to run), and the
        # "validator unavailable" chat warning is suppressed.
        self.validator_enabled = QCheckBox("Enable deterministic validator")
        self.validator_enabled.setChecked(True)
        self.validator_enabled.setToolTip(
            "Master switch for the per-project deterministic validator + "
            "its auto-correction loop. Uncheck to skip validation entirely "
            "for this project. When enabled, the validator additionally "
            "requires a text_renderer to be picked in "
            "Project Config → Parsers."
        )
        layout.addWidget(self.validator_enabled)

        retry_group = QGroupBox("Auto-correction loop")
        retry_form = QFormLayout(retry_group)

        self.validator_max_attempts = QSpinBox()
        self.validator_max_attempts.setRange(1, 10)
        self.validator_max_attempts.setValue(DEFAULT_MAX_VALIDATOR_ATTEMPTS)
        self.validator_max_attempts.setToolTip(
            "Total LLM attempts (1 original + N-1 retries) before the "
            "auto-correction loop gives up and falls through to operator "
            "review. Higher values cost more tokens; default 3 resolves "
            "~95%% of fixable failures empirically. Per-project."
        )
        retry_form.addRow("Max validator-correction attempts:", self.validator_max_attempts)

        # Disable the retry-cap row when the master toggle is off.
        self.validator_enabled.toggled.connect(retry_group.setEnabled)

        layout.addWidget(retry_group)

        # Helper note + project-scope explanation.
        note = QLabel(
            "Settings on this tab are stored per-project in "
            "<code>config/config.json</code> under the "
            "<code>validator_loop</code> section. They take effect on "
            "the next LLM task you run."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.muted_color()}; font-size: 11px;")
        layout.addWidget(note)

        # Disable when no project is bound — there's nowhere to persist.
        if self._project_root is None:
            self.validator_enabled.setEnabled(False)
            self.validator_max_attempts.setEnabled(False)
            disabled_note = QLabel(
                "<i>No project open — open a project to edit these settings.</i>"
            )
            disabled_note.setStyleSheet(f"color: {theme.muted_color()};")
            layout.addWidget(disabled_note)

        layout.addStretch()
        return tab

    def _load_validator_values(self) -> None:
        """Populate the Validator tab from the project's
        ``validator_loop`` config section."""
        if self._project_root is None:
            return  # spinbox disabled; default value already set
        try:
            from ..llm.validator_loop_settings import load_settings as _load_section
            section = _load_section(self._project_root)
        except Exception:
            log.exception("Failed to load validator_loop settings")
            return
        if "enabled" in section:
            self.validator_enabled.setChecked(bool(section["enabled"]))
        if "max_attempts" in section:
            try:
                self.validator_max_attempts.setValue(int(section["max_attempts"]))
            except (TypeError, ValueError):
                log.warning(
                    "validator_loop.max_attempts is not an int (%r); "
                    "leaving spinbox at default.", section["max_attempts"],
                )

    def _save_validator_values(self) -> None:
        """Persist the Validator tab's values into the project's
        ``validator_loop`` config section. No-op without a project."""
        if self._project_root is None:
            return
        try:
            from ..llm.validator_loop_settings import save_setting
            save_setting(
                self._project_root, "enabled",
                bool(self.validator_enabled.isChecked()),
            )
            save_setting(
                self._project_root,
                "max_attempts",
                int(self.validator_max_attempts.value()),
            )
        except Exception:
            log.exception("Failed to persist validator_loop settings")

    def _load_values(self):
        """Load values from settings.

        Loads LLM backend configuration from ``settings.json`` and the
        per-project ``validator_loop`` section. Task / chat editing
        lives in the parent app's Project Configuration dialog as of
        Phase 4 — see ``_WorkflowsTab``.
        """
        # LLM Backend
        self.backend_combo.setCurrentText(
            self._settings.get("llm_backend", "opencode")
        )
        
        # Common LLM parameters
        common_llm = self._settings.get("common_llm", {})
        self.temperature.setValue(common_llm.get("temperature", 0.2))
        # Backwards-compat: old setting name was "max_tokens"; prefer new "context_window"
        self.context_window.setValue(
            common_llm.get("context_window", common_llm.get("max_tokens", 16384))
        )
        self.request_timeout.setValue(common_llm.get("request_timeout", 120.0))
        
        # OpenCode settings
        opencode = self._settings.get("opencode", {})
        self.opencode_port.setValue(opencode.get("port", 4096))
        self.opencode_host.setText(opencode.get("host", "127.0.0.1"))
        self.opencode_model.setText(opencode.get("model", ""))
        self.opencode_wsl_path.setText(opencode.get("wsl_path", ""))
        self.opencode_startup_timeout.setValue(opencode.get("startup_timeout", 60.0))
        
        # External API settings
        api = self._settings.get("external_api", {})
        self.api_url.setText(api.get("url", ""))
        self.api_key.setText(api.get("key", ""))
        self.api_model.setText(api.get("model", ""))
        self.api_retry_count.setValue(api.get("retry_count", 2))
        
        # Update visibility
        self._on_backend_changed(self.backend_combo.currentText())

        # Validator tab — populate from project's validator_loop section.
        self._load_validator_values()
    
    def _on_save(self):
        """Save settings.

        Persists LLM backend configuration to ``settings.json`` and the
        per-project ``validator_loop`` section. Task / chat editing
        moved to ``_WorkflowsTab`` in the parent app's Project
        Configuration dialog (Phase 4).
        """
        # Validator tab — persist into project's validator_loop section.
        self._save_validator_values()

        # Save LLM backend settings to settings.json
        self._settings = {
            "llm_backend": self.backend_combo.currentText(),
            "common_llm": {
                "temperature": self.temperature.value(),
                "context_window": self.context_window.value(),
                "request_timeout": self.request_timeout.value(),
            },
            "opencode": {
                "port": self.opencode_port.value(),
                "host": self.opencode_host.text() or "127.0.0.1",
                "model": self.opencode_model.text(),
                "wsl_path": self.opencode_wsl_path.text(),
                "startup_timeout": self.opencode_startup_timeout.value(),
            },
            "external_api": {
                "url": self.api_url.text(),
                "key": self.api_key.text(),
                "model": self.api_model.text(),
                "retry_count": self.api_retry_count.value(),
            },
        }
        
        try:
            save_settings(self._settings)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", str(e))

    
    def get_settings(self) -> dict:
        """Get the current settings."""
        return self._settings
