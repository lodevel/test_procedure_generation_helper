"""The views' editor cards (redesign slice 4): every keystroke reaches the
``artifact_manager``, the Saved / ● Modified pill and the card's own Save
follow the artifact, and Save writes that one artifact.

Bug report (2026-05-11): user typed text in the text tab, hit Review,
got "No Text" warning even though the editor had content — the edit had
not reached ``artifact_manager``. Every card mirrors its editor into its
artifact on each edit, so Reviews / Validates / token counters and the
header's pills see the live state without needing a Save first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from procedo.studio.core import ArtifactType
from procedo.studio.core.artifact_manager import ArtifactManager
from procedo.studio.widgets.editor_card import EditorCard

FILES = {
    ArtifactType.PROCEDURE_TEXT: "procedure_text.md",
    ArtifactType.PROCEDURE_JSON: "procedure.json",
    ArtifactType.TEST_CODE: "test.py",
}


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _card(kind: ArtifactType, am: ArtifactManager) -> tuple[EditorCard, MagicMock]:
    tab = MagicMock()
    tab.artifact_manager = am
    return EditorCard(tab, kind, FILES[kind]), tab


@pytest.mark.parametrize("kind", list(FILES))
def test_keystrokes_reach_the_artifact_manager(qapp, kind):
    am = ArtifactManager()
    card, _tab = _card(kind, am)
    changed = []
    card.changed.connect(lambda: changed.append(True))
    card.editor.setPlainText("operator just typed this")
    assert am.get_content(kind) == "operator just typed this", (
        "Live editor content was not propagated to artifact_manager — "
        "the Review/Validate empty-checks will see stale content."
    )
    assert changed


def test_the_pill_and_save_follow_the_artifact(qapp, tmp_path):
    test_dir = tmp_path / "t1"
    test_dir.mkdir()
    (test_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
    am = ArtifactManager()
    am.set_test_dir(test_dir)
    am.load_all()
    card, _tab = _card(ArtifactType.TEST_CODE, am)
    card.load()
    assert card.pill.text() == "✓ Saved" and card.pill.property("pill") == "success"
    assert not card.save_button.isEnabled()

    card.editor.appendPlainText("y = 2")
    assert card.pill.text() == "● Modified" and card.pill.property("pill") == "warning"
    assert card.save_button.isEnabled()

    card.set_text("x = 1\n")  # back to what is on disk: nothing to save
    assert card.pill.text() == "✓ Saved" and not card.save_button.isEnabled()


def test_the_cards_save_writes_its_artifact(qapp, tmp_path):
    test_dir = tmp_path / "t1"
    test_dir.mkdir()
    am = ArtifactManager()
    am.set_test_dir(test_dir)
    am.load_all()
    card, tab = _card(ArtifactType.TEST_CODE, am)
    saved = []
    card.saved.connect(lambda: saved.append(True))
    card.editor.setPlainText("def run(ctx):\n    pass\n")

    card.save_button.click()

    assert (test_dir / "test.py").read_text(encoding="utf-8") == "def run(ctx):\n    pass\n"
    assert not am.is_dirty(ArtifactType.TEST_CODE)
    assert not (test_dir / "procedure.json").exists()  # its own artifact only
    assert card.pill.text() == "✓ Saved" and saved == [True]
    tab.artifact_saved.emit.assert_called_once_with()
