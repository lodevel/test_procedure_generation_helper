"""
Comprehensive tests for TaskConfigManager.

Tests all major functionality:
- Load/save operations
- Add/delete tasks
- Migration from old format
- Reset to defaults with backup
- Concurrent access (thread safety)
- Corrupted config recovery
- update_task_config convenience method
"""

import json
import pytest
import threading
import time
from pathlib import Path
from workflow_editor.core.task_config import TaskConfigManager, TaskConfig, DEFAULT_TASK_CONFIGS
from workflow_editor.llm.backend_base import LLMTask


class TestTaskConfigManagerInitialization:
    """Test initialization and loading."""
    
    def test_initialization_with_no_config_file(self, tmp_path):
        """Test that manager initializes with defaults when no config file exists."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Should have defaults for all tabs
        for tab_id in DEFAULT_TASK_CONFIGS:
            tasks = manager.get_all_tasks_for_tab(tab_id)
            assert len(tasks) > 0, f"Tab '{tab_id}' should have default tasks"
            
            # Verify default tasks are loaded
            default_count = len(DEFAULT_TASK_CONFIGS[tab_id])
            assert len(tasks) == default_count, f"Tab '{tab_id}' should have {default_count} tasks"
    
    def test_initialization_with_existing_config(self, tmp_path):
        """Test that manager loads existing configuration."""
        config_path = tmp_path / "tab_contexts.json"
        
        # Create initial config
        config = {
            "text_json": {
                "tasks": [
                    {
                        "id": LLMTask.DERIVE_JSON_FROM_TEXT.value,
                        "name": "Custom Name",
                        "button_label": "Custom Label",
                        "prompt_template": "Custom Prompt",
                        "enabled": True
                    }
                ]
            }
        }
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f)
        
        # Load manager
        manager = TaskConfigManager(config_path)
        
        # Verify custom config was loaded
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task is not None
        assert task.name == "Custom Name"
        assert task.button_label == "Custom Label"
        assert task.prompt_template == "Custom Prompt"


class TestTaskConfigManagerCRUD:
    """Test Create, Read, Update, Delete operations."""
    
    def test_get_task_config(self, tmp_path):
        """Test retrieving a specific task configuration."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Get existing task
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task is not None
        assert task.id == LLMTask.DERIVE_JSON_FROM_TEXT.value
        assert task.button_label == "Text → JSON"
    
    def test_get_all_tasks_for_tab(self, tmp_path):
        """Test retrieving all tasks for a tab."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        tasks = manager.get_all_tasks_for_tab("text_json")
        assert len(tasks) > 0
        
        # Verify all tasks are TaskConfig objects
        for task in tasks:
            assert isinstance(task, TaskConfig)
            assert task.id
            assert task.name
            assert task.button_label
    
    def test_add_task(self, tmp_path):
        """Test adding a new task."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Get initial count
        initial_tasks = manager.get_all_tasks_for_tab("text_json")
        initial_count = len(initial_tasks)
        
        # Add custom task
        custom_task = TaskConfig(
            id="custom_task_test",
            name="Custom Test Task",
            button_label="Custom Test",
            prompt_template="Test prompt",
            enabled=True
        )
        
        success = manager.add_task("text_json", custom_task)
        assert success is True
        
        # Verify task was added
        tasks = manager.get_all_tasks_for_tab("text_json")
        assert len(tasks) == initial_count + 1
        
        # Verify we can retrieve it
        retrieved = manager.get_task_config("text_json", "custom_task_test")
        assert retrieved is not None
        assert retrieved.name == "Custom Test Task"
    
    def test_add_duplicate_task_fails(self, tmp_path):
        """Test that adding a task with duplicate ID fails."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Try to add task with existing ID
        duplicate_task = TaskConfig(
            id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            name="Duplicate",
            button_label="Duplicate",
            prompt_template=None,
            enabled=True
        )
        
        success = manager.add_task("text_json", duplicate_task)
        assert success is False
    
    def test_update_task(self, tmp_path):
        """Test updating an existing task."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Get existing task
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task is not None
        
        # Modify task
        task.button_label = "Modified Label"
        task.prompt_template = "Modified Prompt"
        
        # Update
        success = manager.update_task("text_json", task)
        assert success is True
        
        # Verify changes persisted
        updated = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert updated.button_label == "Modified Label"
        assert updated.prompt_template == "Modified Prompt"
    
    def test_update_nonexistent_task_fails(self, tmp_path):
        """Test that updating a non-existent task fails."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        fake_task = TaskConfig(
            id="nonexistent_task",
            name="Fake",
            button_label="Fake",
            prompt_template=None,
            enabled=True
        )
        
        success = manager.update_task("text_json", fake_task)
        assert success is False
    
    def test_delete_task(self, tmp_path):
        """Test deleting a task."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Add a task to delete
        custom_task = TaskConfig(
            id="task_to_delete",
            name="Delete Me",
            button_label="Delete",
            prompt_template=None,
            enabled=True
        )
        manager.add_task("text_json", custom_task)
        
        # Get count before delete
        before_count = len(manager.get_all_tasks_for_tab("text_json"))
        
        # Delete task
        success = manager.delete_task("text_json", "task_to_delete")
        assert success is True
        
        # Verify deletion
        after_count = len(manager.get_all_tasks_for_tab("text_json"))
        assert after_count == before_count - 1
        
        # Verify task is gone
        deleted = manager.get_task_config("text_json", "task_to_delete")
        assert deleted is None
    
    def test_delete_nonexistent_task_fails(self, tmp_path):
        """Test that deleting a non-existent task fails."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        success = manager.delete_task("text_json", "nonexistent_task")
        assert success is False


class TestTaskConfigManagerConvenience:
    """Test convenience methods."""
    
    def test_update_task_config_button_label(self, tmp_path):
        """Test update_task_config convenience method for button label."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Update just the button label
        success = manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="New Label"
        )
        assert success is True
        
        # Verify only button label changed
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task.button_label == "New Label"
        assert task.prompt_template is None  # Unchanged
        assert task.enabled is True  # Unchanged
    
    def test_update_task_config_prompt_template(self, tmp_path):
        """Test update_task_config convenience method for prompt template."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Update just the prompt template
        success = manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            prompt_template="New Prompt"
        )
        assert success is True
        
        # Verify only prompt changed
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task.prompt_template == "New Prompt"
    
    def test_update_task_config_multiple_fields(self, tmp_path):
        """Test update_task_config with multiple fields."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Update multiple fields
        success = manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="Multi Label",
            prompt_template="Multi Prompt",
            enabled=False
        )
        assert success is True
        
        # Verify all fields changed
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task.button_label == "Multi Label"
        assert task.prompt_template == "Multi Prompt"
        assert task.enabled is False
    
    def test_update_task_config_nonexistent_fails(self, tmp_path):
        """Test update_task_config fails for non-existent task."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        success = manager.update_task_config(
            tab_id="text_json",
            task_id="nonexistent_task",
            button_label="New Label"
        )
        assert success is False


class TestTaskConfigManagerPersistence:
    """Test save/load operations."""
    
    def test_save_config(self, tmp_path):
        """Test saving configuration to file."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Modify a task
        manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="Saved Label"
        )
        
        # Save
        success = manager.save_config()
        assert success is True
        
        # Verify file exists
        assert config_path.exists()
        
        # Load in new manager and verify
        manager2 = TaskConfigManager(config_path)
        task = manager2.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task.button_label == "Saved Label"
    
    def test_atomic_save(self, tmp_path):
        """Test that save uses atomic write pattern."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Make a change and save
        manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="Atomic Test"
        )
        manager.save_config()
        
        # Temp file should not exist after save
        temp_path = config_path.with_suffix('.json.tmp')
        assert not temp_path.exists(), "Temp file should be cleaned up"


class TestTaskConfigManagerMigration:
    """Test migration from old button_labels format."""
    
    def test_migrate_from_button_labels(self, tmp_path):
        """Test migration from old button_labels format to tasks format."""
        config_path = tmp_path / "tab_contexts.json"
        
        # Create old format config
        old_config = {
            "text_json": {
                "selected_rules": ["rule1.py"],
                "button_labels": {
                    "DERIVE_JSON_FROM_TEXT": "Old Custom Label",
                    "REVIEW_JSON": "Old Review Label"
                }
            }
        }
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(old_config, f)
        
        # Load with manager (should trigger migration)
        manager = TaskConfigManager(config_path)
        
        # Verify migration occurred
        task1 = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task1 is not None
        assert task1.button_label == "Old Custom Label"
        
        task2 = manager.get_task_config("text_json", LLMTask.REVIEW_JSON.value)
        assert task2 is not None
        assert task2.button_label == "Old Review Label"
        
        # Verify new format in file
        with open(config_path, 'r', encoding='utf-8') as f:
            new_config = json.load(f)
        
        assert "tasks" in new_config["text_json"]
        assert "button_labels" not in new_config["text_json"]
        assert "selected_rules" in new_config["text_json"]  # Should be preserved
    
    def test_migration_preserves_other_settings(self, tmp_path):
        """Test that migration preserves other settings like selected_rules."""
        config_path = tmp_path / "tab_contexts.json"
        
        old_config = {
            "text_json": {
                "selected_rules": ["rule1.py", "rule2.py"],
                "button_labels": {
                    "DERIVE_JSON_FROM_TEXT": "Custom"
                },
                "custom_setting": "preserved"
            }
        }
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(old_config, f)
        
        manager = TaskConfigManager(config_path)
        
        # Load file and check
        with open(config_path, 'r', encoding='utf-8') as f:
            new_config = json.load(f)
        
        assert new_config["text_json"]["selected_rules"] == ["rule1.py", "rule2.py"]
        assert new_config["text_json"]["custom_setting"] == "preserved"


class TestTaskConfigManagerRecovery:
    """Test auto-recovery from corrupted config."""
    
    def test_auto_recovery_from_corrupted_json(self, tmp_path):
        """Test that manager recovers from corrupted JSON file."""
        config_path = tmp_path / "tab_contexts.json"
        
        # Create corrupted JSON file
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write("{ corrupted json file }")
        
        # Load manager (should auto-recover)
        manager = TaskConfigManager(config_path)
        
        # Should have defaults
        tasks = manager.get_all_tasks_for_tab("text_json")
        assert len(tasks) > 0
        
        # Backup file should exist
        backup_path = config_path.with_suffix('.json.corrupted')
        assert backup_path.exists()
    
    def test_auto_recovery_saves_clean_config(self, tmp_path):
        """Test that auto-recovery saves a clean config file."""
        config_path = tmp_path / "tab_contexts.json"
        
        # Create corrupted file
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write("{ invalid }")
        
        # Load and trigger recovery
        manager = TaskConfigManager(config_path)
        
        # File should now be valid JSON
        with open(config_path, 'r', encoding='utf-8') as f:
            recovered_config = json.load(f)
        
        assert isinstance(recovered_config, dict)
        assert "text_json" in recovered_config or "json_code" in recovered_config


class TestTaskConfigManagerReset:
    """Test reset to defaults functionality."""
    
    def test_reset_to_defaults(self, tmp_path):
        """Test resetting a tab's tasks to defaults."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Modify a task
        manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="Modified"
        )
        manager.save_config()
        
        # Reset
        success = manager.reset_to_defaults("text_json")
        assert success is True
        
        # Verify reset
        task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
        assert task.button_label == "Text → JSON"  # Default label
    
    def test_reset_creates_backup(self, tmp_path):
        """Test that reset creates a backup of the old config."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Modify and save
        manager.update_task_config(
            tab_id="text_json",
            task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
            button_label="To Backup"
        )
        manager.save_config()
        
        # Reset (should create backup)
        manager.reset_to_defaults("text_json")
        manager.save_config()
        
        # Backup should exist
        backup_path = config_path.with_suffix('.json.old')
        assert backup_path.exists()


class TestTaskConfigManagerEnabled:
    """Test task enabled/disabled functionality."""
    
    def test_is_task_enabled(self, tmp_path):
        """Test checking if a task is enabled."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Default should be enabled
        assert manager.is_task_enabled("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value) is True
    
    def test_set_task_enabled(self, tmp_path):
        """Test enabling/disabling a task."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Disable task
        success = manager.set_task_enabled(
            "text_json",
            LLMTask.DERIVE_JSON_FROM_TEXT.value,
            False
        )
        assert success is True
        
        # Verify disabled
        assert manager.is_task_enabled("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value) is False
        
        # Re-enable
        success = manager.set_task_enabled(
            "text_json",
            LLMTask.DERIVE_JSON_FROM_TEXT.value,
            True
        )
        assert success is True
        assert manager.is_task_enabled("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value) is True
    
    def test_get_enabled_tasks_for_tab(self, tmp_path):
        """Test getting only enabled tasks."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        # Get all tasks
        all_tasks = manager.get_all_tasks_for_tab("text_json")
        initial_count = len(all_tasks)
        
        # Disable one task
        manager.set_task_enabled(
            "text_json",
            LLMTask.DERIVE_JSON_FROM_TEXT.value,
            False
        )
        
        # Get only enabled tasks
        enabled_tasks = manager.get_enabled_tasks_for_tab("text_json")
        assert len(enabled_tasks) == initial_count - 1
        
        # Verify disabled task is not in list
        task_ids = [task.id for task in enabled_tasks]
        assert LLMTask.DERIVE_JSON_FROM_TEXT.value not in task_ids


class TestTaskConfigManagerThreadSafety:
    """Test thread-safe operations."""
    
    def test_concurrent_updates(self, tmp_path):
        """Test that concurrent updates are thread-safe."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        results = []
        errors = []
        
        def update_label(label_suffix):
            try:
                for i in range(10):
                    success = manager.update_task_config(
                        tab_id="text_json",
                        task_id=LLMTask.DERIVE_JSON_FROM_TEXT.value,
                        button_label=f"Label_{label_suffix}_{i}"
                    )
                    results.append(success)
                    time.sleep(0.001)  # Small delay to encourage contention
            except Exception as e:
                errors.append(e)
        
        # Create multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=update_label, args=(i,))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads
        for thread in threads:
            thread.join()
        
        # No errors should occur
        assert len(errors) == 0
        
        # All updates should succeed
        assert all(results)
    
    def test_concurrent_reads(self, tmp_path):
        """Test that concurrent reads work correctly."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        results = []
        
        def read_config():
            for _ in range(20):
                task = manager.get_task_config("text_json", LLMTask.DERIVE_JSON_FROM_TEXT.value)
                results.append(task is not None)
        
        # Create multiple reader threads
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=read_config)
            threads.append(thread)
            thread.start()
        
        # Wait for all threads
        for thread in threads:
            thread.join()
        
        # All reads should succeed
        assert all(results)


class TestTaskConfigManagerHelpers:
    """Test helper methods."""
    
    def test_get_task_ids_for_tab(self, tmp_path):
        """Test getting list of task IDs for a tab."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        task_ids = manager.get_task_ids_for_tab("text_json")
        
        assert len(task_ids) > 0
        assert LLMTask.DERIVE_JSON_FROM_TEXT.value in task_ids
        assert LLMTask.REVIEW_JSON.value in task_ids
    
    def test_unknown_tab_returns_empty_list(self, tmp_path):
        """Test that unknown tab returns empty list."""
        config_path = tmp_path / "tab_contexts.json"
        manager = TaskConfigManager(config_path)
        
        tasks = manager.get_all_tasks_for_tab("unknown_tab")
        assert tasks == []
