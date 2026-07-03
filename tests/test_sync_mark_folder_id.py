"""Regression: folder-id enforcement at save must not flip a user-acknowledged sync.

Bug report (2026-07): a test whose ``# <id>`` header / JSON ``"id"``
differed from the folder name was marked IN SYNC by the user; hitting
Save then rewrote the id (``ArtifactManager._enforce_folder_id``, task
#30) and the pair immediately showed NOT in sync. The id correction is
not a real divergence — the acknowledged state must survive it.

Root cause: the mark-in-sync flow (main_window ``_on_sync_indicator_clicked``
/ ``_check_artifact_coherence``) stores ``compute_hashes()`` of the raw
buffer content BEFORE save applies the id rewrite, so the post-save
hash no longer matches the acknowledged baseline.

Fix under test:
- ``compute_hashes`` / ``check_external_changes`` pass content through
  ``_enforce_folder_id`` first, so hashes always reflect what
  ``save_artifact`` will actually write (id-invariant wrt the folder).
- Tab save handlers push the save-time-corrected content back into the
  visible editor via ``_refresh_editor_after_save`` (signal-blocked,
  cursor-preserving), so disk == buffer == recorded hash.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock

from workflow_editor.core.artifact_manager import ArtifactManager, ArtifactType


WRONG_TEXT = "# WRONG_ID\n\n## Steps\n1. do x\n"
WRONG_JSON = json.dumps({"id": "WRONG_ID", "steps": []}, indent=2, ensure_ascii=False)


def _manager(tmp_path, folder="Foo"):
    test_dir = tmp_path / folder
    test_dir.mkdir()
    am = ArtifactManager()
    am.set_test_dir(test_dir)
    return am


def test_mark_in_sync_then_save_stays_in_sync(tmp_path):
    """compute_hashes baseline captured pre-save must equal post-save hashes."""
    am = _manager(tmp_path)
    am.set_content(ArtifactType.PROCEDURE_TEXT, WRONG_TEXT)
    am.set_content(ArtifactType.PROCEDURE_JSON, WRONG_JSON)

    stored = am.compute_hashes()  # user clicks "Mark in sync"

    am.save_artifact(ArtifactType.PROCEDURE_TEXT)
    am.save_artifact(ArtifactType.PROCEDURE_JSON)

    # Enforcement really happened (do NOT weaken it) ...
    assert am.procedure_text.content.splitlines()[0] == "# Foo"
    assert json.loads(am.procedure_json.content)["id"] == "Foo"
    # ... and the acknowledged baseline still matches.
    assert am.compute_hashes() == stored, (
        "id == folder correction at save flipped the acknowledged sync state"
    )


def test_check_external_changes_ignores_id_only_drift(tmp_path):
    """A wrong id on disk is rewritten at the next save — not an external edit."""
    am = _manager(tmp_path)
    am.set_content(ArtifactType.PROCEDURE_TEXT, WRONG_TEXT)
    stored = am.compute_hashes()

    # Disk holds the un-enforced content (e.g. marked in sync, not saved yet).
    am.procedure_text.file_path.write_text(WRONG_TEXT, encoding="utf-8")
    assert am.check_external_changes(stored) == []

    # A genuine content edit must still be reported.
    am.procedure_text.file_path.write_text(
        WRONG_TEXT + "2. do y\n", encoding="utf-8"
    )
    assert am.check_external_changes(stored) == ["procedure_text.md"]


def test_correct_id_hashes_unchanged_by_enforcement(tmp_path):
    """Back-compat: stored baselines of correct-id tests must stay valid.

    For content whose id already equals the folder, enforcement is the
    identity, so the hash equals the raw-content hash recorded by
    pre-fix sessions (and by workspace_tab's raw disk hasher).
    """
    am = _manager(tmp_path)
    text = "# Foo\n\n## Steps\n1. do x\n"
    js = json.dumps({"id": "Foo", "steps": []}, indent=2, ensure_ascii=False)
    am.set_content(ArtifactType.PROCEDURE_TEXT, text)
    am.set_content(ArtifactType.PROCEDURE_JSON, js)

    hashes = am.compute_hashes()
    assert hashes["procedure_text.md"] == ArtifactManager._hash_content(text)
    assert hashes["procedure.json"] == ArtifactManager._hash_content(js)


def test_real_divergence_with_wrong_id_stays_out_of_sync(tmp_path):
    """Enforcement must PRESERVE the sync state, never force in-sync.

    A genuinely edited buffer (extra step) that also carries a wrong id
    was out of sync before save and must still be out of sync after —
    the id fix only neutralizes id-only drift.
    """
    am = _manager(tmp_path)
    am.set_content(ArtifactType.PROCEDURE_TEXT, "# Foo\n\n## Steps\n1. do x\n")
    stored = am.compute_hashes()  # acknowledged baseline

    am.set_content(
        ArtifactType.PROCEDURE_TEXT, "# WRONG_ID\n\n## Steps\n1. do x\n2. do y\n"
    )
    am.save_artifact(ArtifactType.PROCEDURE_TEXT)

    assert am.compute_hashes() != stored, (
        "a real content edit must remain a divergence after id enforcement"
    )
    assert am.check_external_changes(stored) == ["procedure_text.md"]


def test_no_test_dir_keeps_raw_hashes():
    """Without a test_dir the enforcement is a no-op (legacy behavior)."""
    am = ArtifactManager()
    am.set_content(ArtifactType.PROCEDURE_TEXT, WRONG_TEXT)
    hashes = am.compute_hashes()
    assert hashes["procedure_text.md"] == ArtifactManager._hash_content(WRONG_TEXT)


# ---- buffer refresh (tabs) ----


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_refresh_editor_after_save_updates_buffer_without_signals(qapp):
    from PySide6.QtWidgets import QPlainTextEdit
    from workflow_editor.tabs.text_only_tab import _refresh_editor_after_save

    editor = QPlainTextEdit()
    editor.setPlainText(WRONG_TEXT)
    cursor = editor.textCursor()
    cursor.setPosition(4)
    editor.setTextCursor(cursor)

    fired = []
    editor.textChanged.connect(lambda: fired.append(True))

    corrected = "# Foo\n\n## Steps\n1. do x\n"
    _refresh_editor_after_save(editor, corrected)

    assert editor.toPlainText() == corrected
    assert fired == [], "textChanged must not fire — it would re-dirty the artifact"
    assert editor.textCursor().position() == 4, "caret must be preserved"


def test_refresh_editor_after_save_clamps_cursor(qapp):
    from PySide6.QtWidgets import QPlainTextEdit
    from workflow_editor.tabs.text_only_tab import _refresh_editor_after_save

    editor = QPlainTextEdit()
    editor.setPlainText("# WRONG_ID_MUCH_LONGER_THAN_REPLACEMENT")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    editor.setTextCursor(cursor)

    _refresh_editor_after_save(editor, "# Foo")
    assert editor.toPlainText() == "# Foo"
    assert editor.textCursor().position() == len("# Foo")


def test_refresh_editor_after_save_noop_when_verbatim(qapp):
    from PySide6.QtWidgets import QPlainTextEdit
    from workflow_editor.tabs.text_only_tab import _refresh_editor_after_save

    editor = QPlainTextEdit()
    editor.setPlainText("# Foo\nbody\n")
    fired = []
    editor.textChanged.connect(lambda: fired.append(True))
    _refresh_editor_after_save(editor, "# Foo\nbody\n")
    assert fired == []


def test_save_handler_reflects_corrected_id_into_editor(qapp, tmp_path):
    """TextOnlyTab._on_save_text: after save the QTextEdit shows the folder id."""
    from PySide6.QtWidgets import QPlainTextEdit
    from workflow_editor.tabs.text_only_tab import TextOnlyTab

    fake = MagicMock()
    editor = QPlainTextEdit()
    editor.setPlainText(WRONG_TEXT)
    fake.text_editor = editor
    fake.artifact_manager = _manager(tmp_path)

    TextOnlyTab._on_save_text(fake)

    assert editor.toPlainText().splitlines()[0] == "# Foo", (
        "screen must show the enforced id after save, not the stale buffer"
    )
    on_disk = fake.artifact_manager.procedure_text.file_path.read_text(encoding="utf-8")
    assert on_disk == editor.toPlainText() == fake.artifact_manager.procedure_text.content
    assert not fake.artifact_manager.procedure_text.is_dirty


# ---- unloaded path (workspace Tests list context menu) ----
#
# Residual of the same bug: workspace_tab hashed RAW disk content, so
# "Mark Procedure In Sync" on an UNLOADED wrong-id test stored a raw
# baseline that mismatched the enforced hash on the next open. Both
# workspace hashers now route through artifact_manager.enforced_sync_hash.


WRONG_CODE = "print('x')\n"


def _write_wrong_id_test(tmp_path, folder="Foo"):
    test_dir = tmp_path / folder
    test_dir.mkdir()
    (test_dir / "procedure_text.md").write_text(WRONG_TEXT, encoding="utf-8")
    (test_dir / "procedure.json").write_text(WRONG_JSON, encoding="utf-8")
    (test_dir / "test.py").write_text(WRONG_CODE, encoding="utf-8")
    return test_dir


def _workspace_fake():
    fake = MagicMock()
    fake.project_manager.get_equipment_patterns.return_value = []
    return fake


def test_mark_unloaded_wrong_id_test_matches_enforced_baseline(tmp_path):
    """Marking an UNLOADED wrong-id test in sync must store the ENFORCED
    hashes — byte-identical to the loaded path — so opening the test
    later reports no mismatch."""
    from workflow_editor.tabs.workspace_tab import WorkspaceTab

    test_dir = _write_wrong_id_test(tmp_path)
    fake = _workspace_fake()

    WorkspaceTab._mark_test_in_sync(fake, test_dir)

    session = json.loads(
        (test_dir / ".llm_session.json").read_text(encoding="utf-8")
    )
    assert session["artifacts_in_sync"] is True
    # Enforcement really happened: the stored text hash is NOT the raw hash.
    assert session["artifact_hashes"]["procedure_text.md"] != (
        ArtifactManager._hash_content(WRONG_TEXT)
    ), "workspace helper stored a raw-content hash (enforcement skipped)"

    # The loaded/enforced path produces the exact same baseline ...
    am = ArtifactManager()
    am.set_test_dir(test_dir)
    am.load_all()
    assert am.compute_hashes() == session["artifact_hashes"], (
        "unloaded mark-in-sync baseline diverges from the loaded path"
    )
    # ... and no mismatch is reported on open.
    assert am.check_external_changes(session["artifact_hashes"]) == []
    # The Tests list agrees with its own baseline too.
    assert WorkspaceTab._is_test_out_of_sync(fake, test_dir) is False


def test_workspace_out_of_sync_check_ignores_id_only_drift(tmp_path):
    """_is_test_out_of_sync must not flag a wrong-id disk file against an
    enforced baseline (stored by the loaded path) — id-only drift is
    rewritten at the next save, not an external edit."""
    from workflow_editor.tabs.workspace_tab import WorkspaceTab

    test_dir = _write_wrong_id_test(tmp_path)
    am = ArtifactManager()
    am.set_test_dir(test_dir)
    am.load_all()
    (test_dir / ".llm_session.json").write_text(
        json.dumps(
            {"artifacts_in_sync": True, "artifact_hashes": am.compute_hashes()}
        ),
        encoding="utf-8",
    )

    fake = _workspace_fake()
    assert WorkspaceTab._is_test_out_of_sync(fake, test_dir) is False, (
        "id-only drift flagged as out-of-sync by the Tests list"
    )

    # A genuine content edit must still flip the flag.
    (test_dir / "procedure_text.md").write_text(
        WRONG_TEXT + "2. do y\n", encoding="utf-8"
    )
    assert WorkspaceTab._is_test_out_of_sync(fake, test_dir) is True
