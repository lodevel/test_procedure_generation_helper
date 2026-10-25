"""
Test BaseTab button label integration.

Verifies that BaseTab's assistant buttons (its ``task_buttons`` row) work correctly:
- create_button() (a split button when the task has a force variant)
- the action bar's assistant row (_create_action_bar)
"""

import pytest
from pathlib import Path
from PySide6.QtWidgets import QApplication, QToolButton

from procedo.studio.tabs.base_tab import BaseTab
from procedo.studio.llm.backend_base import LLMTask
from procedo.studio.core.task_config import TaskConfigManager


class TestBaseTabButtonLabels:
    """Test BaseTab button label methods."""

    def test_create_task_button_basic(self, row):
        """Test creating basic task button."""
        callback_called = []

        def callback():
            callback_called.append(True)

        button = row.create_button(task=LLMTask.REVIEW_JSON, callback=callback)

        assert isinstance(button, QToolButton)
        assert button.isEnabled()
        assert button.menu() is None  # no force variant: a plain button

        # Verify callback works
        button.click()
        assert callback_called == [True]

    def test_force_variant_is_the_split_buttons_menu(self, row):
        """A task with a force variant is ONE split button (owner decision): a
        click runs the task, its ▾ menu offers the task and its force variant,
        and the force entry runs the force callback."""
        calls = []
        button = row.create_button(
            task=LLMTask.DERIVE_JSON_FROM_TEXT,
            callback=lambda: calls.append("strict"),
            force_callback=lambda: calls.append("force"),
            force_tooltip="Force generate JSON",
        )

        assert button.popupMode() == QToolButton.ToolButtonPopupMode.MenuButtonPopup
        strict, force = button.menu().actions()
        assert force.text().startswith(strict.text() + " — force")
        assert force.toolTip() == "Force generate JSON"

        button.click()
        force.trigger()
        strict.trigger()
        assert calls == ["strict", "force", "strict"]

    def test_create_task_button_disabled(self, row):
        """Test creating disabled task button."""
        button = row.create_button(task=LLMTask.REVIEW_CODE, callback=lambda: None, enabled=False)

        assert not button.isEnabled()

    def test_create_task_button_custom_tooltip(self, row):
        """Test creating task button with custom tooltip."""
        custom_tooltip = "This is a custom tooltip"
        button = row.create_button(
            task=LLMTask.GENERATE_CODE_FROM_JSON, callback=lambda: None, tooltip=custom_tooltip
        )

        assert button.toolTip() == custom_tooltip

    def test_create_task_button_custom_tab_name(self, row):
        """A custom tab name is the label lookup's: a tab without the task falls back."""
        button = row.create_button(
            task=LLMTask.AD_HOC_CHAT, callback=lambda: None, tab_name="custom_tab"
        )

        assert button.text() == "✦ " + LLMTask.AD_HOC_CHAT.value.replace("_", " ").title()

    def test_create_task_button_uses_task_config_manager(self, row, mock_main_window):
        """Test that create_button uses TaskConfigManager for labels."""
        # Add the task to base tab if not exists (since "base" is not in defaults)
        if not mock_main_window.task_config_manager.get_task_config(
            "base", LLMTask.REVIEW_JSON.value
        ):
            from procedo.studio.core.task_config_model import TaskConfig

            task = TaskConfig(
                id=LLMTask.REVIEW_JSON.value,
                name="Review JSON",
                button_label="Review JSON",
                prompt_template=None,
                enabled=True,
            )
            mock_main_window.task_config_manager.add_task("base", task)

        # Set a custom label
        mock_main_window.task_config_manager.update_task_config(
            tab_id="base", task_id=LLMTask.REVIEW_JSON.value, button_label="Custom Review Label"
        )

        button = row.create_button(task=LLMTask.REVIEW_JSON, callback=lambda: None)

        assert button.text() == "✦ Custom Review Label"

    def test_task_config_manager_property(self, base_tab, mock_main_window):
        """Test task_config_manager property accessor."""
        assert base_tab.task_config_manager is mock_main_window.task_config_manager


class TestLLMButtonCapabilityGating:
    """Task #22: a task button is enabled only when the effective
    bundle/project config declares a non-empty prompt_template.
    Bare-editor mode (editor defaults only) -> all task buttons greyed
    with a discoverable tooltip; chat is unaffected (no button)."""

    @staticmethod
    def _collect_task_buttons(tab):
        """The action bar's assistant row's current task buttons (layout
        traversal, not findChildren — deleteLater'd buttons from a rebuild
        would still show up as children)."""
        layout = tab.action_bar.assistant_layout
        widgets = (layout.itemAt(i).widget() for i in range(layout.count()))
        return [w for w in widgets if isinstance(w, QToolButton)]

    @classmethod
    def _by_id(cls, tab):
        """The task buttons by task id: one per enabled task but the chat, in order."""
        ids = [
            t.id
            for t in tab.task_config_manager.get_enabled_tasks_for_tab(tab.tab_id)
            if t.id != LLMTask.AD_HOC_CHAT.value
        ]
        buttons = cls._collect_task_buttons(tab)
        assert len(buttons) == len(ids)
        return dict(zip(ids, buttons))

    def test_bare_editor_all_task_buttons_greyed(self, base_tab, mock_main_window):
        base_tab.tab_id = "text_json"
        base_tab._create_action_bar()

        by_id = self._by_id(base_tab)
        assert len(by_id) > 0
        for task_id, btn in by_id.items():
            assert not btn.isEnabled(), f"{task_id} must be greyed without a bundle"
            assert btn.toolTip() == "Not provided by this bundle"

    def test_declared_task_button_enabled_after_rebuild(self, base_tab, mock_main_window):
        base_tab.tab_id = "text_json"
        base_tab._create_action_bar()

        declared_id = LLMTask.DERIVE_JSON_FROM_TEXT.value
        mock_main_window.task_config_manager.update_task_config(
            tab_id="text_json",
            task_id=declared_id,
            prompt_template="Derive the JSON per the bundle rules.",
        )
        base_tab.task_buttons.rebuild()

        by_id = self._by_id(base_tab)
        assert len(by_id) > 0
        assert by_id[declared_id].isEnabled()
        assert by_id[declared_id].toolTip() != "Not provided by this bundle"
        for task_id, btn in by_id.items():
            if task_id != declared_id:
                assert not btn.isEnabled(), f"{task_id} must stay greyed"

    def test_force_variant_tasks_are_split_buttons(self, base_tab, mock_main_window):
        """Only the tasks the view maps to a force callback get the ▾ menu."""
        base_tab.tab_id = "text_json"
        derive = LLMTask.DERIVE_JSON_FROM_TEXT.value
        base_tab._get_force_callback_map = lambda: {derive: (lambda: None, "force it")}
        base_tab._create_action_bar()

        by_id = self._by_id(base_tab)
        assert by_id[derive].menu() is not None
        assert all(b.menu() is None for task_id, b in by_id.items() if task_id != derive)

    def test_no_ad_hoc_chat_button(self, base_tab, mock_main_window):
        from procedo.studio.core.task_config_model import TaskConfig

        base_tab.tab_id = "json_code"
        mock_main_window.task_config_manager.add_task(
            "json_code",
            TaskConfig(
                id=LLMTask.AD_HOC_CHAT.value, name="Chat", button_label="Chat", enabled=True
            ),
        )
        base_tab._create_action_bar()
        enabled = [
            t.id for t in base_tab.task_config_manager.get_enabled_tasks_for_tab("json_code")
        ]
        assert LLMTask.AD_HOC_CHAT.value in enabled
        assert len(self._collect_task_buttons(base_tab)) == len(enabled) - 1

    def test_declared_custom_task_click_threads_custom_task_id(self, base_tab, mock_main_window):
        """A bundle-declared CUSTOM task's button is enabled and clicking
        runs AD_HOC_CHAT with custom_task_id (gpt-5.5 finding)."""
        from unittest.mock import MagicMock
        from procedo.studio.core.task_config_model import TaskConfig

        base_tab.tab_id = "text_json"
        mock_main_window.task_config_manager.add_task(
            "text_json",
            TaskConfig(
                id="verify_shunt",
                name="Verify Shunt",
                button_label="Verify Shunt",
                prompt_template="Verify the shunt wiring.",
            ),
        )
        base_tab._run_task_async = MagicMock()
        base_tab._create_action_bar()

        btn = self._by_id(base_tab)["verify_shunt"]
        assert btn.isEnabled()
        assert btn.toolTip() != "Not provided by this bundle"

        btn.click()
        base_tab._run_task_async.assert_called_once_with(
            LLMTask.AD_HOC_CHAT, custom_task_id="verify_shunt"
        )

    def test_generic_callback_guards_undeclared_custom_task(self, base_tab, mock_main_window):
        """Defense in depth: the generic callback re-checks invocability,
        so a programmatic call with an undeclared id never dispatches."""
        from unittest.mock import MagicMock

        base_tab.tab_id = "text_json"
        base_tab._run_task_async = MagicMock()
        base_tab._create_action_bar()
        callback = base_tab.task_buttons._generic_callback("undeclared_custom")
        callback()
        base_tab._run_task_async.assert_not_called()


@pytest.fixture
def base_tab(mock_main_window, qapp):
    """Create a BaseTab instance for testing."""
    tab = BaseTab(mock_main_window)
    return tab


@pytest.fixture
def row(base_tab):
    """The tab's task button row (its action bar built)."""
    base_tab._create_action_bar()
    return base_tab.task_buttons


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
