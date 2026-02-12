"""
Test BaseTab button label integration.

Verifies that BaseTab's new methods work correctly:
- create_action_group()
- create_task_button()
- refresh_button_labels()
"""

import pytest
from pathlib import Path
from PySide6.QtWidgets import QApplication, QPushButton, QGroupBox, QVBoxLayout

from workflow_editor.tabs.base_tab import BaseTab
from workflow_editor.llm.backend_base import LLMTask
from workflow_editor.core.task_config import TaskConfigManager


class TestBaseTabButtonLabels:
    """Test BaseTab button label methods."""
    
    def test_create_action_group_file_style(self, base_tab):
        """Test creating action group with file style."""
        group = base_tab.create_action_group("Test Group", "file")
        
        assert isinstance(group, QGroupBox)
        assert group.title() == "Test Group"
        
        # Verify light blue styling is applied
        stylesheet = group.styleSheet()
        assert "#e8f4f8" in stylesheet  # Light blue background
        assert "#b3d9e8" in stylesheet  # Border color
    
    def test_create_action_group_llm_style(self, base_tab):
        """Test creating action group with llm style."""
        group = base_tab.create_action_group("LLM Actions", "llm")
        
        assert isinstance(group, QGroupBox)
        assert group.title() == "LLM Actions"
        
        # Verify light purple styling is applied
        stylesheet = group.styleSheet()
        assert "#f0e8f8" in stylesheet  # Light purple background
        assert "#d8c8e8" in stylesheet  # Border color
    
    def test_create_action_group_default_style(self, base_tab):
        """Test creating action group with unknown style defaults correctly."""
        group = base_tab.create_action_group("Default Group", "unknown")
        
        assert isinstance(group, QGroupBox)
        stylesheet = group.styleSheet()
        assert "#f5f5f5" in stylesheet  # Default background
        assert "#cccccc" in stylesheet  # Default border
    
    def test_create_task_button_basic(self, base_tab):
        """Test creating basic task button."""
        callback_called = []
        
        def callback():
            callback_called.append(True)
        
        button = base_tab.create_task_button(
            task=LLMTask.REVIEW_JSON,
            callback=callback
        )
        
        assert isinstance(button, QPushButton)
        assert button.isEnabled()
        
        # Check that metadata is stored
        assert button.property("llm_task") == LLMTask.REVIEW_JSON
        assert button.property("force_mode") is False
        assert button.property("tab_name") == "base"
        
        # Verify callback works
        button.click()
        assert callback_called == [True]
    
    def test_create_task_button_with_force_mode(self, base_tab):
        """Test creating task button with force mode."""
        button = base_tab.create_task_button(
            task=LLMTask.DERIVE_JSON_FROM_TEXT,
            callback=lambda: None,
            force_mode=True
        )
        
        assert button.property("llm_task") == LLMTask.DERIVE_JSON_FROM_TEXT
        assert button.property("force_mode") is True
        
        # Tooltip should mention force mode
        assert "Force Mode" in button.toolTip()
    
    def test_create_task_button_disabled(self, base_tab):
        """Test creating disabled task button."""
        button = base_tab.create_task_button(
            task=LLMTask.REVIEW_CODE,
            callback=lambda: None,
            enabled=False
        )
        
        assert not button.isEnabled()
    
    def test_create_task_button_custom_tooltip(self, base_tab):
        """Test creating task button with custom tooltip."""
        custom_tooltip = "This is a custom tooltip"
        button = base_tab.create_task_button(
            task=LLMTask.GENERATE_CODE_FROM_JSON,
            callback=lambda: None,
            tooltip=custom_tooltip
        )
        
        assert button.toolTip() == custom_tooltip
    
    def test_create_task_button_custom_tab_name(self, base_tab):
        """Test creating task button with custom tab name."""
        button = base_tab.create_task_button(
            task=LLMTask.AD_HOC_CHAT,
            callback=lambda: None,
            tab_name="custom_tab"
        )
        
        assert button.property("tab_name") == "custom_tab"
    
    def test_create_task_button_uses_task_config_manager(self, base_tab, mock_main_window):
        """Test that create_task_button uses TaskConfigManager for labels."""
        # Add the task to base tab if not exists (since "base" is not in defaults)
        if not mock_main_window.task_config_manager.get_task_config("base", LLMTask.REVIEW_JSON.value):
            from workflow_editor.core.task_config import TaskConfig
            task = TaskConfig(
                id=LLMTask.REVIEW_JSON.value,
                name="Review JSON",
                button_label="Review JSON",
                prompt_template=None,
                enabled=True
            )
            mock_main_window.task_config_manager.add_task("base", task)
        
        # Set a custom label
        mock_main_window.task_config_manager.update_task_config(
            tab_id="base",
            task_id=LLMTask.REVIEW_JSON.value,
            button_label="Custom Review Label"
        )
        
        button = base_tab.create_task_button(
            task=LLMTask.REVIEW_JSON,
            callback=lambda: None
        )
        
        assert button.text() == "Custom Review Label"
    
    def test_refresh_button_labels_updates_existing_buttons(self, base_tab, mock_main_window):
        """Test that refresh_button_labels updates all task buttons."""
        # Add the tasks to base tab if not exists (since "base" is not in defaults)
        from workflow_editor.core.task_config import TaskConfig
        
        if not mock_main_window.task_config_manager.get_task_config("base", LLMTask.REVIEW_JSON.value):
            task1 = TaskConfig(
                id=LLMTask.REVIEW_JSON.value,
                name="Review JSON",
                button_label="Review JSON",
                prompt_template=None,
                enabled=True
            )
            mock_main_window.task_config_manager.add_task("base", task1)
        
        if not mock_main_window.task_config_manager.get_task_config("base", LLMTask.REVIEW_CODE.value):
            task2 = TaskConfig(
                id=LLMTask.REVIEW_CODE.value,
                name="Review Code",
                button_label="Review Code",
                prompt_template=None,
                enabled=True
            )
            mock_main_window.task_config_manager.add_task("base", task2)
        
        # Create several task buttons and add them to the tab's layout
        button1 = base_tab.create_task_button(
            task=LLMTask.REVIEW_JSON,
            callback=lambda: None
        )
        button2 = base_tab.create_task_button(
            task=LLMTask.REVIEW_CODE,
            callback=lambda: None
        )
        
        # Create layout and add buttons to the tab so findChildren works
        layout = QVBoxLayout()
        layout.addWidget(button1)
        layout.addWidget(button2)
        base_tab.setLayout(layout)
        
        # Record original labels
        original_label1 = button1.text()
        original_label2 = button2.text()
        
        # Change labels in manager
        mock_main_window.task_config_manager.update_task_config(
            tab_id="base",
            task_id=LLMTask.REVIEW_JSON.value,
            button_label="New JSON Label"
        )
        mock_main_window.task_config_manager.update_task_config(
            tab_id="base",
            task_id=LLMTask.REVIEW_CODE.value,
            button_label="New Code Label"
        )
        
        # Refresh
        base_tab.refresh_button_labels()
        
        # Verify buttons were updated
        assert button1.text() == "New JSON Label"
        assert button2.text() == "New Code Label"
        assert button1.text() != original_label1
        assert button2.text() != original_label2
    
    def test_refresh_button_labels_ignores_non_task_buttons(self, base_tab):
        """Test that refresh_button_labels doesn't affect regular buttons."""
        # Create regular button (not a task button)
        regular_button = base_tab.create_button("Regular Button")
        original_text = regular_button.text()
        
        # Refresh should not affect it
        base_tab.refresh_button_labels()
        
        assert regular_button.text() == original_text
    
    def test_refresh_button_labels_handles_empty_tab(self, base_tab):
        """Test that refresh_button_labels works on tab with no buttons."""
        # Should not raise exception
        base_tab.refresh_button_labels()
    
    def test_task_config_manager_property(self, base_tab, mock_main_window):
        """Test task_config_manager property accessor."""
        assert base_tab.task_config_manager is mock_main_window.task_config_manager


@pytest.fixture
def base_tab(mock_main_window, qapp):
    """Create a BaseTab instance for testing."""
    tab = BaseTab(mock_main_window)
    return tab


@pytest.fixture
def qapp():
    """Create QApplication instance if not already created."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def mock_main_window(tmp_path):
    """Create a mock MainWindow with TaskConfigManager."""
    from unittest.mock import MagicMock
    
    main_window = MagicMock()
    
    # Create a real TaskConfigManager with temp config
    config_path = tmp_path / "test_config.json"
    main_window.task_config_manager = TaskConfigManager(config_path)
    
    return main_window
