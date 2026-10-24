"""Studio's assistant row and chat list a project's AI tasks the way Project Configuration
does (both from ``workflow_defaults.effective_workflows``): a bundle-declared tab lists the
editor-only tasks too, a tab with no runnable task lists none, and a partial bundle
``chat_config`` inherits no editor system prompt."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QToolButton

from procedo.studio.core.task_config import TaskConfigManager
from procedo.studio.tabs.base_tab import BaseTab


@pytest.fixture
def studio_tab(tmp_path):
    """A view of a project whose bundle declares text_json (one prompt, chat {enabled:false})."""
    QApplication.instance() or QApplication([])
    project = tmp_path / "proj"
    (project / "config").mkdir(parents=True)
    (project / "bundle").mkdir()
    (project / "bundle" / "defaults.json").write_text(json.dumps({"workflows": {"text_json": {
        "tasks": [{"id": "derive_json_from_text", "prompt_template": "BUNDLE derive"}],
        "chat_config": {"enabled": False},
    }}}), encoding="utf-8")
    window = MagicMock()
    window.task_config_manager = TaskConfigManager(tmp_path / "none.json", project_root=project)
    return BaseTab(window)


def _buttons(tab, tab_id):
    tab.tab_id = tab_id
    tab._create_action_bar()
    layout = tab.action_bar.assistant_layout
    widgets = (layout.itemAt(i).widget() for i in range(layout.count()))
    return [w for w in widgets if isinstance(w, QToolButton)]


def test_a_bundle_tab_lists_the_editor_only_tasks_too(studio_tab):
    buttons = _buttons(studio_tab, "text_json")
    assert [b.text().removeprefix("✦ ") for b in buttons] == [
        "Text → JSON", "JSON → Text", "Review Text", "Review JSON", "Check Text↔JSON"]
    assert [b.isEnabled() for b in buttons] == [True, False, False, False, False]


def test_a_tab_with_no_runnable_task_lists_none(studio_tab):
    assert _buttons(studio_tab, "text_only") == []
    assert _buttons(studio_tab, "json_code") == []


def test_a_partial_bundle_chat_config_has_no_system_prompt(studio_tab):
    chat = studio_tab.task_config_manager.get_chat_config("text_json")
    assert (chat.enabled, chat.system_prompt) == (False, None)
