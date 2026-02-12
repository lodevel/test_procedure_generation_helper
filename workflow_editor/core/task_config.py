"""
Task Configuration Manager - Centralized management of LLM tasks.

Provides a unified system for managing LLM tasks with their button labels and prompts:
- TaskConfig dataclass for each task
- Per-tab task configuration storage
- Thread-safe operations
- Atomic file writes with temp + rename pattern
- Backward compatibility migration from old settings
- Auto-recovery from corrupted config files
- Default task definitions merged from button_labels and prompt_builder
"""

import json
import logging
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional
from threading import Lock

from ..llm.backend_base import LLMTask

log = logging.getLogger(__name__)


@dataclass
class TaskConfig:
    """
    Configuration for a single LLM task.
    
    Attributes:
        id: Unique task identifier (typically LLMTask enum value)
        name: Human-readable task name
        button_label: Label displayed on button in UI
        prompt_template: Optional custom prompt template (None = use default)
        enabled: Whether task is active/visible in UI
    """
    id: str
    name: str
    button_label: str
    prompt_template: Optional[str] = None
    enabled: bool = True
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'TaskConfig':
        """Create TaskConfig from dictionary."""
        return cls(**data)


@dataclass
class ChatConfig:
    """
    Per-tab configuration for the AD_HOC_CHAT feature (chat panel).
    
    Attributes:
        enabled: Whether chat is available for this tab
        system_prompt: Custom system prompt (None = use default)
    """
    enabled: bool = True
    system_prompt: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ChatConfig':
        """Create ChatConfig from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in ('enabled', 'system_prompt')})


# Default task configurations for each tab
# Merges button labels from button_labels.py and prompts from prompt_builder.py
DEFAULT_TASK_CONFIGS = {
    "text_json": [
        TaskConfig(
            id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            name="Derive JSON from Text",
            button_label="Text → JSON",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.RENDER_TEXT_FROM_JSON.value,
            name="Render Text from JSON",
            button_label="JSON → Text",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.REVIEW_TEXT_PROCEDURE.value,
            name="Review Text Procedure",
            button_label="Review Text",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.REVIEW_JSON.value,
            name="Review JSON",
            button_label="Review JSON",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.REVIEW_TEXT_VS_JSON.value,
            name="Review Text vs JSON",
            button_label="Check Text↔JSON",
            prompt_template=None,
            enabled=True
        ),
    ],
    "json_code": [
        TaskConfig(
            id=LLMTask.GENERATE_CODE_FROM_JSON.value,
            name="Generate Code from JSON",
            button_label="JSON → Code",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.DERIVE_JSON_FROM_CODE.value,
            name="Derive JSON from Code",
            button_label="Code → JSON",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.REVIEW_JSON.value,
            name="Review JSON",
            button_label="Review JSON",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.REVIEW_CODE.value,
            name="Review Code",
            button_label="Review Code",
            prompt_template=None,
            enabled=True
        ),
        TaskConfig(
            id=LLMTask.REVIEW_CODE_VS_JSON.value,
            name="Review Code vs JSON",
            button_label="Check JSON↔Code",
            prompt_template=None,
            enabled=True
        ),
    ],
}

# Default chat configuration per tab
DEFAULT_CHAT_CONFIG = {
    "text_json": ChatConfig(enabled=True, system_prompt=None),
    "json_code": ChatConfig(enabled=True, system_prompt=None),
}


class TaskConfigManager:
    """
    Manages task configurations across all tabs with thread-safe operations.
    
    Features:
    - Per-tab task storage in config/tab_contexts.json
    - Thread-safe operations using threading.Lock
    - Atomic file writes (temp + rename pattern)
    - Backward compatibility migration from old settings
    - Auto-recovery from corrupted config files
    - Reset to defaults with backup of current config
    
    Config file structure:
    {
      "text_json": {
        "selected_rules": [...],
        "tasks": [
          {
            "id": "DERIVE_JSON_FROM_TEXT",
            "name": "Derive JSON from Text",
            "button_label": "Text → JSON",
            "prompt_template": null,
            "enabled": true
          },
          ...
        ]
      },
      ...
    }
    """
    
    def __init__(self, config_path: Path):
        """
        Initialize the task configuration manager.
        
        Args:
            config_path: Path to config/tab_contexts.json
        """
        self._config_path = config_path
        self._task_configs: Dict[str, List[TaskConfig]] = {}
        self._chat_configs: Dict[str, ChatConfig] = {}
        self._lock = Lock()
        
        # Load existing configurations
        self._load_config()
        
        log.info(f"TaskConfigManager initialized with config: {config_path}")
    
    def _load_config(self) -> None:
        """
        Load task configurations from config file.
        
        Handles missing files gracefully and auto-recovers from corrupted files.
        Performs backward compatibility migration if needed.
        """
        if not self._config_path.exists():
            log.info(f"Config file not found: {self._config_path}, using defaults")
            self._initialize_defaults()
            return
        
        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # Migrate old format if needed (modifies config dict in-place)
            migrated = self._migrate_from_old_format(config)
            
            # Extract task configurations from each tab context
            for tab_id, tab_config in config.items():
                if not isinstance(tab_config, dict):
                    continue
                
                # Check for new 'tasks' format
                if "tasks" in tab_config:
                    tasks_data = tab_config["tasks"]
                    if isinstance(tasks_data, list):
                        # Don't overwrite if migration already populated _task_configs
                        if tab_id not in self._task_configs:
                            self._task_configs[tab_id] = [
                                TaskConfig.from_dict(task_data)
                                for task_data in tasks_data
                            ]
                            log.debug(f"Loaded {len(self._task_configs[tab_id])} tasks for tab '{tab_id}'")
                
                # Check for chat_config
                if "chat_config" in tab_config:
                    chat_data = tab_config["chat_config"]
                    if isinstance(chat_data, dict):
                        if tab_id not in self._chat_configs:
                            self._chat_configs[tab_id] = ChatConfig.from_dict(chat_data)
            
            # Fill in missing tabs with defaults
            for tab_id in DEFAULT_TASK_CONFIGS:
                if tab_id not in self._task_configs:
                    self._task_configs[tab_id] = [
                        TaskConfig(**asdict(task))
                        for task in DEFAULT_TASK_CONFIGS[tab_id]
                    ]
                    log.debug(f"Initialized default tasks for tab '{tab_id}'")
            
            # Fill in missing chat configs with defaults
            for tab_id in DEFAULT_CHAT_CONFIG:
                if tab_id not in self._chat_configs:
                    self._chat_configs[tab_id] = ChatConfig(
                        enabled=DEFAULT_CHAT_CONFIG[tab_id].enabled,
                        system_prompt=DEFAULT_CHAT_CONFIG[tab_id].system_prompt
                    )
            
            # Save if migration occurred
            if migrated:
                log.info("Configuration migrated from old format, saving updated config")
                self._save_config_internal(existing_config=config)
            
            log.info(f"Loaded task configurations for {len(self._task_configs)} tab(s)")
        
        except json.JSONDecodeError as e:
            log.error(f"Corrupted config file: {e}. Auto-recovering with defaults.")
            self._auto_recover_config()
        except Exception as e:
            log.error(f"Error loading config: {e}. Auto-recovering with defaults.", exc_info=True)
            self._auto_recover_config()
    
    def _initialize_defaults(self) -> None:
        """Initialize all tabs with default task configurations."""
        with self._lock:
            for tab_id, default_tasks in DEFAULT_TASK_CONFIGS.items():
                self._task_configs[tab_id] = [
                    TaskConfig(**asdict(task))
                    for task in default_tasks
                ]
            for tab_id, default_chat in DEFAULT_CHAT_CONFIG.items():
                self._chat_configs[tab_id] = ChatConfig(
                    enabled=default_chat.enabled,
                    system_prompt=default_chat.system_prompt
                )
            log.info("Initialized all tabs with default task configurations")
    
    def _auto_recover_config(self) -> None:
        """
        Auto-recover from corrupted config by backing up and recreating with defaults.
        
        Silently backs up corrupted file and initializes with defaults.
        """
        try:
            # Backup corrupted file
            if self._config_path.exists():
                backup_path = self._config_path.with_suffix('.json.corrupted')
                shutil.copy2(self._config_path, backup_path)
                log.warning(f"Backed up corrupted config to {backup_path}")
            
            # Initialize with defaults
            self._initialize_defaults()
            
            # Save clean config
            self._save_config_internal()
            log.info("Auto-recovered config file with defaults")
        
        except Exception as e:
            log.error(f"Failed to auto-recover config: {e}", exc_info=True)
            # Continue with in-memory defaults
            self._initialize_defaults()
    
    def _migrate_from_old_format(self, config: dict) -> bool:
        """
        Migrate from old button_labels format to new tasks format.
        
        Old format (in tab_contexts.json):
        {
          "text_json": {
            "selected_rules": [...],
            "button_labels": {
              "DERIVE_JSON_FROM_TEXT": "Custom Label"
            }
          }
        }
        
        New format:
        {
          "text_json": {
            "selected_rules": [...],
            "tasks": [
              {
                "id": "derive_json_from_text",
                "name": "Derive JSON from Text",
                "button_label": "Custom Label",
                "prompt_template": null,
                "enabled": true
              }
            ]
          }
        }
        
        Args:
            config: Configuration dictionary to migrate
        
        Returns:
            True if migration occurred, False otherwise
        """
        migrated = False
        
        # Create a mapping from uppercase enum names to actual enum values
        # LLMTask enum names are uppercase (DERIVE_JSON_FROM_TEXT)
        # but their values are lowercase (derive_json_from_text)
        task_name_to_value = {task.name: task.value for task in LLMTask}
        
        for tab_id, tab_config in config.items():
            if not isinstance(tab_config, dict):
                continue
            
            # Check if we have old button_labels format
            if "button_labels" in tab_config and "tasks" not in tab_config:
                button_labels = tab_config.get("button_labels", {})
                
                # Start with default tasks for this tab
                if tab_id in DEFAULT_TASK_CONFIGS:
                    new_tasks = []
                    for default_task in DEFAULT_TASK_CONFIGS[tab_id]:
                        # Check if there's a custom label for this task
                        # Old format used uppercase enum names, new format uses lowercase values
                        task_name = next(
                            (name for name, value in task_name_to_value.items() if value == default_task.id),
                            None
                        )
                        custom_label = button_labels.get(task_name) if task_name else None
                        
                        # Also try the lowercase value directly for compatibility
                        if not custom_label:
                            custom_label = button_labels.get(default_task.id)
                        
                        task = TaskConfig(
                            id=default_task.id,
                            name=default_task.name,
                            button_label=custom_label if custom_label else default_task.button_label,
                            prompt_template=default_task.prompt_template,
                            enabled=default_task.enabled
                        )
                        new_tasks.append(task)
                    
                    # Update config with new format
                    tab_config["tasks"] = [task.to_dict() for task in new_tasks]
                    self._task_configs[tab_id] = new_tasks
                    
                    # Remove old button_labels key
                    del tab_config["button_labels"]
                    
                    migrated = True
                    log.info(f"Migrated tab '{tab_id}' from button_labels to tasks format")
        
        # Also check for old user-level settings.json with custom_prompts
        # (This would be loaded separately by the application, but we document the migration path)
        
        return migrated
    
    def _save_config_internal(self, existing_config: Optional[dict] = None) -> None:
        """
        Internal method to save config (assumes lock is already held).
        
        Uses atomic write pattern: write to temp file, then rename.
        
        Args:
            existing_config: Optional existing config dict to use instead of reading from file
        """
        try:
            # Read existing config to preserve other fields (unless provided)
            if existing_config is not None:
                config = existing_config
            else:
                config = {}
                if self._config_path.exists():
                    try:
                        with open(self._config_path, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                    except Exception as e:
                        log.warning(f"Failed to read existing config during save, starting fresh: {e}")
            
            # Merge task configurations into config
            for tab_id, tasks in self._task_configs.items():
                if tab_id not in config:
                    config[tab_id] = {}
                config[tab_id]["tasks"] = [task.to_dict() for task in tasks]
            
            # Merge chat configurations into config
            for tab_id, chat_config in self._chat_configs.items():
                if tab_id not in config:
                    config[tab_id] = {}
                config[tab_id]["chat_config"] = chat_config.to_dict()
            
            # Ensure parent directory exists
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Atomic write: write to temp file, then rename
            temp_path = self._config_path.with_suffix('.json.tmp')
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            # Atomic rename
            temp_path.replace(self._config_path)
            
            log.debug(f"Saved task configurations to {self._config_path}")
        
        except Exception as e:
            log.error(f"Failed to save task configurations: {e}", exc_info=True)
            raise
    
    def save_config(self) -> bool:
        """
        Save task configurations to config file.
        
        Thread-safe operation with atomic file write.
        
        Returns:
            True if save succeeded, False otherwise
        """
        try:
            with self._lock:
                self._save_config_internal()
            return True
        except Exception as e:
            log.error(f"Failed to save config: {e}", exc_info=True)
            return False
    
    def get_task_config(self, tab_id: str, task_id: str) -> Optional[TaskConfig]:
        """
        Get configuration for a specific task.
        
        Args:
            tab_id: Tab identifier (e.g., "text_json", "json_code")
            task_id: Task identifier (LLMTask enum value)
        
        Returns:
            TaskConfig if found, None otherwise (with fallback to defaults)
        """
        with self._lock:
            # Check current config
            if tab_id in self._task_configs:
                for task in self._task_configs[tab_id]:
                    if task.id == task_id:
                        return task
            
            # Fallback to defaults
            if tab_id in DEFAULT_TASK_CONFIGS:
                for default_task in DEFAULT_TASK_CONFIGS[tab_id]:
                    if default_task.id == task_id:
                        # Return a copy so modifications don't affect defaults
                        return TaskConfig(**asdict(default_task))
        
        return None
    
    def get_all_tasks_for_tab(self, tab_id: str) -> List[TaskConfig]:
        """
        Get all task configurations for a tab.
        
        Args:
            tab_id: Tab identifier
        
        Returns:
            List of TaskConfig objects (returns copy to prevent external modification)
        """
        with self._lock:
            if tab_id in self._task_configs:
                # Return copies to prevent external modification
                return [TaskConfig(**asdict(task)) for task in self._task_configs[tab_id]]
            elif tab_id in DEFAULT_TASK_CONFIGS:
                # Return default configs
                return [TaskConfig(**asdict(task)) for task in DEFAULT_TASK_CONFIGS[tab_id]]
            else:
                log.warning(f"Unknown tab_id: {tab_id}")
                return []
    
    def add_task(self, tab_id: str, task_config: TaskConfig) -> bool:
        """
        Add a new task configuration to a tab.
        
        Args:
            tab_id: Tab identifier
            task_config: Task configuration to add
        
        Returns:
            True if task was added, False if task with same ID already exists
        """
        with self._lock:
            # Initialize tab if needed
            if tab_id not in self._task_configs:
                self._task_configs[tab_id] = []
            
            # Check for duplicate ID
            for existing_task in self._task_configs[tab_id]:
                if existing_task.id == task_config.id:
                    log.warning(f"Task with id '{task_config.id}' already exists in tab '{tab_id}'")
                    return False
            
            # Add task
            self._task_configs[tab_id].append(task_config)
            log.info(f"Added task '{task_config.id}' to tab '{tab_id}'")
            return True
    
    def update_task(self, tab_id: str, task_config: TaskConfig) -> bool:
        """
        Update an existing task configuration.
        
        Args:
            tab_id: Tab identifier
            task_config: Updated task configuration
        
        Returns:
            True if task was updated, False if task not found
        """
        with self._lock:
            if tab_id not in self._task_configs:
                log.warning(f"Tab '{tab_id}' not found")
                return False
            
            # Find and update task
            for i, existing_task in enumerate(self._task_configs[tab_id]):
                if existing_task.id == task_config.id:
                    self._task_configs[tab_id][i] = task_config
                    log.info(f"Updated task '{task_config.id}' in tab '{tab_id}'")
                    return True
            
            log.warning(f"Task '{task_config.id}' not found in tab '{tab_id}'")
            return False
    
    def delete_task(self, tab_id: str, task_id: str) -> bool:
        """
        Delete a task configuration from a tab.
        
        Args:
            tab_id: Tab identifier
            task_id: Task identifier to delete
        
        Returns:
            True if task was deleted, False if task not found
        """
        with self._lock:
            if tab_id not in self._task_configs:
                log.warning(f"Tab '{tab_id}' not found")
                return False
            
            # Find and remove task
            for i, task in enumerate(self._task_configs[tab_id]):
                if task.id == task_id:
                    deleted_task = self._task_configs[tab_id].pop(i)
                    log.info(f"Deleted task '{task_id}' from tab '{tab_id}'")
                    return True
            
            log.warning(f"Task '{task_id}' not found in tab '{tab_id}'")
            return False
    
    def reset_to_defaults(self, tab_id: str) -> bool:
        """
        Reset a tab's task configurations to defaults.
        
        Saves current config as .old backup if different from defaults.
        
        Args:
            tab_id: Tab identifier to reset
        
        Returns:
            True if reset succeeded, False otherwise
        """
        with self._lock:
            if tab_id not in DEFAULT_TASK_CONFIGS:
                log.error(f"Unknown tab_id: {tab_id}")
                return False
            
            # Check if current config differs from defaults
            current_tasks = self._task_configs.get(tab_id, [])
            default_tasks = DEFAULT_TASK_CONFIGS[tab_id]
            
            # Compare configurations
            is_different = False
            if len(current_tasks) != len(default_tasks):
                is_different = True
            else:
                for current, default in zip(current_tasks, default_tasks):
                    if (current.id != default.id or
                        current.button_label != default.button_label or
                        current.prompt_template != default.prompt_template or
                        current.enabled != default.enabled):
                        is_different = True
                        break
            
            # Backup current config if different
            if is_different and self._config_path.exists():
                try:
                    backup_path = self._config_path.with_suffix('.json.old')
                    shutil.copy2(self._config_path, backup_path)
                    log.info(f"Backed up current config to {backup_path}")
                except Exception as e:
                    log.warning(f"Failed to backup config: {e}")
            
            # Reset to defaults
            self._task_configs[tab_id] = [
                TaskConfig(**asdict(task))
                for task in default_tasks
            ]
            
            log.info(f"Reset tab '{tab_id}' to default task configurations")
            return True
    
    def get_task_ids_for_tab(self, tab_id: str) -> List[str]:
        """
        Get list of task IDs for a tab.
        
        Args:
            tab_id: Tab identifier
        
        Returns:
            List of task IDs
        """
        tasks = self.get_all_tasks_for_tab(tab_id)
        return [task.id for task in tasks]
    
    def is_task_enabled(self, tab_id: str, task_id: str) -> bool:
        """
        Check if a task is enabled.
        
        Args:
            tab_id: Tab identifier
            task_id: Task identifier
        
        Returns:
            True if task is enabled, False otherwise (defaults to True if not found)
        """
        task = self.get_task_config(tab_id, task_id)
        return task.enabled if task else True
    
    def set_task_enabled(self, tab_id: str, task_id: str, enabled: bool) -> bool:
        """
        Enable or disable a task.
        
        Args:
            tab_id: Tab identifier
            task_id: Task identifier
            enabled: Whether to enable or disable the task
        
        Returns:
            True if task was updated, False if task not found
        """
        with self._lock:
            # Initialize tab from defaults if needed
            if tab_id not in self._task_configs:
                if tab_id in DEFAULT_TASK_CONFIGS:
                    self._task_configs[tab_id] = [
                        TaskConfig(**asdict(task))
                        for task in DEFAULT_TASK_CONFIGS[tab_id]
                    ]
                else:
                    return False
            
            # Find and update task
            for task in self._task_configs[tab_id]:
                if task.id == task_id:
                    task.enabled = enabled
                    log.info(f"{'Enabled' if enabled else 'Disabled'} task '{task_id}' in tab '{tab_id}'")
                    return True
            
            # Task not found even in defaults
            log.warning(f"Task '{task_id}' not found in tab '{tab_id}'")
            return False
    
    def set_all_tasks_for_tab(self, tab_id: str, tasks: List[TaskConfig]) -> None:
        """
        Replace all task configurations for a tab.
        
        Thread-safe bulk replacement of the entire task list for a given tab.
        Use this instead of directly accessing _task_configs.
        
        Args:
            tab_id: Tab identifier (e.g., "text_json", "json_code")
            tasks: Complete list of TaskConfig objects to set
        """
        with self._lock:
            self._task_configs[tab_id] = list(tasks)
            log.info(f"Replaced all tasks for tab '{tab_id}' ({len(tasks)} tasks)")

    def get_chat_config(self, tab_id: str) -> ChatConfig:
        """
        Get chat configuration for a tab.
        
        Args:
            tab_id: Tab identifier
        
        Returns:
            ChatConfig for the tab (returns default if not found)
        """
        with self._lock:
            if tab_id in self._chat_configs:
                return ChatConfig(
                    enabled=self._chat_configs[tab_id].enabled,
                    system_prompt=self._chat_configs[tab_id].system_prompt
                )
            if tab_id in DEFAULT_CHAT_CONFIG:
                default = DEFAULT_CHAT_CONFIG[tab_id]
                return ChatConfig(enabled=default.enabled, system_prompt=default.system_prompt)
            return ChatConfig()
    
    def set_chat_config(self, tab_id: str, chat_config: ChatConfig) -> None:
        """
        Set chat configuration for a tab.
        
        Args:
            tab_id: Tab identifier
            chat_config: Chat configuration to set
        """
        with self._lock:
            self._chat_configs[tab_id] = chat_config
            log.info(f"Updated chat config for tab '{tab_id}'")

    def get_enabled_tasks_for_tab(self, tab_id: str) -> List[TaskConfig]:
        """
        Get all enabled task configurations for a tab.
        
        Args:
            tab_id: Tab identifier
        
        Returns:
            List of enabled TaskConfig objects
        """
        all_tasks = self.get_all_tasks_for_tab(tab_id)
        return [task for task in all_tasks if task.enabled]
    
    def update_task_config(
        self,
        tab_id: str,
        task_id: str,
        button_label: Optional[str] = None,
        prompt_template: Optional[str] = None,
        enabled: Optional[bool] = None
    ) -> bool:
        """
        Convenience method to update specific fields of a task configuration.
        
        Updates only the provided fields, leaving others unchanged.
        
        Args:
            tab_id: Tab identifier
            task_id: Task identifier
            button_label: Optional new button label
            prompt_template: Optional new prompt template
            enabled: Optional enabled state
        
        Returns:
            True if task was updated, False if task not found
        """
        # Get current task config
        task = self.get_task_config(tab_id, task_id)
        if task is None:
            log.warning(f"Task '{task_id}' not found in tab '{tab_id}'")
            return False
        
        # Update fields if provided
        if button_label is not None:
            task.button_label = button_label
        if prompt_template is not None:
            task.prompt_template = prompt_template
        if enabled is not None:
            task.enabled = enabled
        
        # Update the task
        return self.update_task(tab_id, task)
