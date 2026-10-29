"""What works on the open project does not survive a project switch: Open Project, a recent
project and New Project first ask about the open test's unsaved changes (as opening another test
does) and close the Skill chat windows and the wizards, each through its own close (its turn and
backend stop); then the open test closes. A cancelled question (Cancel on the unsaved changes, a
wizard whose discard question was cancelled) keeps the open project. Reopening the open project
keeps them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from procedo.studio.authoring import skill_menu
from procedo.studio.controllers import project_controller
from procedo.studio.dock import skill_chat_widget as scw

from .test_backend_start_off_gui import _spin, _Worker
from .test_lazy_backend import _Backend, _Factory
from .test_main_window_characterization import _make_project, _open_project, _opened
from .test_main_window_characterization import make_window as make_window
from .test_main_window_characterization import mbox as mbox
from .test_main_window_characterization import qapp as qapp
from .test_skill_needed_context import _skill

SWITCHES = ("open", "recent", "new")


@pytest.fixture
def opened(make_window, tmp_path):
    w = make_window()
    _open_project(w, _make_project(tmp_path / "proj"))
    return w


def _switch(w, monkeypatch, how: str, root) -> None:
    """Switch *w* to *root* the way the operator does it (*how*: one of :data:`SWITCHES`)."""
    if how == "open":
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(root))
        )
        w._project_controller.on_open_project()
    elif how == "recent":
        w._project_controller.on_open_recent(str(root))
    else:
        made = SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted,
            project_location=str(root.parent),
            project_name=root.name,
        )
        monkeypatch.setattr(project_controller, "NewProjectDialog", lambda _parent: made)
        w._project_controller.on_new_project()
    _opened(w)


def _flush_deletes() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class _KeepsItsWork(QDialog):
    """A wizard with unsaved work whose "discard?" question the operator cancels (``keep``)."""

    keep = True

    def closeEvent(self, event):  # noqa: N802
        if self.keep:
            event.ignore()
        else:
            super().closeEvent(event)


def _keeping_wizard(w) -> _KeepsItsWork:
    wizard = _KeepsItsWork()
    skill_menu._hold_modeless(w, "_wizard_dialogs", wizard)
    wizard.show()
    return wizard


def _close(wizard) -> None:
    wizard.keep = False
    wizard.close()
    _flush_deletes()


def _chat_with_a_turn(w, monkeypatch):
    """The Skill chat window open on *w*, its turn running on a started backend:
    ``(dialog, chat, backend)``."""
    backend = _Backend()
    w.llm_server._backend_factory = _Factory(backend)
    monkeypatch.setattr(scw, "LLMWorker", _Worker)
    monkeypatch.setattr(skill_menu, "load_skills", lambda project_root=None: [_skill()])
    monkeypatch.setattr("procedo.studio.core.studio_settings.load_settings", lambda: {})
    _Worker.made = []
    skill_menu.launch_skill_chat(w)
    (dialog,) = w._skill_chat_dialogs
    chat = dialog._chat
    assert chat.send_user_turn("hello")
    _spin(lambda: _Worker.made)  # the turn runs on the started backend
    return dialog, chat, backend


@pytest.mark.parametrize("how", SWITCHES)
def test_a_switch_closes_the_skill_chat_window_and_stops_its_turn_and_backend(
    opened, monkeypatch, tmp_path, how
):
    w = opened
    dialog, chat, backend = _chat_with_a_turn(w, monkeypatch)

    _switch(w, monkeypatch, how, _make_project(tmp_path / "other"))

    assert w.project_manager.project_root == tmp_path / "other"
    assert backend.stopped and chat._worker is None and chat._llm.backend is None
    _flush_deletes()
    assert not shiboken6.isValid(dialog) and w._skill_chat_dialogs == []


@pytest.mark.parametrize("how", SWITCHES)
def test_a_wizard_that_keeps_its_work_cancels_the_switch(opened, monkeypatch, tmp_path, how):
    w = opened
    before = w.project_manager.project_root
    wizard = _keeping_wizard(w)
    noted = list(w._noted_recents)

    _switch(w, monkeypatch, how, _make_project(tmp_path / "other"))

    assert w.project_manager.project_root == before
    assert w._noted_recents == noted
    assert w._wizard_dialogs == [wizard] and wizard.isVisible()
    _close(wizard)


def test_reopening_the_open_project_keeps_its_windows(opened, monkeypatch):
    w = opened
    wizard = _keeping_wizard(w)
    root = w.project_manager.project_root

    _switch(w, monkeypatch, "open", root)

    assert w._noted_recents[-1] == str(root) and wizard.isVisible()
    _close(wizard)


EDITED = "# t1\n\nEdited, unsaved.\n"


@pytest.mark.parametrize("how", SWITCHES)
def test_cancel_on_the_open_test_s_unsaved_changes_cancels_the_switch(
    opened, mbox, monkeypatch, tmp_path, how
):
    w = opened
    before = w.project_manager.project_root
    w.open_test(before / "tests" / "t1")
    am = w.artifact_manager
    w.text_only_tab.text_editor.setPlainText(EDITED)
    mbox.question_answer = QMessageBox.StandardButton.Cancel

    _switch(w, monkeypatch, how, _make_project(tmp_path / "other"))

    assert [t for t, _ in mbox.question] == ["Unsaved Changes"]
    assert w.project_manager.project_root == before and w.artifact_manager is am
    assert w.text_only_tab.text_editor.toPlainText() == EDITED


def test_after_a_switch_no_test_of_the_old_project_stays_open(opened, mbox, monkeypatch, tmp_path):
    w = opened
    t1 = w.project_manager.project_root / "tests" / "t1"
    w.open_test(t1)
    w.text_only_tab.text_editor.setPlainText(EDITED)
    mbox.question_answer = QMessageBox.StandardButton.Discard

    _switch(w, monkeypatch, "open", _make_project(tmp_path / "other"))

    assert [t for t, _ in mbox.question] == ["Unsaved Changes"]
    assert w.project_manager.project_root == tmp_path / "other"
    assert w.artifact_manager is None and w.session_state is None
    assert w.text_only_tab.text_editor.toPlainText() == ""
    assert not w.view_column.isEnabled() and not w.dock.isEnabled()
    assert w.header.title.text() == "No test open"
    assert "Edited" not in (t1 / "procedure_text.md").read_text(encoding="utf-8")


def test_a_wizard_s_cancel_leaves_the_skill_chat_and_its_turn(opened, monkeypatch, tmp_path):
    """The wizards close first: one kept open leaves the Skill chat window untouched."""
    w = opened
    dialog, chat, backend = _chat_with_a_turn(w, monkeypatch)
    wizard = _keeping_wizard(w)

    _switch(w, monkeypatch, "open", _make_project(tmp_path / "other"))

    assert w._skill_chat_dialogs == [dialog] and dialog.isVisible()
    assert chat.is_busy and chat._worker is not None and not backend.stopped
    _close(wizard)
    dialog.close()
    _flush_deletes()


@pytest.mark.parametrize("refuses", ["unsaved test", "wizard"])
def test_new_project_asks_before_its_dialog_so_a_cancel_creates_nothing(
    opened, mbox, monkeypatch, refuses
):
    w = opened
    wizard = None
    if refuses == "wizard":
        wizard = _keeping_wizard(w)
    else:
        w.open_test(w.project_manager.project_root / "tests" / "t1")
        w.text_only_tab.text_editor.setPlainText(EDITED)
        mbox.question_answer = QMessageBox.StandardButton.Cancel
    shown = []
    monkeypatch.setattr(project_controller, "NewProjectDialog", shown.append)

    w._project_controller.on_new_project()

    assert shown == []
    if wizard is not None:
        _close(wizard)


def test_a_root_that_will_not_open_warns_and_switches_nothing(opened, mbox, monkeypatch, tmp_path):
    """The folder names no project: the Invalid Project warning, no recent noted, the open
    test kept."""
    w = opened
    before = w.project_manager.project_root
    w.open_test(before / "tests" / "t1")
    am = w.artifact_manager
    noted = list(w._noted_recents)
    monkeypatch.setattr(w.project_manager, "project_root_of", lambda _path: None)

    _switch(w, monkeypatch, "open", _make_project(tmp_path / "other"))

    assert [t for t, _ in mbox.warning] == ["Invalid Project"]
    assert w._noted_recents == noted and w.project_manager.project_root == before
    assert w.artifact_manager is am
