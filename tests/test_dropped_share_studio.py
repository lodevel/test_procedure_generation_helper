"""A project can live on a share. When it drops, Python 3.13 raises ``OSError [WinError 64]``
from ``Path.exists()/is_file()/is_dir()/iterdir()``; a Studio GUI-thread entry reads that as
"not there" (display) or stops (a write / delete / overwrite / refuse guard), never dies of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from procedo.studio.core import test_folder_ops, test_sync
from procedo.studio.core.artifact_manager import Artifact, ArtifactManager, ArtifactType
from procedo.studio.core.project_manager import ProjectManager, TestFolderInfo

SHARE = Path("//gone/share/project")


def _share_dropped(*_args, **_kwargs):
    raise OSError(64, "The specified network name is no longer available")


@pytest.fixture
def dropped_share(monkeypatch):
    for name in ("exists", "is_file", "is_dir", "iterdir", "glob"):
        monkeypatch.setattr(Path, name, _share_dropped)


def test_a_project_manager_of_a_dropped_share_reads_everything_as_absent(dropped_share):
    pm = ProjectManager()
    pm.project_root = SHARE
    assert not pm.is_valid_project_root(SHARE)
    assert pm.get_tests_dir() is None and pm.get_config_dir() is None
    assert pm.detect_project_from_test_folder(SHARE / "tests" / "t") is None
    assert not TestFolderInfo.of(SHARE / "tests" / "t").has_json


def test_the_test_list_of_a_share_that_drops_after_the_folder_answered_is_empty(monkeypatch):
    pm = ProjectManager()
    pm.project_root = SHARE
    monkeypatch.setattr(ProjectManager, "get_tests_dir", lambda self: SHARE / "tests")
    monkeypatch.setattr(Path, "iterdir", _share_dropped)
    assert pm.enumerate_test_folders() == []


def test_a_new_test_folder_is_not_created_on_a_dropped_share(dropped_share):
    pm = ProjectManager()
    pm.project_root = SHARE
    pm.get_tests_dir = lambda: SHARE / "tests"
    assert pm.create_test_folder("t") is None


def test_an_unreadable_artifact_keeps_its_content_instead_of_reading_as_deleted(dropped_share):
    manager = ArtifactManager()
    artifact = Artifact(ArtifactType.PROCEDURE_JSON, content="kept", original_content="kept", file_path=SHARE / "p.json")
    manager._load_artifact(artifact)
    assert artifact.content == "kept"


@pytest.mark.parametrize("action", ["delete", "rename"])
def test_deleting_or_renaming_a_test_of_a_dropped_share_is_refused(dropped_share, action):
    tests = SHARE / "tests"
    with pytest.raises(test_folder_ops.TestFolderError, match="cannot be reached"):
        if action == "delete":
            test_folder_ops.delete_test(tests, tests / "t")
        else:
            test_folder_ops.rename_test(SHARE, tests, tests / "t", "u", [])


def test_the_legacy_tab_contexts_migration_does_nothing_on_a_dropped_share(dropped_share):
    from procedo.studio.core import task_config_migrations

    task_config_migrations.migrate_project_tab_contexts(SHARE)  # raises nothing, writes nothing


def test_opening_a_test_on_a_dropped_share_blanks_and_writes_nothing(dropped_share, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    from procedo.studio.controllers import document_controller

    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda _p, _t, text: shown.append(text)))
    saved: list[int] = []
    previous = SimpleNamespace(_file_path=SHARE / "old" / ".llm_session.json", save=lambda: saved.append(1))
    mw = SimpleNamespace(artifact_manager="kept", session_state=previous)
    fake = SimpleNamespace(_mw=mw)
    document_controller.DocumentController.on_test_opened(fake, SHARE / "tests" / "t")
    assert "cannot be reached" in shown[0]
    assert mw.artifact_manager == "kept" and mw.session_state is previous and saved == []


def test_a_session_on_a_dropped_share_is_not_read_as_no_session(dropped_share):
    from procedo.studio.core.session_state import SessionState

    state = SessionState()
    state.set_file_path(SHARE / "t")
    with pytest.raises(OSError):
        state.load()


def test_a_sync_baseline_is_not_written_from_a_dropped_share(dropped_share, monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(test_sync, "write_sync_baseline", lambda _d, hashes: written.append(hashes))
    with pytest.raises(OSError):
        test_sync.mark_in_sync(SHARE / "tests" / "t", [])
    assert written == []


def test_a_failed_delete_on_a_dropped_share_does_not_report_the_test_gone(dropped_share, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    from procedo.studio.tabs import workspace_tab

    emitted: list[Path] = []
    refreshed: list[int] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *_a: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *_a: None))

    def _fail(*_a):
        raise test_folder_ops.TestFolderError("busy")

    monkeypatch.setattr(workspace_tab.test_folder_ops, "delete_test", _fail)
    answers = iter([True])  # the guard before the confirm; then the share drops
    monkeypatch.setattr(Path, "exists", lambda self: next(answers, None) or _share_dropped())
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    path = SHARE / "tests" / "t"
    fake = SimpleNamespace(
        project_manager=SimpleNamespace(get_tests_dir=lambda: SHARE / "tests"),
        test_deleted=SimpleNamespace(emit=emitted.append), refresh=lambda: refreshed.append(1),
    )
    workspace_tab.WorkspaceTab._delete_test(fake, path)
    assert emitted == [] and refreshed == []
