"""Regression: editor keystrokes must propagate into ``artifact_manager``.

Bug report (2026-05-11): user typed text in the text tab, hit Review,
got "No Text" warning even though the editor had content. Root cause:
``_on_text_changed`` only set a tab-level dirty flag — it did NOT push
the editor's live content into ``artifact_manager.procedure_text.content``.
``_on_review_text`` then checked the stale (last-saved) content and
rejected the unsaved edit.

Fix: each ``_on_<artifact>_changed`` handler now mirrors
``editor.toPlainText()`` into the matching artifact via
``artifact_manager.set_content(...)``. Reviews / Validates / token
counters now see the live state without needing a Save first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from workflow_editor.core import ArtifactType
from workflow_editor.core.artifact_manager import ArtifactManager
from workflow_editor.tabs.json_code_tab import JsonCodeTab
from workflow_editor.tabs.text_json_tab import TextJsonTab
from workflow_editor.tabs.text_only_tab import TextOnlyTab


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _stub_self(qapp, editor_attr: str, content: str) -> MagicMock:
    """Build a minimal stand-in for `self` that the handler under test
    will touch. The handler reads ``self.<editor_attr>.toPlainText()``
    and writes through ``self.artifact_manager.set_content(...)``; all
    other attributes are MagicMock-friendly (called freely, return
    Mocks). ArtifactManager is real so we can read back the stored
    content."""
    fake = MagicMock()
    editor = QPlainTextEdit()
    editor.setPlainText(content)
    setattr(fake, editor_attr, editor)
    fake.artifact_manager = ArtifactManager()
    return fake


def test_text_only_keystroke_propagates_to_artifact_manager(qapp):
    """TextOnlyTab._on_text_changed must push text_editor content into
    artifact_manager.procedure_text."""
    fake = _stub_self(qapp, "text_editor", "operator just typed this")
    TextOnlyTab._on_text_changed(fake)
    assert fake.artifact_manager.procedure_text.content == "operator just typed this", (
        "Live editor content was not propagated to artifact_manager — "
        "the Review/Validate empty-checks will see stale content."
    )


def test_text_json_text_keystroke_propagates(qapp):
    fake = _stub_self(qapp, "text_editor", "fresh procedure text")
    TextJsonTab._on_text_changed(fake)
    assert fake.artifact_manager.procedure_text.content == "fresh procedure text"


def test_text_json_json_keystroke_propagates(qapp):
    fake = _stub_self(qapp, "json_editor", '{"id": "DEMO"}')
    TextJsonTab._on_json_changed(fake)
    assert fake.artifact_manager.procedure_json.content == '{"id": "DEMO"}'


def test_json_code_json_keystroke_propagates(qapp):
    fake = _stub_self(qapp, "json_editor", '{"id": "JC"}')
    JsonCodeTab._on_json_changed(fake)
    assert fake.artifact_manager.procedure_json.content == '{"id": "JC"}'


def test_json_code_code_keystroke_propagates(qapp):
    fake = _stub_self(qapp, "code_editor", "def test_x():\n    pass\n")
    JsonCodeTab._on_code_changed(fake)
    assert fake.artifact_manager.test_code.content == "def test_x():\n    pass\n"
