"""core.test_folder_ops — rename and delete a test folder (class A: the user's
work). Every case builds its own project under tmp_path; nothing touches a
real project. Windows-only cases (junctions, locked files, case-insensitive
names) skip elsewhere."""

import json
import os
import sys

import pytest

from workflow_editor.core import test_folder_ops, test_sync
from workflow_editor.core.test_folder_ops import TestFolderError, delete_test, rename_test

WIN = pytest.mark.skipif(sys.platform != "win32", reason="Windows behaviour")


def _project(tmp_path, *names):
    root = tmp_path / "proj"
    tests = root / "tests"
    for name in names:
        d = tests / name
        d.mkdir(parents=True)
        (d / "procedure_text.md").write_text(f"# {name}\n\n## Steps\n1. do x\n", encoding="utf-8")
        (d / "procedure.json").write_text(json.dumps({"id": name, "steps": []}), encoding="utf-8")
        (d / "test.py").write_text("x = 1\n", encoding="utf-8")
    tests.mkdir(parents=True, exist_ok=True)
    return root, tests


def _junction(link, target):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


# -- delete -------------------------------------------------------------------


def test_delete_removes_the_test_folder(tmp_path):
    root, tests = _project(tmp_path, "Foo", "Bar")
    delete_test(tests, tests / "Foo")
    assert sorted(p.name for p in tests.iterdir()) == ["Bar"]


@pytest.mark.parametrize("where", ["outside", "tests_itself", "missing", "nested"])
def test_delete_refuses_anything_but_a_test_folder(tmp_path, where):
    root, tests = _project(tmp_path, "Foo")
    target = {
        "outside": root / "config",
        "tests_itself": tests,
        "missing": tests / "Ghost",
        "nested": tests / "Foo" / "sub",
    }[where]
    if where in ("outside", "nested"):
        target.mkdir()
    with pytest.raises(TestFolderError):
        delete_test(tests, target)
    assert (tests / "Foo" / "test.py").exists()
    if where in ("outside", "nested"):
        assert target.exists()


@WIN
def test_delete_refuses_a_junction_and_leaves_its_target(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep", encoding="utf-8")
    _junction(tests / "Link", precious)
    with pytest.raises(TestFolderError, match="link"):
        delete_test(tests, tests / "Link")
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep"


@WIN
def test_delete_removes_a_junction_inside_without_touching_its_target(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep", encoding="utf-8")
    _junction(tests / "Foo" / "media", precious)
    delete_test(tests, tests / "Foo")
    assert not (tests / "Foo").exists()
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep"


@WIN
def test_delete_works_in_a_tests_folder_that_is_itself_a_junction(tmp_path):
    root, real_tests = _project(tmp_path, "Foo")
    linked = tmp_path / "linked_tests"
    _junction(linked, real_tests)
    delete_test(linked, linked / "Foo")
    assert not (real_tests / "Foo").exists()


@WIN
def test_a_locked_file_stops_the_delete_before_anything_is_removed(tmp_path, monkeypatch):
    from workflow_editor.core import fileio

    monkeypatch.setattr(fileio, "_BUSY_PAUSE_S", 0)
    root, tests = _project(tmp_path, "Foo")
    with open(tests / "Foo" / "test.py", "r", encoding="utf-8"):
        with pytest.raises(TestFolderError, match="nothing was removed"):
            delete_test(tests, tests / "Foo")
    assert sorted(p.name for p in (tests / "Foo").iterdir()) == ["procedure.json", "procedure_text.md", "test.py"]


# -- rename -------------------------------------------------------------------


def test_rename_moves_the_folder_ids_and_references(tmp_path):
    root, tests = _project(tmp_path, "Foo", "Other")
    (root / "config").mkdir()
    (root / "config" / "config.json").write_text(
        json.dumps({"test_order": {"order": ["Other", "Foo"]}, "x": "µ"}), encoding="utf-8")
    (root / "scenarios").mkdir()
    (root / "scenarios" / "s1.json").write_text(json.dumps({"tests": ["Foo", "Other"]}), encoding="utf-8")
    (root / "scenarios" / "s2.json").write_text(json.dumps({"tests": ["Other"]}), encoding="utf-8")

    new_dir, warnings = rename_test(root, tests, tests / "Foo", "Bar", [])
    assert warnings == [] and new_dir == tests / "Bar" and not (tests / "Foo").exists()
    assert (new_dir / "procedure_text.md").read_text(encoding="utf-8").startswith("# Bar\n")
    assert json.loads((new_dir / "procedure.json").read_text(encoding="utf-8"))["id"] == "Bar"
    config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert config == {"test_order": {"order": ["Other", "Bar"]}, "x": "µ"}
    assert json.loads((root / "scenarios" / "s1.json").read_text(encoding="utf-8"))["tests"] == ["Bar", "Other"]
    assert (root / "scenarios" / "s2.json").read_text(encoding="utf-8") == json.dumps({"tests": ["Other"]})
    for written in (root / "config" / "config.json", root / "scenarios" / "s1.json"):  # today's layout
        text = written.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def test_rename_refuses_an_existing_name_and_touches_nothing(tmp_path):
    root, tests = _project(tmp_path, "Foo", "Bar")
    with pytest.raises(TestFolderError, match="A test named 'Bar' already exists"):
        rename_test(root, tests, tests / "Foo", "Bar", [])
    assert sorted(p.name for p in (tests / "Bar").iterdir()) == ["procedure.json", "procedure_text.md", "test.py"]
    assert json.loads((tests / "Foo" / "procedure.json").read_text(encoding="utf-8"))["id"] == "Foo"


@WIN
def test_rename_refuses_a_junction(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    _junction(tests / "Link", tests / "Foo")
    with pytest.raises(TestFolderError, match="link"):
        rename_test(root, tests, tests / "Link", "Bar", [])
    assert json.loads((tests / "Foo" / "procedure.json").read_text(encoding="utf-8"))["id"] == "Foo"


@WIN
def test_a_case_only_rename_works(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    rename_test(root, tests, tests / "Foo", "foo", [])
    assert [p.name for p in tests.iterdir()] == ["foo"]
    assert json.loads((tests / "foo" / "procedure.json").read_text(encoding="utf-8"))["id"] == "foo"


def test_an_in_sync_test_stays_in_sync(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    test_sync.mark_in_sync(tests / "Foo", [])
    new_dir, _ = rename_test(root, tests, tests / "Foo", "Bar", [])
    assert test_sync.is_out_of_sync(new_dir, []) is False


def test_an_out_of_sync_test_stays_out_of_sync(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    (tests / "Foo" / ".llm_session.json").write_text('{"artifacts_in_sync": false}', encoding="utf-8")
    new_dir, _ = rename_test(root, tests, tests / "Foo", "Bar", [])
    assert test_sync.is_out_of_sync(new_dir, []) is True


def test_a_test_without_a_session_gets_none(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    new_dir, _ = rename_test(root, tests, tests / "Foo", "Bar", [])
    assert not (new_dir / ".llm_session.json").exists()


def test_an_unreadable_reference_is_a_warning(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    (root / "config").mkdir()
    (root / "config" / "config.json").write_text("{broken", encoding="utf-8")
    new_dir, warnings = rename_test(root, tests, tests / "Foo", "Bar", [])
    assert new_dir.exists() and len(warnings) == 1 and warnings[0].startswith("config.json test_order")
    assert (root / "config" / "config.json").read_text(encoding="utf-8") == "{broken"


def test_a_file_where_a_test_folder_was_is_refused(tmp_path):
    root, tests = _project(tmp_path)
    (tests / "x").write_text("not a folder", encoding="utf-8")
    with pytest.raises(TestFolderError, match="no longer exists"):
        delete_test(tests, tests / "x")
    assert (tests / "x").read_text(encoding="utf-8") == "not a folder"


def test_a_leftover_from_a_failed_delete_does_not_block_the_next(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    (tests / ".Foo.deleting").mkdir()
    delete_test(tests, tests / "Foo")
    assert sorted(p.name for p in tests.iterdir()) == [".Foo.deleting"]


def test_read_only_files_are_deleted_too(tmp_path):
    import stat

    root, tests = _project(tmp_path, "Foo")
    os.chmod(tests / "Foo" / "test.py", stat.S_IREAD)
    delete_test(tests, tests / "Foo")
    assert list(tests.iterdir()) == []


def test_a_rename_never_acknowledges_an_edit_the_warning_shows(tmp_path):
    """Marked in sync, then test.py edited while the editor was closed: the list
    shows ⚠. Renaming must keep the ⚠, not re-baseline the edit."""
    root, tests = _project(tmp_path, "Foo")
    test_sync.mark_in_sync(tests / "Foo", [])
    (tests / "Foo" / "test.py").write_text("x = 2\n", encoding="utf-8")
    assert test_sync.is_out_of_sync(tests / "Foo", []) is True
    new_dir, _ = rename_test(root, tests, tests / "Foo", "Bar", [])
    assert test_sync.is_out_of_sync(new_dir, []) is True


@WIN
def test_a_failed_folder_rename_touches_nothing(tmp_path):
    root, tests = _project(tmp_path, "Foo")
    (root / "config").mkdir()
    (root / "config" / "config.json").write_text(json.dumps({"test_order": {"order": ["Foo"]}}), encoding="utf-8")
    with open(tests / "Foo" / "test.py", "r", encoding="utf-8"):
        with pytest.raises(TestFolderError, match="Could not rename"):
            rename_test(root, tests, tests / "Foo", "Bar", [])
    assert json.loads((tests / "Foo" / "procedure.json").read_text(encoding="utf-8"))["id"] == "Foo"
    assert json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))["test_order"]["order"] == ["Foo"]


# -- the Tests list drives them --------------------------------------------------


def _list(qapp_tests):
    from unittest.mock import MagicMock

    from workflow_editor.core.project_manager import ProjectManager

    fake = MagicMock()
    pm = ProjectManager()
    pm.set_project_root(qapp_tests)
    fake.project_manager = pm
    return fake


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_list_renames_through_the_core(tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from workflow_editor.tabs.workspace_tab import WorkspaceTab

    root, tests = _project(tmp_path, "Foo")
    (root / "config").mkdir()
    fake = _list(root)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Bar!", True)))
    WorkspaceTab._rename_test(fake, tests / "Foo")
    assert [p.name for p in tests.iterdir()] == ["Bar_"]
    fake.refresh.assert_called_once()
    fake._select_test_by_path.assert_called_once_with(tests / "Bar_")


@WIN
def test_the_list_reports_a_refused_delete_and_keeps_the_test(tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from workflow_editor.core import fileio
    from workflow_editor.tabs.workspace_tab import WorkspaceTab

    monkeypatch.setattr(fileio, "_BUSY_PAUSE_S", 0)
    root, tests = _project(tmp_path, "Foo")
    (root / "config").mkdir()
    fake = _list(root)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(a[2])))
    with open(tests / "Foo" / "test.py", "r", encoding="utf-8"):
        WorkspaceTab._delete_test(fake, tests / "Foo")
    assert "nothing was removed" in shown[0] and (tests / "Foo" / "test.py").exists()
    fake.test_deleted.emit.assert_not_called()


def test_the_list_deletes_through_the_core_after_confirmation(tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from workflow_editor.tabs.workspace_tab import WorkspaceTab

    root, tests = _project(tmp_path, "Foo")
    (root / "config").mkdir()
    fake = _list(root)
    answers = iter([QMessageBox.No, QMessageBox.Yes])
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: next(answers)))
    WorkspaceTab._delete_test(fake, tests / "Foo")
    assert (tests / "Foo").exists()  # "No" deletes nothing
    WorkspaceTab._delete_test(fake, tests / "Foo")
    assert not (tests / "Foo").exists()
    fake.test_deleted.emit.assert_called_once_with(tests / "Foo")

