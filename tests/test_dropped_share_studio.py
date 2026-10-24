"""A project can live on a share. When it drops, Python 3.13 raises ``OSError [WinError 64]``
from ``Path.exists()/is_file()/is_dir()/iterdir()``; a Studio GUI-thread entry reads that as
"not there" (display) or stops (a write / delete / overwrite / refuse guard), never dies of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from procedo.studio.core import test_folder_ops
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
