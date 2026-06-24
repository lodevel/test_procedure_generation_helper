"""
Settings Dialog - Application configuration.

Implements Section 12.2 of the spec with unified task management.
"""

import json
import logging
import shutil
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
from ..llm.server_manager import fetch_opencode_models
from ..theme import muted_text
from ..core.odb_inspect import load_hide_prefixes, save_hide_prefixes

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


def get_opencode_config_dir() -> Path:
    """The editor-OWNED OpenCode config directory (sibling of settings.json).

    OpenCode is launched with this as its working dir so it loads THIS
    ``opencode.json`` — never the open project's (which is a relic). One config,
    tied to the editor, not per project.
    """
    d = get_settings_path().parent / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_opencode_config(seed_from: Optional[Path] = None) -> Path:
    """Return the editor's OpenCode config dir, seeding its ``opencode.json``
    ONCE from ``seed_from`` (e.g. a project's opencode.json) if it doesn't exist
    yet. After the one-time seed the editor owns the copy; the source is never
    read again.
    """
    d = get_opencode_config_dir()
    cfg = d / "opencode.json"
    if not cfg.exists() and seed_from is not None:
        try:
            src = Path(seed_from)
            if src.is_file():
                shutil.copyfile(src, cfg)
                log.info("Seeded editor OpenCode config from %s", src)
        except Exception:
            log.exception("Failed to seed editor OpenCode config")
    return d



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
        self.backend_combo.addItems(["opencode", "none"])
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        backend_layout.addRow("Backend:", self.backend_combo)
        
        layout.addWidget(backend_group)
        
        # Common LLM Parameters
        common_group = QGroupBox("Common Parameters")
        common_layout = QFormLayout(common_group)

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
        
        self.opencode_model = QComboBox()
        self.opencode_model.setToolTip(
            "Which model OpenCode uses. 'Default — use opencode.json' sends no "
            "override, so the server uses its own configured model. The other "
            "entries are the models the running server reports (provider/model); "
            "picking one overrides it per request.")
        self.opencode_model_refresh = QPushButton("Refresh")
        self.opencode_model_refresh.setToolTip(
            "Re-query the running OpenCode server for the models configured in "
            "its opencode.json.")
        self.opencode_model_refresh.clicked.connect(
            lambda: self._populate_opencode_models())
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(self.opencode_model, 1)
        model_row.addWidget(self.opencode_model_refresh)
        opencode_layout.addRow("Model:", model_row)
        self.opencode_models_note = QLabel("")
        self.opencode_models_note.setStyleSheet(
            f"color: {theme.muted_color()}; font-size: 11px;")
        opencode_layout.addRow("", self.opencode_models_note)
        
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
        # Lazily fill the model dropdown the first time OpenCode is shown
        # (a /config round-trip; Refresh re-queries on demand).
        if backend == "opencode" and self.opencode_model.count() == 0:
            self._populate_opencode_models()

    def _opencode_model_value(self) -> str:
        """The selected 'provider/model' override, or '' for Default."""
        return self.opencode_model.currentData() or ""

    def _populate_opencode_models(self):
        """Fill the OpenCode model dropdown from the running server's /config.

        Preserves the current/saved selection even when the server is down
        (kept as a '(saved)' entry) so saving never silently drops it.
        """
        if self.opencode_model.count():
            preserve = self.opencode_model.currentData() or ""
        else:
            preserve = self._settings.get("opencode", {}).get("model", "") or ""
        url = (f"http://{self.opencode_host.text() or '127.0.0.1'}"
               f":{self.opencode_port.value()}")
        models = fetch_opencode_models(url)
        self.opencode_model.blockSignals(True)
        self.opencode_model.clear()
        self.opencode_model.addItem("Default — use opencode.json", "")
        for m in models:
            self.opencode_model.addItem(m, m)
        if preserve and preserve not in models:
            self.opencode_model.addItem(f"{preserve}  (saved)", preserve)
        idx = self.opencode_model.findData(preserve)
        self.opencode_model.setCurrentIndex(idx if idx >= 0 else 0)
        self.opencode_model.blockSignals(False)
        self.opencode_models_note.setText(
            f"{len(models)} model(s) from the running server" if models
            else "Server not reachable — showing the saved value. "
                 "Start OpenCode, then Refresh.")

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
                    model=self._opencode_model_value() or None,
                    # Test against the EDITOR-owned OpenCode config (seeded once
                    # from the project), matching how real chat launches it.
                    working_directory=str(ensure_opencode_config(
                        seed_from=(self._project_root / "opencode.json")
                        if self._project_root else None)),
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

        # Board-netlist (Text-tab panel) hidden net-name prefixes — per project.
        netlist_group = QGroupBox("Board netlist")
        netlist_form = QFormLayout(netlist_group)
        self.net_hide_prefixes = QLineEdit()
        self.net_hide_prefixes.setPlaceholderText("Net")
        self.net_hide_prefixes.setToolTip(
            "Comma-separated net-name prefixes hidden by default in the Text "
            "tab's netlist panel (case-insensitive). Default 'Net' hides Altium "
            "autogen names like NetD16_A."
        )
        netlist_form.addRow("Hidden net prefixes:", self.net_hide_prefixes)
        layout.addWidget(netlist_group)

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
            self.net_hide_prefixes.setEnabled(False)
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
            prefixes = [p.strip() for p in self.net_hide_prefixes.text().split(",")
                        if p.strip()]
            save_hide_prefixes(self._project_root, prefixes)
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
        # Backwards-compat: old setting name was "max_tokens"; prefer new "context_window"
        self.context_window.setValue(
            common_llm.get("context_window", common_llm.get("max_tokens", 16384))
        )
        self.request_timeout.setValue(common_llm.get("request_timeout", 120.0))
        
        # OpenCode settings — host/port set BEFORE the final
        # _on_backend_changed (below) so the model dropdown queries the right
        # server; _populate_opencode_models preserves opencode["model"].
        opencode = self._settings.get("opencode", {})
        self.opencode_port.setValue(opencode.get("port", 4096))
        self.opencode_host.setText(opencode.get("host", "127.0.0.1"))
        self.opencode_wsl_path.setText(opencode.get("wsl_path", ""))
        self.opencode_startup_timeout.setValue(opencode.get("startup_timeout", 60.0))
        
        
        # Update visibility
        self._on_backend_changed(self.backend_combo.currentText())

        # Validator tab — populate from project's validator_loop section.
        self._load_validator_values()
        try:
            self.net_hide_prefixes.setText(
                ", ".join(load_hide_prefixes(self._project_root)))
        except Exception:
            log.exception("Failed to load net_explorer.hide_prefixes")

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
                "context_window": self.context_window.value(),
                "request_timeout": self.request_timeout.value(),
            },
            "opencode": {
                "port": self.opencode_port.value(),
                "host": self.opencode_host.text() or "127.0.0.1",
                "model": self._opencode_model_value(),
                "wsl_path": self.opencode_wsl_path.text(),
                "startup_timeout": self.opencode_startup_timeout.value(),
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
