"""
Settings Dialog - Application configuration.

Implements Section 12.2 of the spec with unified task management.
"""

import json
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

from ..core.task_config import TaskConfig, TaskConfigManager, ChatConfig
from ..llm.backend_base import LLMTask


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


class PromptEditorDialog(QDialog):
    """
    Modal dialog for editing a task's prompt template.
    
    Displays a large text editor with monospace font and shows
    available template variables.
    """
    
    def __init__(self, task_config: TaskConfig, parent=None):
        super().__init__(parent)
        self.task_config = task_config
        self._original_prompt = task_config.prompt_template
        
        self.setWindowTitle(f"Edit Prompt - {task_config.name}")
        self.setMinimumSize(700, 500)
        
        self._setup_ui()
        self._load_prompt()
    
    def _setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Instructions
        info_label = QLabel(
            "Edit the prompt template for this task. Use template variables like "
            "{procedure_text}, {json_procedure}, {test_code} as needed."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; padding: 5px;")
        layout.addWidget(info_label)
        
        # Prompt editor
        editor_label = QLabel("Prompt Template:")
        editor_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(editor_label)
        
        self.prompt_editor = QPlainTextEdit()
        self.prompt_editor.setPlaceholderText("Enter custom prompt template or leave empty to use default...")
        
        # Use monospace font for better readability
        mono_font = QFont("Consolas", 10)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier New", 10)
        self.prompt_editor.setFont(mono_font)
        
        layout.addWidget(self.prompt_editor, stretch=1)
        
        # Variable reference section
        help_group = QGroupBox("Available Template Variables")
        help_layout = QVBoxLayout(help_group)
        
        help_text = QLabel(
            "• {procedure_text} - Textual test procedure\n"
            "• {json_procedure} - JSON structure of procedure\n"
            "• {test_code} - Python test code implementation\n"
            "• {user_message} - User's custom message/question\n"
            "• {rules} - Selected validation rules"
        )
        help_text.setFont(QFont("Segoe UI", 9))
        help_text.setStyleSheet("color: #555;")
        help_layout.addWidget(help_text)
        
        layout.addWidget(help_group)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        self.reset_btn = QPushButton("Reset to Default")
        self.reset_btn.clicked.connect(self._on_reset)
        self.reset_btn.setToolTip("Clear custom prompt and use the default template")
        btn_layout.addWidget(self.reset_btn)
        
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save)
        self.save_btn.setDefault(True)
        btn_layout.addWidget(self.save_btn)
        
        layout.addLayout(btn_layout)
    
    def _load_prompt(self):
        """Load the current prompt template."""
        if self.task_config.prompt_template:
            self.prompt_editor.setPlainText(self.task_config.prompt_template)
        else:
            # Show default prompt from PromptBuilder
            from ..llm.prompt_builder import PromptBuilder
            default_prompts = PromptBuilder.get_default_prompts()
            default_prompt = default_prompts.get(self.task_config.id, "")
            self.prompt_editor.setPlainText(default_prompt)
    
    def _on_reset(self):
        """Reset to default prompt."""
        reply = QMessageBox.question(
            self,
            "Reset Prompt",
            "Reset this prompt to the default template?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            from ..llm.prompt_builder import PromptBuilder
            default_prompts = PromptBuilder.get_default_prompts()
            default_prompt = default_prompts.get(self.task_config.id, "")
            self.prompt_editor.setPlainText(default_prompt)
    
    def _on_save(self):
        """Save the prompt template."""
        prompt_text = self.prompt_editor.toPlainText().strip()
        
        # Get default prompt for comparison
        from ..llm.prompt_builder import PromptBuilder
        default_prompts = PromptBuilder.get_default_prompts()
        default_prompt = default_prompts.get(self.task_config.id, "")
        
        # If prompt matches default, store as None (use default)
        if prompt_text == default_prompt.strip():
            self.task_config.prompt_template = None
        else:
            self.task_config.prompt_template = prompt_text if prompt_text else None
        
        self.accept()
    
    def get_modified_task_config(self) -> TaskConfig:
        """Get the modified task configuration."""
        return self.task_config


class SettingsDialog(QDialog):
    """
    Settings dialog for application configuration.
    
    Settings are stored in settings.json in user's home directory.
    Task configurations are managed through TaskConfigManager.
    """
    
    def __init__(self, task_config_manager: TaskConfigManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(700)
        self.setMinimumHeight(600)
        
        self._settings = load_settings()
        self._task_config_manager = task_config_manager
        
        # Cache for task modifications (indexed by tab_id)
        self._task_cache = {}
        
        # Track which tab's data is currently displayed in the table
        self._current_displayed_tab_id = None
        
        self._setup_ui()
        self._load_values()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Tab widget
        tabs = QTabWidget()
        
        # LLM Backend tab (index 0)
        llm_tab = self._create_llm_tab()
        tabs.addTab(llm_tab, "LLM Backend")
        
        # Tasks tab (index 1) - NEW UNIFIED TAB
        tasks_tab = self._create_tasks_tab()
        tabs.addTab(tasks_tab, "Tasks")
        
        # Chat tab (index 2) - Per-tab chat configuration
        chat_tab = self._create_chat_tab()
        tabs.addTab(chat_tab, "Chat")
        
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
    
    def _create_tasks_tab(self) -> QWidget:
        """
        Create the unified Tasks tab for managing LLM tasks.
        
        Features:
        - Tab selector dropdown (text_json, json_code)
        - Table showing tasks with columns: Enabled | Task Name | Button Label | Actions
        - Edit Prompt button in Actions column
        - Add Task, Delete Task, Reset to Defaults buttons
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        info_label = QLabel(
            "Manage LLM tasks for each workflow tab. Customize button labels and prompt templates."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; padding: 5px;")
        layout.addWidget(info_label)
        
        # Tab selection
        tab_selection_layout = QHBoxLayout()
        tab_selection_layout.addWidget(QLabel("Workflow Tab:"))
        
        self.task_tab_combo = QComboBox()
        self.task_tab_combo.addItems([
            "Text-JSON",
            "JSON-Code"
        ])
        self.task_tab_combo.currentIndexChanged.connect(self._on_task_tab_changed)
        tab_selection_layout.addWidget(self.task_tab_combo)
        tab_selection_layout.addStretch()
        
        layout.addLayout(tab_selection_layout)
        
        # Tasks table
        self.tasks_table = QTableWidget()
        self.tasks_table.setColumnCount(4)
        self.tasks_table.setHorizontalHeaderLabels(["Enabled", "Task Name", "Button Label", "Actions"])
        
        # Configure table appearance
        self.tasks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tasks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tasks_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tasks_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tasks_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tasks_table.setAlternatingRowColors(True)
        self.tasks_table.verticalHeader().setVisible(False)
        
        # Set table font
        table_font = QFont("Segoe UI", 9)
        self.tasks_table.setFont(table_font)
        
        layout.addWidget(self.tasks_table, stretch=1)
        
        # Action buttons
        action_layout = QHBoxLayout()
        
        self.add_task_btn = QPushButton("Add Task")
        self.add_task_btn.clicked.connect(self._on_add_task)
        self.add_task_btn.setToolTip("Add a new task from available LLMTask enum values")
        action_layout.addWidget(self.add_task_btn)
        
        self.delete_task_btn = QPushButton("Delete Task")
        self.delete_task_btn.clicked.connect(self._on_delete_task)
        self.delete_task_btn.setToolTip("Remove the selected task")
        action_layout.addWidget(self.delete_task_btn)
        
        action_layout.addStretch()
        
        self.reset_tab_btn = QPushButton("Reset Tab to Defaults")
        self.reset_tab_btn.clicked.connect(self._on_reset_tab_tasks)
        self.reset_tab_btn.setToolTip("Reset all tasks for this tab to default configuration")
        action_layout.addWidget(self.reset_tab_btn)
        
        layout.addLayout(action_layout)
        
        # Help text
        help_label = QLabel(
            "💡 Tip: Double-click cells to edit task names and button labels. "
            "Click 'Edit Prompt...' to customize the prompt template for each task."
        )
        help_label.setStyleSheet("color: #666; font-style: italic; padding: 5px;")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        
        # Load initial tasks for first tab
        self._load_tasks_for_tab()
        
        # Connect cell changed signal for inline editing (only once)
        self.tasks_table.cellChanged.connect(self._on_task_cell_changed)
        
        return tab
    
    def _get_current_tab_id(self) -> str:
        """Get the current tab ID based on combo box selection."""
        index = self.task_tab_combo.currentIndex()
        tab_ids = ["text_json", "json_code"]
        return tab_ids[index] if 0 <= index < len(tab_ids) else "text_json"
    
    def _load_tasks_for_tab(self):
        """Load tasks from TaskConfigManager for the currently selected tab.
        
        Retrieves task configurations from _task_cache (if modified) or from
        TaskConfigManager (if not yet cached). Populates the tasks table with
        enabled checkboxes, editable task names/labels, and Edit Prompt buttons.
        """
        # Block signals temporarily to prevent spurious events during population
        self.tasks_table.blockSignals(True)
        
        tab_id = self._get_current_tab_id()
        
        # Get tasks from cache if available, otherwise from manager
        if tab_id in self._task_cache:
            tasks = self._task_cache[tab_id]
        else:
            tasks = self._task_config_manager.get_all_tasks_for_tab(tab_id)
            self._task_cache[tab_id] = tasks
        
        # Clear and repopulate table
        self.tasks_table.setRowCount(0)
        self.tasks_table.setRowCount(len(tasks))
        
        for row, task in enumerate(tasks):
            # Enabled checkbox (column 0)
            enabled_checkbox = QCheckBox()
            enabled_checkbox.setChecked(task.enabled)
            enabled_checkbox.stateChanged.connect(
                lambda state, r=row: self._on_task_enabled_changed(r, state)
            )
            
            # Center the checkbox
            checkbox_widget = QWidget()
            checkbox_layout = QHBoxLayout(checkbox_widget)
            checkbox_layout.addWidget(enabled_checkbox)
            checkbox_layout.setAlignment(Qt.AlignCenter)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            
            self.tasks_table.setCellWidget(row, 0, checkbox_widget)
            
            # Task Name (column 1) - editable
            name_item = QTableWidgetItem(task.name)
            name_item.setData(Qt.UserRole, task.id)  # Store task ID
            self.tasks_table.setItem(row, 1, name_item)
            
            # Button Label (column 2) - editable
            label_item = QTableWidgetItem(task.button_label)
            self.tasks_table.setItem(row, 2, label_item)
            
            # Actions (column 3) - Edit Prompt button
            edit_prompt_btn = QPushButton("Edit Prompt...")
            edit_prompt_btn.clicked.connect(
                lambda checked, r=row: self._on_edit_prompt(r)
            )
            self.tasks_table.setCellWidget(row, 3, edit_prompt_btn)
        
        # Unblock signals
        self.tasks_table.blockSignals(False)
        
        # Track which tab is now displayed
        self._current_displayed_tab_id = tab_id
        
        # Adjust row heights
        for row in range(self.tasks_table.rowCount()):
            self.tasks_table.setRowHeight(row, 35)
    
    def _on_task_tab_changed(self, index: int):
        """Handle tab selection change in the workflow tab dropdown."""
        # Save current table state to cache BEFORE switching
        # Use the currently displayed tab_id, not the new selection
        if self._current_displayed_tab_id is not None:
            self._save_tasks_to_cache_for_tab(self._current_displayed_tab_id)
        
        # Load tasks for new tab (signal handling done inside)
        self._load_tasks_for_tab()
    
    def _save_tasks_to_cache_for_tab(self, tab_id: str):
        """Save table state to task cache for a specific tab.
        
        Reads all task data from the tasks table (enabled state, name, label, prompt)
        and stores it in _task_cache indexed by the provided tab_id.
        """
        
        # Block signals to prevent re-entrant cellChanged events
        self.tasks_table.blockSignals(True)
        
        tasks = []
        for row in range(self.tasks_table.rowCount()):
            # Get enabled state
            checkbox_widget = self.tasks_table.cellWidget(row, 0)
            enabled = False
            if checkbox_widget:
                checkbox = checkbox_widget.findChild(QCheckBox)
                if checkbox:
                    enabled = checkbox.isChecked()
            
            # Get task ID from name item
            name_item = self.tasks_table.item(row, 1)
            if not name_item:
                continue
            
            task_id = name_item.data(Qt.UserRole)
            name = name_item.text().strip()
            
            # Get button label
            label_item = self.tasks_table.item(row, 2)
            label = label_item.text().strip() if label_item else ""
            
            # Get existing task config to preserve prompt_template
            existing_task = None
            if tab_id in self._task_cache:
                for t in self._task_cache[tab_id]:
                    if t.id == task_id:
                        existing_task = t
                        break
            
            if not existing_task:
                existing_task = self._task_config_manager.get_task_config(tab_id, task_id)
            
            # Create updated task config
            task = TaskConfig(
                id=task_id,
                name=name,
                button_label=label,
                prompt_template=existing_task.prompt_template if existing_task else None,
                enabled=enabled
            )
            tasks.append(task)
        
        self._task_cache[tab_id] = tasks
        
        # Restore signals
        self.tasks_table.blockSignals(False)
    
    def _save_current_tasks_to_cache(self):
        """Save current table state to task cache.
        
        Convenience method that saves the currently displayed tab's data.
        """
        if self._current_displayed_tab_id is not None:
            self._save_tasks_to_cache_for_tab(self._current_displayed_tab_id)
    
    def _on_task_enabled_changed(self, row: int, state: int):
        """Handle task enabled checkbox change."""
        # Changes are saved to cache when switching tabs or saving
        pass
    
    def _on_task_cell_changed(self, row: int, column: int):
        """Handle inline editing of task name or button label."""
        # Validation
        if column == 1:  # Task Name
            item = self.tasks_table.item(row, column)
            if item and not item.text().strip():
                QMessageBox.warning(self, "Invalid Input", "Task name cannot be empty.")
                # Restore previous value
                tab_id = self._get_current_tab_id()
                if tab_id in self._task_cache and row < len(self._task_cache[tab_id]):
                    item.setText(self._task_cache[tab_id][row].name)
        
        elif column == 2:  # Button Label
            item = self.tasks_table.item(row, column)
            if item and len(item.text()) > 50:
                QMessageBox.warning(self, "Invalid Input", "Button label cannot exceed 50 characters.")
                # Restore previous value
                tab_id = self._get_current_tab_id()
                if tab_id in self._task_cache and row < len(self._task_cache[tab_id]):
                    item.setText(self._task_cache[tab_id][row].button_label)
    
    def _on_edit_prompt(self, row: int):
        """Open prompt editor for the selected task."""
        tab_id = self._get_current_tab_id()
        
        # Save current state first
        self._save_current_tasks_to_cache()
        
        # Get task config
        if tab_id not in self._task_cache or row >= len(self._task_cache[tab_id]):
            QMessageBox.warning(self, "Error", "Task not found.")
            return
        
        task = self._task_cache[tab_id][row]
        
        # Open prompt editor dialog
        dialog = PromptEditorDialog(task, self)
        if dialog.exec() == QDialog.Accepted:
            # Update task in cache
            self._task_cache[tab_id][row] = dialog.get_modified_task_config()
    
    def _on_add_task(self):
        """Add a new blank task row to the current tab.
        
        Creates a new task with default placeholder values that the user
        can then customize by editing the table cells and prompt template.
        """
        tab_id = self._get_current_tab_id()
        
        # Save current state first
        self._save_current_tasks_to_cache()
        
        # Generate unique task ID
        existing_ids = set()
        if tab_id in self._task_cache:
            existing_ids = {task.id for task in self._task_cache[tab_id]}
        
        # Find next available custom task number
        counter = 1
        while f"custom_task_{counter}" in existing_ids:
            counter += 1
        
        new_task_id = f"custom_task_{counter}"
        
        # Create new task with placeholder values
        new_task = TaskConfig(
            id=new_task_id,
            name="New Task",
            button_label="New Task",
            prompt_template="Define your prompt here. Use {context} for artifact content.",
            enabled=True
        )
        
        # Add to cache
        if tab_id not in self._task_cache:
            self._task_cache[tab_id] = []
        self._task_cache[tab_id].append(new_task)
        
        # Reload table
        self._load_tasks_for_tab()
    
    def _on_delete_task(self):
        """Delete the selected task."""
        selected_rows = self.tasks_table.selectionModel().selectedRows()
        
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select a task to delete.")
            return
        
        row = selected_rows[0].row()
        
        # Get task name for confirmation
        name_item = self.tasks_table.item(row, 1)
        task_name = name_item.text() if name_item else "this task"
        
        reply = QMessageBox.question(
            self,
            "Delete Task",
            f"Delete '{task_name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            tab_id = self._get_current_tab_id()
            
            # Save current state
            self._save_current_tasks_to_cache()
            
            # Remove from cache
            if tab_id in self._task_cache and row < len(self._task_cache[tab_id]):
                self._task_cache[tab_id].pop(row)
            
            # Reload table
            self._load_tasks_for_tab()
    
    def _on_reset_tab_tasks(self):
        """Reset the current tab to default task configurations."""
        tab_id = self._get_current_tab_id()
        tab_name = self.task_tab_combo.currentText()
        
        reply = QMessageBox.question(
            self,
            "Reset to Defaults",
            f"Reset all tasks for '{tab_name}' tab to defaults?\n\n"
            "This will restore default task names, button labels, and prompt templates.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Clear cache for this tab (will reload from defaults)
            if tab_id in self._task_cache:
                del self._task_cache[tab_id]
            
            # Reload table
            self._load_tasks_for_tab()
    
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
        
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(1000, 128000)
        self.max_tokens.setValue(16384)
        self.max_tokens.setToolTip("Maximum tokens in response (includes both input and output). Check your model's documentation for limits. Modern models like GPT-5.2 typically support 16K-128K+")
        common_layout.addRow("Max Tokens:", self.max_tokens)
        
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
                import os
                
                api_key = self.api_key.text() or os.getenv("OPENAI_API_KEY", "")
                
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
                    request_timeout=test_timeout,
                )
                backend_obj = ExternalAPIBackend(config=config)
                
                # Set API key if provided (optional for services like Ollama)
                if api_key:
                    backend_obj._api_key = api_key
                
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
    



    
    def _create_chat_tab(self) -> QWidget:
        """Create the Chat configuration tab for per-tab AD_HOC_CHAT settings."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        info_label = QLabel(
            "Configure the chat panel behavior for each workflow tab. "
            "The system prompt is prepended to every chat message sent from this tab."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; padding: 5px;")
        layout.addWidget(info_label)
        
        # Tab selection
        tab_selection_layout = QHBoxLayout()
        tab_selection_layout.addWidget(QLabel("Workflow Tab:"))
        
        self.chat_tab_combo = QComboBox()
        self.chat_tab_combo.addItems(["Text-JSON", "JSON-Code"])
        self.chat_tab_combo.currentIndexChanged.connect(self._on_chat_tab_changed)
        tab_selection_layout.addWidget(self.chat_tab_combo)
        tab_selection_layout.addStretch()
        
        layout.addLayout(tab_selection_layout)
        
        # Enable checkbox
        self.chat_enabled_checkbox = QCheckBox("Enable Chat Panel for this tab")
        self.chat_enabled_checkbox.setChecked(True)
        layout.addWidget(self.chat_enabled_checkbox)
        
        # System prompt editor
        prompt_group = QGroupBox("System Prompt")
        prompt_layout = QVBoxLayout(prompt_group)
        
        prompt_info = QLabel(
            "Custom system prompt prepended to chat messages. "
            "Leave empty to use the default prompt."
        )
        prompt_info.setWordWrap(True)
        prompt_info.setStyleSheet("color: #666;")
        prompt_layout.addWidget(prompt_info)
        
        self.chat_system_prompt = QPlainTextEdit()
        self.chat_system_prompt.setPlaceholderText(
            "Enter custom system prompt or leave empty for default...\n\n"
            "Example: You are an expert test engineer. Help the user write "
            "correct and complete test procedures."
        )
        mono_font = QFont("Consolas", 10)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier New", 10)
        self.chat_system_prompt.setFont(mono_font)
        self.chat_system_prompt.setMinimumHeight(200)
        prompt_layout.addWidget(self.chat_system_prompt)
        
        layout.addWidget(prompt_group, stretch=1)
        
        # Reset button
        reset_layout = QHBoxLayout()
        reset_layout.addStretch()
        self.chat_reset_btn = QPushButton("Reset to Default")
        self.chat_reset_btn.clicked.connect(self._on_reset_chat)
        self.chat_reset_btn.setToolTip("Clear custom system prompt and use default")
        reset_layout.addWidget(self.chat_reset_btn)
        layout.addLayout(reset_layout)
        
        # Cache for chat config modifications
        self._chat_cache = {}
        self._current_chat_tab_id = None
        
        # Load initial chat config
        self._load_chat_config()
        
        return tab
    
    def _get_current_chat_tab_id(self) -> str:
        """Get the current chat tab ID based on combo box selection."""
        index = self.chat_tab_combo.currentIndex()
        tab_ids = ["text_json", "json_code"]
        return tab_ids[index] if 0 <= index < len(tab_ids) else "text_json"
    
    def _load_chat_config(self):
        """Load chat config for the currently selected tab."""
        # Save current state first
        if self._current_chat_tab_id is not None:
            self._save_chat_to_cache()
        
        tab_id = self._get_current_chat_tab_id()
        
        # Get from cache or manager
        if tab_id in self._chat_cache:
            chat_config = self._chat_cache[tab_id]
        else:
            chat_config = self._task_config_manager.get_chat_config(tab_id)
            self._chat_cache[tab_id] = chat_config
        
        # Populate UI
        self.chat_enabled_checkbox.setChecked(chat_config.enabled)
        self.chat_system_prompt.setPlainText(chat_config.system_prompt or "")
        
        self._current_chat_tab_id = tab_id
    
    def _save_chat_to_cache(self):
        """Save current chat UI state to cache."""
        if self._current_chat_tab_id is None:
            return
        
        self._chat_cache[self._current_chat_tab_id] = ChatConfig(
            enabled=self.chat_enabled_checkbox.isChecked(),
            system_prompt=self.chat_system_prompt.toPlainText().strip() or None
        )
    
    def _on_chat_tab_changed(self, index: int):
        """Handle chat tab selection change."""
        self._load_chat_config()
    
    def _on_reset_chat(self):
        """Reset chat config to defaults."""
        from ..core.task_config import DEFAULT_CHAT_CONFIG
        tab_id = self._get_current_chat_tab_id()
        
        reply = QMessageBox.question(
            self, "Reset Chat Config",
            f"Reset chat configuration for this tab to defaults?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            if tab_id in self._chat_cache:
                del self._chat_cache[tab_id]
            self._load_chat_config()



    
    def _load_values(self):
        """Load values from settings.
        
        Loads LLM backend configuration from settings.json.
        Task configurations are already loaded in _create_tasks_tab().
        """
        # LLM Backend
        self.backend_combo.setCurrentText(
            self._settings.get("llm_backend", "opencode")
        )
        
        # Common LLM parameters
        common_llm = self._settings.get("common_llm", {})
        self.temperature.setValue(common_llm.get("temperature", 0.2))
        self.max_tokens.setValue(common_llm.get("max_tokens", 16384))
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
    
    def _on_save(self):
        """Save settings.
        
        Saves LLM backend configuration to settings.json and task configurations
        to TaskConfigManager. Task configurations include button labels, prompts,
        and enabled state for each task.
        """
        # Save current table state to cache
        self._save_current_tasks_to_cache()
        
        # Persist all task configurations through TaskConfigManager
        for tab_id, tasks in self._task_cache.items():
            # Replace entire tab configuration via public API
            self._task_config_manager.set_all_tasks_for_tab(tab_id, tasks)
        
        # Save chat configurations
        self._save_chat_to_cache()  # Save current chat tab state
        for tab_id, chat_config in self._chat_cache.items():
            self._task_config_manager.set_chat_config(tab_id, chat_config)
        
        # Save to disk
        self._task_config_manager.save_config()
        
        # Save LLM backend settings to settings.json
        self._settings = {
            "llm_backend": self.backend_combo.currentText(),
            "common_llm": {
                "temperature": self.temperature.value(),
                "max_tokens": self.max_tokens.value(),
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
