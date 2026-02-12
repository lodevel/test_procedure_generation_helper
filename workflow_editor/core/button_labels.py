"""
Button Label Manager - Manages custom button labels for LLM tasks.

**DEPRECATED:** This module is deprecated in favor of TaskConfigManager.
Use workflow_editor.core.task_config.TaskConfigManager instead, which provides
unified management of both button labels and prompt templates.

ButtonLabelManager will be maintained for backward compatibility but is no longer
actively developed. New code should use TaskConfigManager.

Migration guide:
- TaskConfigManager provides all ButtonLabelManager functionality
- Plus: integrated prompt template management
- Plus: task enable/disable functionality
- Plus: better migration support from old configurations

Provides a centralized system for managing button labels displayed in tabs:
- Default labels for all LLM tasks
- Custom label overrides per tab/task
- Persistence to config/tab_contexts.json
- Thread-safe operations
- Graceful fallback when config is missing
"""

import json
import logging
import warnings
from pathlib import Path
from typing import Dict, Optional
from threading import Lock

from ..llm.backend_base import LLMTask

log = logging.getLogger(__name__)


# Deprecation warning helper
def _deprecated_warning(message: str):
    """Issue a deprecation warning."""
    warnings.warn(
        f"ButtonLabelManager is deprecated: {message}. "
        "Use TaskConfigManager instead.",
        DeprecationWarning,
        stacklevel=3
    )


# Default button labels for all LLM tasks
# These use concise labels optimized for UI space
DEFAULT_BUTTON_LABELS = {
    LLMTask.DERIVE_JSON_FROM_TEXT: "Text → JSON",
    LLMTask.RENDER_TEXT_FROM_JSON: "JSON → Text",
    LLMTask.REVIEW_TEXT_PROCEDURE: "Review Text",
    LLMTask.REVIEW_JSON: "Review JSON",
    LLMTask.REVIEW_TEXT_VS_JSON: "Check Text↔JSON",
    LLMTask.GENERATE_CODE_FROM_JSON: "JSON → Code",
    LLMTask.DERIVE_JSON_FROM_CODE: "Code → JSON",
    LLMTask.REVIEW_CODE: "Review Code",
    LLMTask.REVIEW_CODE_VS_JSON: "Check JSON↔Code",
    LLMTask.AD_HOC_CHAT: "Ask LLM",
}


class ButtonLabelManager:
    """
    Manages button labels for LLM tasks across all tabs.
    
    .. deprecated::
        ButtonLabelManager is deprecated. Use TaskConfigManager instead for
        unified management of button labels, prompt templates, and task settings.
    
    Features:
    - Per-tab custom label overrides
    - Automatic persistence to config/tab_contexts.json
    - Thread-safe operations
    - Graceful degradation when config file is missing
    - Validation of label inputs
    
    The config file structure for button labels:
    {
        "text_json": {
            "selected_rules": [...],
            "button_labels": {
                "DERIVE_JSON_FROM_TEXT": "Custom Label",
                ...
            }
        },
        ...
    }
    """
    
    # Maximum label length to prevent UI overflow
    MAX_LABEL_LENGTH = 50
    
    def __init__(self, config_path: Path):
        """
        Initialize the button label manager.
        
        .. deprecated::
            Use TaskConfigManager instead.
        
        Args:
            config_path: Path to config/tab_contexts.json
        """
        _deprecated_warning("ButtonLabelManager.__init__ called")
        self._config_path = config_path
        self._custom_labels: Dict[str, Dict[str, str]] = {}
        self._lock = Lock()
        
        # Load existing custom labels
        self._load_custom_labels()
        
        log.info(f"ButtonLabelManager initialized with config: {config_path} (DEPRECATED)")
    
    def _load_custom_labels(self) -> None:
        """
        Load custom labels from config file.
        
        Handles missing files gracefully and validates data structure.
        Logs warnings for invalid data but continues with defaults.
        """
        if not self._config_path.exists():
            log.info(f"Config file not found: {self._config_path}, using defaults")
            return
        
        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # Extract button_labels from each tab context
            for tab_id, tab_config in config.items():
                if not isinstance(tab_config, dict):
                    continue
                
                button_labels = tab_config.get("button_labels", {})
                if isinstance(button_labels, dict) and button_labels:
                    self._custom_labels[tab_id] = button_labels
                    log.debug(f"Loaded {len(button_labels)} custom labels for tab '{tab_id}'")
            
            if self._custom_labels:
                log.info(f"Loaded custom labels for {len(self._custom_labels)} tab(s)")
        
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse config file: {e}")
        except Exception as e:
            log.error(f"Error loading custom labels: {e}", exc_info=True)
    
    def get_label(
        self,
        task: LLMTask,
        tab_id: Optional[str] = None,
        force_default: bool = False
    ) -> str:
        """
        Get the button label for a task, with fallback logic.
        
        Lookup order:
        1. Custom label for this tab/task (if tab_id provided and not force_default)
        2. Default label for this task
        3. Fallback to task name if default missing
        
        Args:
            task: The LLM task to get label for
            tab_id: Optional tab identifier (e.g., "text_json", "json_code")
            force_default: If True, ignore custom labels and use default
        
        Returns:
            The button label string
        """
        # Check for custom label (if tab specified and not forcing default)
        if tab_id and not force_default:
            with self._lock:
                tab_labels = self._custom_labels.get(tab_id, {})
                custom_label = tab_labels.get(task.value)
                if custom_label:
                    return custom_label
        
        # Fall back to default label
        default_label = DEFAULT_BUTTON_LABELS.get(task)
        if default_label:
            return default_label
        
        # Final fallback: use task name (shouldn't normally happen)
        log.warning(f"No label found for task {task}, using task name as fallback")
        return task.value.replace("_", " ").title()
    
    def set_label(
        self,
        task: LLMTask,
        tab_id: str,
        label: str
    ) -> bool:
        """
        Set a custom label for a task in a specific tab.
        
        Args:
            task: The LLM task to set label for
            tab_id: Tab identifier (e.g., "text_json", "json_code")
            label: The custom label text
        
        Returns:
            True if label was set successfully, False if validation failed
        """
        # Validate label
        if not label or not label.strip():
            log.warning(f"Attempted to set empty label for {task} in {tab_id}")
            return False
        
        label = label.strip()
        if len(label) > self.MAX_LABEL_LENGTH:
            log.warning(f"Label too long ({len(label)} > {self.MAX_LABEL_LENGTH}), truncating")
            label = label[:self.MAX_LABEL_LENGTH]
        
        # Set custom label
        with self._lock:
            if tab_id not in self._custom_labels:
                self._custom_labels[tab_id] = {}
            self._custom_labels[tab_id][task.value] = label
        
        log.info(f"Set custom label for {task} in {tab_id}: '{label}'")
        return True
    
    def clear_label(
        self,
        task: LLMTask,
        tab_id: str
    ) -> bool:
        """
        Clear a custom label for a task in a specific tab.
        
        After clearing, the default label will be used.
        
        Args:
            task: The LLM task to clear label for
            tab_id: Tab identifier
        
        Returns:
            True if a label was cleared, False if no custom label existed
        """
        with self._lock:
            if tab_id in self._custom_labels:
                if task.value in self._custom_labels[tab_id]:
                    del self._custom_labels[tab_id][task.value]
                    log.info(f"Cleared custom label for {task} in {tab_id}")
                    return True
        return False
    
    def clear_tab_labels(self, tab_id: str) -> int:
        """
        Clear all custom labels for a tab.
        
        Args:
            tab_id: Tab identifier
        
        Returns:
            Number of labels cleared
        """
        with self._lock:
            if tab_id in self._custom_labels:
                count = len(self._custom_labels[tab_id])
                del self._custom_labels[tab_id]
                log.info(f"Cleared {count} custom label(s) for tab '{tab_id}'")
                return count
        return 0
    
    def clear_all_labels(self) -> int:
        """
        Clear all custom labels across all tabs.
        
        Returns:
            Total number of labels cleared
        """
        with self._lock:
            total = sum(len(labels) for labels in self._custom_labels.values())
            self._custom_labels.clear()
            log.info(f"Cleared all custom labels ({total} total)")
            return total
    
    def get_all_custom_labels(self, tab_id: Optional[str] = None) -> Dict[str, str]:
        """
        Get all custom labels, optionally filtered by tab.
        
        Args:
            tab_id: Optional tab identifier to filter by
        
        Returns:
            Dictionary mapping task values to custom labels
        """
        with self._lock:
            if tab_id:
                return self._custom_labels.get(tab_id, {}).copy()
            else:
                # Return all labels from all tabs
                all_labels = {}
                for tab_labels in self._custom_labels.values():
                    all_labels.update(tab_labels)
                return all_labels
    
    def save_custom_labels(self) -> bool:
        """
        Save custom labels to config file.
        
        Merges button labels into existing config structure, preserving
        other settings like selected_rules.
        
        Returns:
            True if save succeeded, False otherwise
        """
        try:
            # Read existing config (if any)
            config = {}
            if self._config_path.exists():
                try:
                    with open(self._config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                except Exception as e:
                    log.warning(f"Failed to read existing config, starting fresh: {e}")
            
            # Merge button labels into config
            with self._lock:
                for tab_id, button_labels in self._custom_labels.items():
                    if tab_id not in config:
                        config[tab_id] = {}
                    config[tab_id]["button_labels"] = button_labels
                
                # Also remove button_labels from tabs that have no custom labels
                for tab_id in config:
                    if tab_id not in self._custom_labels and "button_labels" in config[tab_id]:
                        del config[tab_id]["button_labels"]
            
            # Ensure parent directory exists
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write config atomically (write to temp file, then rename)
            temp_path = self._config_path.with_suffix('.json.tmp')
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            temp_path.replace(self._config_path)
            
            log.info(f"Saved custom labels to {self._config_path}")
            return True
        
        except Exception as e:
            log.error(f"Failed to save custom labels: {e}", exc_info=True)
            return False
    
    def reset_to_defaults(self) -> None:
        """
        Reset all labels to defaults (clear all custom labels).
        
        Note: This only clears in-memory state. Call save_custom_labels()
        to persist the reset to disk.
        """
        cleared = self.clear_all_labels()
        log.info(f"Reset to default labels, cleared {cleared} custom label(s)")
    
    def get_tabs_with_custom_labels(self) -> list[str]:
        """
        Get list of tab IDs that have custom labels.
        
        Returns:
            List of tab identifiers
        """
        with self._lock:
            return list(self._custom_labels.keys())
    
    def has_custom_label(self, task: LLMTask, tab_id: str) -> bool:
        """
        Check if a task has a custom label in a specific tab.
        
        Args:
            task: The LLM task to check
            tab_id: Tab identifier
        
        Returns:
            True if a custom label exists for this task/tab
        """
        with self._lock:
            return (
                tab_id in self._custom_labels and
                task.value in self._custom_labels[tab_id]
            )
