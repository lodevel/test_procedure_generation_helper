"""Characterization tests for the editor MainWindow (procedo/studio/main_window.py).

Safety net for the planned carve of the 2,726-line / 94-method MainWindow:
each test pins one seam of CURRENT behavior — construction, menu/action
wiring, host-backed slots, project open, test-open tab lifecycle, the save
choke-point (id=folder enforcement + sync-state transparency, task #41),
the bundle-import live-refresh seams (task #39), settings-dialog wiring and
the close flow — so an extraction that silently drops a wire fails loudly
here. Behavior is recorded AS IS; suspected bugs are noted in comments and
pinned, not fixed.

Conventions (mirrors the host repo's test_main_window_run_report.py):
  * offscreen Qt, module-scoped QApplication;
  * ALL QMessageBox statics neutralized into a recording log, and
    QMessageBox.exec neutralized so the three-button coherence modal can
    never block the suite;
  * settings load stubbed to a DISABLED LLM backend -> no
    OpenCodeServerManager, no prewarm threads, no health poll, no network;
  * app_settings.add_recent_project stubbed -> never touches the real
    shared app settings;
  * showEvent is never triggered (no .show()), so the CLI-args / prewarm /
    health-poll path stays inert — construction-time state is what's pinned.
"""

import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from pathlib import Path  # noqa: E402

from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFileDialog, QMessageBox,
)

import procedo.studio.main_window as mw_mod  # noqa: E402
from procedo.studio.pack_ops import engine_async  # noqa: E402
from procedo.core import app_settings  # noqa: E402
from procedo.studio.llm.backend_factory import BACKEND_TYPE_NONE  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class _MBoxLog:
    """Records every static message box the handlers raise (title, text)."""

    def __init__(self):
        self.information: list[tuple[str, str]] = []
        self.warning: list[tuple[str, str]] = []
        self.critical: list[tuple[str, str]] = []
        self.question: list[tuple[str, str]] = []
        self.question_answer = QMessageBox.StandardButton.Yes


@pytest.fixture
def mbox(monkeypatch):
    log = _MBoxLog()
    ok = QMessageBox.StandardButton.Ok

    def _rec(kind, ret=None):
        def _fn(parent, title="", text="", *a, **k):
            getattr(log, kind).append((str(title), str(text)))
            return ret if ret is not None else log.question_answer
        return staticmethod(_fn)

    monkeypatch.setattr(QMessageBox, "information", _rec("information", ok))
    monkeypatch.setattr(QMessageBox, "warning", _rec("warning", ok))
    monkeypatch.setattr(QMessageBox, "critical", _rec("critical", ok))
    monkeypatch.setattr(QMessageBox, "question", _rec("question"))
    # _check_artifact_coherence builds a QMessageBox INSTANCE and .exec()s
    # it (three custom buttons). Neutralize instance exec so no test (or
    # fixture teardown) can ever block on a modal; clickedButton() then
    # returns None, which that handler treats as "Continue anyway".
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    yield log


@pytest.fixture
def make_window(qapp, monkeypatch, mbox):
    """Factory building a MainWindow with the LLM backend disabled, recents
    writes sandboxed and modals neutralized; returns its StudioWorkspace.
    Never calls .show() (the showEvent CLI/prewarm/health-poll path stays
    untriggered)."""
    monkeypatch.setattr(mw_mod, "load_settings", lambda: {"llm_backend": "none"})
    recents: list[str] = []
    monkeypatch.setattr(app_settings, "add_recent_project", recents.append)

    windows = []

    def _make(**kwargs):
        window = mw_mod.MainWindow(**kwargs)
        w = window.workspace
        w._noted_recents = recents  # test-side spy channel
        windows.append(window)
        return w

    yield _make
    for window in windows:
        # Drop test/session state so closeEvent's unsaved-changes and
        # coherence prompts can never fire during teardown.
        window.workspace.artifact_manager = None
        window.workspace.session_state = None
        window.close()


# --- fixture project helpers -------------------------------------------------

def _make_project(root: Path, tests=("t1",), with_bundle=True) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    # A project_id: machine-local state (venv, media cache, install stamp) is keyed
    # by it since projects moved to a share, and without one there is none at all.
    (root / "config" / "config.json").write_text(
        json.dumps({"project_id": "aaaaaaaa-1111-2222-3333-444444444444"}), encoding="utf-8")
    if with_bundle:
        (root / "bundle" / "wheels").mkdir(parents=True, exist_ok=True)
        (root / "bundle" / "bundle.json").write_text(
            json.dumps({"id": "b", "version": "1.0.0"}), encoding="utf-8")
        # An INSTALLABLE bundle: the install stamp sits beside the venv, and the venv
        # is keyed by the bundle, so a folder with no requirements.txt has no stamp.
        (root / "bundle" / "wheels" / "requirements.txt").write_text("", encoding="utf-8")
    for name in tests:
        d = root / "tests" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "procedure_text.md").write_text(
            f"# {name}\n\nBody of {name}.\n", encoding="utf-8")
        (d / "procedure.json").write_text(
            json.dumps({"id": name, "steps": []}, indent=2), encoding="utf-8")
        (d / "test.py").write_text("def main():\n    return 0\n",
                                   encoding="utf-8")
    return root


def _settle(until=None, timeout=10):
    """Pump until ``until()`` holds; without one, until no engine call has run for 50 ms (each
    runs off the GUI thread, and an answer may start another)."""
    def quiet():
        return not any(t.name == engine_async.THREAD for t in threading.enumerate())

    end = time.monotonic() + timeout
    quiet_since = None
    while time.monotonic() < end:
        QApplication.processEvents()
        if until is not None:
            if until():
                return
        elif not quiet():
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since > 0.05:
            return
        time.sleep(0.001)
    assert until is None or until(), "the awaited engine answer never landed"


def _open_project(w, root: Path) -> None:
    """Arrange-only project open (the same calls _on_open_project makes
    after the directory picker), without any dialog."""
    assert w.project_manager.set_project_root(root)
    w._refresh_after_open()


def _menus(w):
    """[(title, action, menu)] for the top-level menubar, in bar order.

    The QAction refs MUST be kept alive by the caller: PySide6 deletes the
    C++ QMenu when the owning QAction wrapper is garbage-collected (verified
    on PySide6 offscreen — a {title: menu} dict alone goes dangling).
    """
    return [(a.text(), a, a.menu()) for a in w.menu_bar.actions()
            if a.menu() is not None]


def _layout(menu):
    """A menu's entries in order: action texts, "-" for a separator, submenus by title."""
    return ["-" if a.isSeparator() else a.text() for a in menu.actions()]


def _find_action(menu, text):
    """The action called *text* in *menu* or one of its submenus."""
    for a in menu.actions():
        if a.menu() is not None:
            found = _find_action(a.menu(), text)
            if found is not None:
                return found
        elif a.text() == text:
            return a
    return None


# =========================================================================
# 1. construction offscreen
# =========================================================================

def test_construction_smoke_disabled_backend(make_window):
    # Pins what a bare construction (no project, no show) produces once
    # load_settings returns llm_backend="none": NO server manager is built
    # (the only stub needed for offscreen construction is load_settings —
    # everything else constructs for real), the four tabs exist in workflow
    # order, and the views + Assistant start disabled until a test is loaded.
    w = make_window()

    assert w._server_manager is None
    assert w._backend_factory.backend_type == BACKEND_TYPE_NONE
    assert [b.text() for b in w.view_buttons.buttons()] == [
        "Text", "Text–JSON", "JSON–Code", "Traceability"]
    assert w.view_stack.currentWidget() is w.text_only_tab
    assert not w.view_column.isEnabled()
    assert not w.dock.isEnabled()

    # Header and footer initial state: nothing loaded, Save all off.
    assert w.header.title.text() == "No test open"
    assert [p.text() for p in w.artifact_pills.values()] == [
        "Text ○", "JSON ○", "Code ○"]
    assert w.sync_pill.text() == "⇄ Sync"
    assert w.project_link.text() == "Project: none"
    assert w.rules_link.text() == "Rules: none"
    assert not w.save_all_btn.isEnabled()

    # The first addTab fires currentChanged(0) during __init__, so the
    # Save action is already context-labelled at construction.
    assert w.save_action.text() == "&Save Text"

    # No watches armed before a project exists.
    assert w._config_watcher.files() == []
    assert w._config_watcher.directories() == []
    assert w._watched_config_path is None
    assert w._bundle_signature is None


def test_a_health_probes_verdict_lands_on_the_ui_thread(make_window):
    w = make_window()
    ran = []
    w._on_health_probe_result = lambda sm, alive: ran.append((sm, alive, threading.current_thread()))
    worker = threading.Thread(target=lambda: w._health_verdict(("sm", True)))
    worker.start()
    worker.join(5)
    _settle(lambda: ran)
    assert ran == [("sm", True, threading.main_thread())]


# =========================================================================
# 2. menu / action wiring
# =========================================================================

def test_menubar_order_and_menu_contents(make_window):
    # Pins the Menus board (Studio redesign slice 6): the bar in board order
    # (Bench's Project menu holds Project Configuration, Template Manager and
    # Scenarios; Bundle Library sits in Packages) and every menu's entries.
    w = make_window()
    entries = _menus(w)  # keeps action refs alive for the whole test
    by_title = {t: m for t, _, m in entries}
    assert [t for t, _, _ in entries] == [
        "&File", "&Edit", "&View", "&Project", "S&kills", "&Wizards",
        "&Packages", "&Help"]

    # "&Save Text" (not "&Save"): the first addTab fires currentChanged(0)
    # during __init__, so the context-aware Save label is set from birth.
    assert _layout(by_title["&File"]) == [
        "&New Project…", "&Open Project…", "Open &Recent", "-",
        "&Save Text", "Save &All", "&Export", "-",
        "Se&ttings…", "-",
        "E&xit"]
    assert _layout(w.export_menu) == [
        "Procedure to &Markdown…", "Procedure to &Word…"]
    assert _layout(by_title["&Edit"]) == [
        "&Find…", "&Replace…", "-", "Mark Artifacts In &Sync"]
    assert _layout(by_title["&View"]) == ["Show &Workspace", "Show &Assistant"]
    assert _layout(by_title["&Project"]) == [
        "Project &Configuration…", "&Template Manager…", "&Scenarios…"]
    skills = _layout(by_title["S&kills"])
    assert skills[0].startswith("Skill chat…")
    assert skills[-3:] == ["-", "Open skills folder…", "How to write a skill…"]
    assert _layout(by_title["&Wizards"]) == ["Choose a wizard…"]
    assert _layout(by_title["&Packages"]) == [
        "Bundle &Library…", "&Install Package…"]
    assert _layout(by_title["&Help"]) == [
        "&DSL Syntax Reference", "Word Export &Keywords…", "-", "&About"]

    # View menu: both column toggles are checkable and start checked (the
    # columns are fixed; only View hides them).
    for a in by_title["&View"].actions():
        assert a.isCheckable()
        assert a.isChecked()


def test_menu_shortcuts(make_window):
    # Pins the keyboard surface a carve must not lose.
    w = make_window()
    entries = _menus(w)  # keeps action refs alive for the whole test
    shortcuts = {}
    for _, _, m in entries:
        for a in m.actions():
            if not a.isSeparator() and a.menu() is None:
                shortcuts[a.text()] = a.shortcut().toString()
    assert {t: k for t, k in shortcuts.items() if k} == {
        "&New Project…": "Ctrl+N",
        "&Open Project…": "Ctrl+O",
        "&Save Text": "Ctrl+S",  # context-labelled at birth
        "Save &All": "Ctrl+Shift+S",
        "Se&ttings…": "Ctrl+,",
        "&Find…": "Ctrl+F",
        "&Replace…": "Ctrl+H",
        "Show &Workspace": "Ctrl+Shift+E",
        "Show &Assistant": "Ctrl+Shift+A",
        "&DSL Syntax Reference": "F1",
    }


# (menu, entry, StudioWorkspace handler) for every handler-backed menu entry.
_MENU_HANDLERS = [
    ("&File", "&New Project…", "_on_new_project"),
    ("&File", "&Open Project…", "_on_open_project"),
    ("&File", "&Save Text", "_on_save"),
    ("&File", "Save &All", "_on_save_all"),
    ("&File", "Procedure to &Markdown…", "_on_export_markdown"),
    ("&File", "Procedure to &Word…", "_on_export_word"),
    ("&File", "Se&ttings…", "_on_settings"),
    ("&Edit", "&Find…", "_on_find"),
    ("&Edit", "&Replace…", "_on_replace"),
    ("&Edit", "Mark Artifacts In &Sync", "_on_sync_indicator_clicked"),
    ("&Project", "Project &Configuration…", "_on_project_configuration"),
    ("&Project", "&Template Manager…", "_on_template_manager"),
    ("&Project", "&Scenarios…", "_on_scenarios"),
    ("&Packages", "Bundle &Library…", "_on_bundle_library"),
    ("&Packages", "&Install Package…", "_on_install_package"),
    ("&Help", "&DSL Syntax Reference", "_on_syntax_reference"),
    ("&Help", "Word Export &Keywords…", "_on_export_keywords"),
    ("&Help", "&About", "_on_about"),
]


def test_every_menu_entry_triggers_its_handler(make_window, monkeypatch):
    # The moved entries (Project menu, Bundle Library) and every other one
    # still reach their handler when triggered from their new place.
    calls = []
    for _, _, name in _MENU_HANDLERS:
        monkeypatch.setattr(mw_mod.StudioWorkspace, name,
                            lambda self, *a, _name=name: calls.append(_name))
    w = make_window()
    entries = _menus(w)  # keeps action refs alive for the whole test
    by_title = {t: m for t, _, m in entries}
    for title, text, _ in _MENU_HANDLERS:
        _find_action(by_title[title], text).trigger()
    assert calls == [name for _, _, name in _MENU_HANDLERS]


# =========================================================================
# 3. host-backed slots
# =========================================================================

def test_host_slots_guard_missing_project(make_window, mbox):
    # Pins the guard inside host-backed slots, triggered from their menu
    # entries (the triggered(checked) payload reaches a (self)-only slot): no
    # project open -> the slot's own "open a project first" info box, and the
    # heavy procedo.core dialog is never reached.
    w = make_window()
    entries = _menus(w)  # keeps action refs alive for the whole test
    project_menu = {t: m for t, _, m in entries}["&Project"]

    _find_action(project_menu, "Project &Configuration…").trigger()
    _find_action(project_menu, "&Scenarios…").trigger()

    titles = [t for t, _ in mbox.information]
    assert titles == ["Project Configuration", "Scenarios"]
    assert all("Open or create a project first." == text
               for _, text in mbox.information)


# =========================================================================
# 4. project open flows
# =========================================================================

def test_open_project_dialog_happy_path(make_window, mbox, monkeypatch,
                                        tmp_path):
    # Pins the whole _on_open_project -> _refresh_after_open wiring: root
    # set, shared recents noted, test list loaded + New Test enabled,
    # footer updated (the project by name, "Rules: none" when the project
    # has no rules), and the task-#39 watcher set armed (config.json file,
    # config dir, project root AND bundle dir) with the bundle-signature
    # baseline seeded.
    root = _make_project(tmp_path / "proj")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(root)))
    w = make_window()

    w._on_open_project()

    assert w.project_manager.project_root == root
    assert w._noted_recents == [str(root)]
    assert w.workspace_widget.new_test_btn.isEnabled()
    assert w.project_link.text() == "Project: proj"
    assert str(root) in w.project_link.toolTip()
    assert w.rules_link.text() == "Rules: none"

    assert w._watched_config_path == root / "config" / "config.json"
    assert w._watched_config_dir == root / "config"
    assert w._watched_project_root == root
    assert w._watched_bundle_dir == root / "bundle"
    watched_files = {Path(p) for p in w._config_watcher.files()}
    watched_dirs = {Path(p) for p in w._config_watcher.directories()}
    assert root / "config" / "config.json" in watched_files
    assert {root, root / "config", root / "bundle"} <= watched_dirs
    # Baselines seeded so the FIRST watcher event compares, never fires.
    assert w._bundle_signature is not None
    assert w._bundle_signature_changed() is False
    # Opening a project does NOT load any test: tabs stay disabled.
    assert not w.view_column.isEnabled()
    assert mbox.warning == []


def test_open_project_dialog_invalid_folder_warns(make_window, mbox,
                                                  monkeypatch, tmp_path):
    # Pins the rejection path: a folder with neither tests/ nor config/
    # (and not detectable as a test folder) -> "Invalid Project" warning,
    # nothing set, no recents write.
    bogus = tmp_path / "not_a_project"
    bogus.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(bogus)))
    w = make_window()

    w._on_open_project()

    assert w.project_manager.project_root is None
    assert w._noted_recents == []
    assert [t for t, _ in mbox.warning] == ["Invalid Project"]


def test_open_recent_menu_and_missing_dir(make_window, mbox, monkeypatch,
                                          tmp_path):
    # Pins the Open Recent surface: the menu repopulates from the SHARED
    # app_settings recents; an empty
    # list yields one disabled placeholder; a recent whose folder vanished
    # warns "Project Not Found" and leaves the project unset.
    w = make_window()
    recents: list[str] = []
    monkeypatch.setattr(app_settings, "load_app_settings",
                        lambda: {"recent_projects": list(recents)})

    w._rebuild_open_recent_menu()
    acts = w._open_recent_menu.actions()
    assert [a.text() for a in acts] == ["No recent projects"]
    assert not acts[0].isEnabled()

    recents[:] = [str(tmp_path / "gone"), str(tmp_path / "also_gone")]
    w._rebuild_open_recent_menu()
    assert [a.text() for a in w._open_recent_menu.actions()] == recents

    w._on_open_recent(str(tmp_path / "gone"))
    assert [t for t, _ in mbox.warning] == ["Project Not Found"]
    assert w.project_manager.project_root is None


# =========================================================================
# 5. test-open tab lifecycle
# =========================================================================

def test_open_test_loads_tabs_and_seeds_session(make_window, tmp_path):
    # Pins _on_test_opened end to end: managers rebuilt for the folder,
    # editors filled from disk (editor.toPlainText IS the live content
    # source afterwards), views + Assistant enabled, header pills ✓ for
    # every non-empty artifact, FIRST open lands on the Text tab, and the
    # sync baseline is seeded + persisted to .llm_session.json on open.
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    t1 = root / "tests" / "t1"

    w.open_test(t1)  # public API -> _on_test_opened

    assert w.artifact_manager.test_dir == t1
    assert w.session_state._file_path == t1 / ".llm_session.json"
    assert w.view_column.isEnabled() and w.dock.isEnabled()
    assert w.header.title.text() == "t1"
    assert w.view_stack.currentWidget() is w.text_only_tab
    assert w.text_only_tab.text_editor.toPlainText() == \
        (t1 / "procedure_text.md").read_text(encoding="utf-8")
    assert w.text_json_tab.json_editor.toPlainText() == \
        (t1 / "procedure.json").read_text(encoding="utf-8")
    assert [p.text() for p in w.artifact_pills.values()] == [
        "Text ✓", "JSON ✓", "Code ✓"]
    assert w.sync_pill.text() == "⇄ JSON ↔ Code in sync"
    # First-open baseline: hashes for all three tracked artifacts, saved.
    assert set(w.session_state.artifact_hashes) == {
        "procedure.json", "test.py", "procedure_text.md"}
    on_disk = json.loads(
        (t1 / ".llm_session.json").read_text(encoding="utf-8"))
    assert on_disk["artifacts_in_sync"] is True
    assert on_disk["artifact_hashes"] == w.session_state.artifact_hashes


def test_open_second_test_preserves_current_tab(make_window, tmp_path):
    # Pins: the Text-tab default applies to the FIRST open only; switching
    # tests later keeps the user's current tab.
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    w = make_window()
    _open_project(w, root)

    w.open_test(root / "tests" / "t1")
    w.view_stack.setCurrentWidget(w.json_code_tab)
    w.open_test(root / "tests" / "t2")

    assert w.artifact_manager.test_dir == root / "tests" / "t2"
    assert w.view_stack.currentWidget() is w.json_code_tab
    assert w.header.title.text() == "t2"


def test_reopen_after_external_edit_flips_sync_state(make_window, tmp_path):
    # Pins _check_for_external_changes: an artifact edited on disk while
    # the stored baseline exists (here: between two opens of the same test)
    # marks the session out-of-sync and shows the Sync pill unconfirmed. Never
    # auto-restores — only user acknowledgment does.
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    assert w.session_state.artifacts_in_sync is True

    with open(t1 / "test.py", "a", encoding="utf-8") as f:
        f.write("# externally edited\n")
    w.open_test(t1)

    assert w.session_state.artifacts_in_sync is False
    assert w.sync_pill.text() == "⇄ Sync unconfirmed · acknowledge"
    on_disk = json.loads(
        (t1 / ".llm_session.json").read_text(encoding="utf-8"))
    assert on_disk["artifacts_in_sync"] is False


# =========================================================================
# 6. save choke-point: id=folder enforcement + sync transparency (#41)
# =========================================================================

def test_save_enforces_folder_id_without_flipping_sync(make_window, tmp_path):
    # Pins task #41 end to end at the _on_save choke-point: a hand-authored
    # id that differs from the folder is rewritten to the folder name ON
    # SAVE (folder is the single source of truth), the correction is
    # mirrored back into the editor buffer, and — because sync hashes are
    # computed through the same enforcement — the id-only correction does
    # NOT flip the acknowledged in-sync state.
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    (t1 / "procedure_text.md").write_text(
        "# WRONG_ID\n\nBody of t1.\n", encoding="utf-8")
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    assert w.view_stack.currentWidget() is w.text_only_tab
    assert w.session_state.artifacts_in_sync is True

    w._on_save()  # current tab = Text -> saves procedure_text.md only

    disk = (t1 / "procedure_text.md").read_text(encoding="utf-8")
    assert disk.startswith("# t1\n")
    assert "WRONG_ID" not in disk
    # correction mirrored into the live editor, not just disk
    assert w.text_only_tab.text_editor.toPlainText().startswith("# t1\n")
    # id-only enforcement is NOT a divergence (the #41 fix)
    assert w.session_state.artifacts_in_sync is True
    assert w.sync_pill.text() == "⇄ JSON ↔ Code in sync"
    assert w.footer.note.text() == "Saved"


def test_real_edit_save_flips_out_of_sync(make_window, tmp_path):
    # Pins _check_sync_hashes transparency: a REAL content change saved on
    # top of an acknowledged baseline flips artifacts_in_sync to False
    # (persisted) and the indicator to ⚠️ — and the baseline hash is NOT
    # advanced by the save (only acknowledgment re-baselines).
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    baseline = dict(w.session_state.artifact_hashes)

    w.text_only_tab.text_editor.setPlainText(
        "# t1\n\nBody of t1.\n\nNew paragraph.\n")
    w._on_save()

    assert "New paragraph." in \
        (t1 / "procedure_text.md").read_text(encoding="utf-8")
    assert w.session_state.artifacts_in_sync is False
    assert w.sync_pill.text() == "⇄ Sync unconfirmed · acknowledge"
    assert w.session_state.artifact_hashes == baseline  # not re-seeded


@pytest.mark.parametrize("tab", ["text_only_tab", "text_json_tab"])
def test_parse_and_generate_marks_in_sync(make_window, monkeypatch, tmp_path, tab):
    # procedure.json and test.py both come from the same text in one action:
    # an out-of-sync test is in sync afterwards (once its proposal card is
    # accepted), and stays so on reopen.
    from procedo.studio.dock.chat_messages import ProposalMessage
    from procedo.studio.pack_ops import pack_parsers
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    w.text_only_tab.text_editor.setPlainText("# t1\n\nEdited body.\n")
    w._on_save()
    assert w.session_state.artifacts_in_sync is False

    monkeypatch.setattr(pack_parsers, "parse_text",
                        lambda text, project_root=None: ({"id": "t1", "steps": [{"n": 1}]}, []))
    monkeypatch.setattr(pack_parsers, "generate_code",
                        lambda proc, project_root=None: ("def main():\n    return 1\n", []))
    view = getattr(w, tab)
    view._on_parse_and_generate()
    _settle(lambda: view.tab_context.messages
            and isinstance(view.tab_context.messages[-1], ProposalMessage))
    assert "return 1" not in (t1 / "test.py").read_text(encoding="utf-8")  # waits for Accept
    card = view.tab_context.messages[-1]
    assert isinstance(card, ProposalMessage)
    assert card.proposal.accept()

    assert "return 1" in (t1 / "test.py").read_text(encoding="utf-8")
    assert w.session_state.artifacts_in_sync is True
    assert w.sync_pill.text() == "⇄ JSON ↔ Code in sync"
    assert w.session_state.artifact_hashes == w.artifact_manager.compute_hashes()
    assert json.loads((t1 / ".llm_session.json").read_text(encoding="utf-8"))["artifacts_in_sync"] is True
    assert not w.workspace_widget._stale_artifacts(t1)


def test_the_media_refresh_of_an_open_test_keeps_it_in_sync(make_window, monkeypatch, tmp_path):
    # The save's media refresh rewrites procedure.json once the engine answers: derived data,
    # so the open test's acknowledged baseline follows it. Else the next Ctrl+S un-syncs the
    # test, and switching tests writes the stale baseline over the session file.
    from procedo.studio.pack_ops import pack_parsers
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    monkeypatch.setattr(pack_parsers, "refresh_media",
                        lambda doc, root=None: {**doc, "steps": [{"n": 1, "media": ["P4"]}]})
    from procedo.studio.core import test_sync
    disk_marks = []  # the open test's baseline is its controller's alone
    monkeypatch.setattr(test_sync, "mark_in_sync", lambda *a: disk_marks.append(a))
    w.text_json_tab._write_parse_and_generate(
        json.dumps({"id": "t1", "steps": [{"n": 1}]}, indent=2), "def main():\n    return 1\n")
    _settle(lambda: "P4" in (t1 / "procedure.json").read_text(encoding="utf-8"))
    _settle()
    assert disk_marks == []

    assert w.session_state.artifact_hashes == w.artifact_manager.compute_hashes()
    w._on_save()                                                  # Ctrl+S, nothing edited
    assert w.session_state.artifacts_in_sync is True
    assert not w.workspace_widget._stale_artifacts(t1)
    w.open_test(root / "tests" / "t2")                            # the switch saves t1's session
    assert not w.workspace_widget._stale_artifacts(t1)


def test_an_edited_json_is_not_acknowledged_by_its_own_refresh(make_window, monkeypatch, tmp_path):
    # Only the JSON as acknowledged gives way to its refresh: an answer landing at once (no
    # project: the engine runs in this process) must not take the user's edit into the baseline.
    from procedo.studio.core import ArtifactType
    from procedo.studio.pack_ops import pack_parsers
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    assert w.session_state.artifacts_in_sync is True
    monkeypatch.setattr(engine_async, "call",
                        lambda project_root, fn, on_done, **k: on_done(engine_async._run(fn)))
    monkeypatch.setattr(pack_parsers, "refresh_media",
                        lambda doc, root=None: {**doc, "steps": [{"n": 2, "media": ["P4"]}]})
    card = next(c for c in w.text_json_tab.cards if c.kind is ArtifactType.PROCEDURE_JSON)
    card.editor.setPlainText(json.dumps({"id": "t1", "steps": [{"n": 2}]}, indent=2))
    assert card.save()
    assert w.session_state.artifacts_in_sync is False


def test_the_refresh_of_an_out_of_sync_test_leaves_its_baseline(make_window, monkeypatch, tmp_path):
    # Out of sync, the JSON's refresh is no acknowledgment: the baseline stays as it was.
    from procedo.studio.core import ArtifactType
    from procedo.studio.pack_ops import pack_parsers
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    w.text_only_tab.text_editor.setPlainText("# t1\n\nEdited body.\n")
    w._on_save()
    assert w.session_state.artifacts_in_sync is False
    baseline = dict(w.session_state.artifact_hashes)
    monkeypatch.setattr(engine_async, "call",
                        lambda project_root, fn, on_done, **k: on_done(engine_async._run(fn)))
    monkeypatch.setattr(pack_parsers, "refresh_media",
                        lambda doc, root=None: {**doc, "steps": [{"n": 1, "media": ["P4"]}]})
    card = next(c for c in w.text_json_tab.cards if c.kind is ArtifactType.PROCEDURE_JSON)
    assert card.save()                                            # the JSON as acknowledged
    assert "P4" in (root / "tests" / "t1" / "procedure.json").read_text(encoding="utf-8")
    assert w.session_state.artifact_hashes == baseline


def test_a_save_rereads_the_open_tests_row_only(make_window, tmp_path):
    # 44 tests listed and hashed again on every save froze Parse + Generate for 300 ms.
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    listed = []
    real = w.project_manager.enumerate_test_folders
    w.project_manager.enumerate_test_folders = lambda: listed.append(1) or real()
    w.text_only_tab.text_editor.setPlainText("# t1\n\nEdited body.\n")
    w._on_save()
    w._document_controller.on_tab_artifact_saved()
    assert listed == []
    rows = w.workspace_widget.test_list
    tips = {rows.item(i).text(): rows.item(i).toolTip() for i in range(rows.count())}
    assert "Text: out of sync" in next(tip for text, tip in tips.items() if "t1" in text)


def test_a_media_refresh_landing_after_the_switch_rebaselines_the_session_file(
        make_window, monkeypatch, tmp_path):
    # The test closed before the engine answered: its session file is the baseline, so the
    # artifact manager re-takes it there (the controller only holds the open test's).
    from procedo.studio.pack_ops import pack_parsers
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    answers = []
    monkeypatch.setattr(engine_async, "call", lambda project_root, fn, on_done, **k: answers.append(
        lambda: on_done(engine_async._run(fn))))
    monkeypatch.setattr(pack_parsers, "refresh_media",
                        lambda doc, root=None: {**doc, "steps": [{"n": 1, "media": ["P4"]}]})
    w.text_json_tab._write_parse_and_generate(
        json.dumps({"id": "t1", "steps": [{"n": 1}]}, indent=2), "def main():\n    return 1\n")
    w.open_test(root / "tests" / "t2")
    [land] = answers
    land()

    assert "P4" in (t1 / "procedure.json").read_text(encoding="utf-8")
    assert not w.workspace_widget._stale_artifacts(t1)
    assert w.session_state.artifacts_in_sync is True              # t2's own state untouched


def test_parse_and_generate_with_nothing_changed_opens_no_review(make_window, monkeypatch, tmp_path):
    # The diff compares lines (a final newline on one side only is no change),
    # so output identical to the files on disk is no change at all: no proposal
    # card waits, the files are simply written.
    from procedo.studio.dock.chat_messages import ProposalMessage
    from procedo.studio.pack_ops import pack_parsers
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    code = (t1 / "test.py").read_text(encoding="utf-8")
    assert code.endswith("\n")
    monkeypatch.setattr(pack_parsers, "parse_text",
                        lambda text, project_root=None: ({"id": "t1", "steps": []}, []))
    monkeypatch.setattr(pack_parsers, "generate_code", lambda proc, project_root=None: (code, []))
    w.text_only_tab._on_parse_and_generate()
    _settle()

    assert not any(isinstance(m, ProposalMessage) for m in w.text_only_tab.tab_context.messages)
    assert w.session_state.artifacts_in_sync is True
    assert (t1 / "test.py").read_text(encoding="utf-8") == code


def test_sync_indicator_acknowledge_flow(make_window, mbox, tmp_path):
    # Pins _on_sync_indicator_clicked: out-of-sync + user answers Yes ->
    # in-sync restored, hashes re-baselined to current content, session
    # persisted, indicator back to ✅. Answering No changes nothing. When
    # already in sync the click is informational only.
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    w.text_only_tab.text_editor.setPlainText("# t1\n\nEdited body.\n")
    w._on_save()
    assert w.session_state.artifacts_in_sync is False
    old_baseline = dict(w.session_state.artifact_hashes)

    mbox.question_answer = QMessageBox.StandardButton.No
    w._on_sync_indicator_clicked()
    assert w.session_state.artifacts_in_sync is False

    mbox.question_answer = QMessageBox.StandardButton.Yes
    w._on_sync_indicator_clicked()

    assert [t for t, _ in mbox.question] == ["Acknowledge Sync"] * 2
    assert w.session_state.artifacts_in_sync is True
    assert w.session_state.artifact_hashes != old_baseline
    assert w.sync_pill.text() == "⇄ JSON ↔ Code in sync"
    on_disk = json.loads(
        (t1 / ".llm_session.json").read_text(encoding="utf-8"))
    assert on_disk["artifacts_in_sync"] is True

    w._on_sync_indicator_clicked()  # already in sync -> info box only
    assert [t for t, _ in mbox.information] == ["Artifacts In Sync"]


# =========================================================================
# 7. tab-change lifecycle
# =========================================================================

def test_tab_change_syncs_previous_tab_and_switches_context(make_window,
                                                            tmp_path):
    # Pins _on_tab_changed: ONLY the previously-active tab is synced to
    # artifacts + deactivated (inactive tabs may hold stale shared-artifact
    # content), the new tab is activated, the dock panels switch to the new
    # tab's TabContext, and the Save action label follows the tab.
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    assert w.view_stack.currentWidget() is w.text_only_tab
    assert w.save_action.text() == "&Save Text"

    calls = []
    w.text_only_tab.sync_editors_to_artifacts = \
        lambda: calls.append("sync_prev")
    w.text_only_tab.on_deactivated = lambda: calls.append("deactivate_prev")
    w.json_code_tab.on_activated = lambda: calls.append("activate_new")
    switched = []
    w.dock.chat_panel.switch_context = switched.append
    w.dock.session_viewer.switch_context = lambda ctx: None
    w.dock.raw_viewer.switch_context = lambda ctx: None

    w.view_stack.setCurrentWidget(w.json_code_tab)

    assert calls == ["sync_prev", "deactivate_prev", "activate_new"]
    assert switched == [w.json_code_tab.tab_context]
    assert w.save_action.text() == "&Save JSON–Code"


def test_switch_to_tab_name_mapping(make_window):
    # Pins the public switch_to_tab surface incl. the LEGACY name mappings
    # (json/text/code) other tools may still call with.
    w = make_window()
    for name, expected in [
        ("text_only", "text_only_tab"), ("text_json", "text_json_tab"),
        ("json_code", "json_code_tab"), ("traceability", "traceability_tab"),
        ("json", "text_json_tab"), ("text", "text_only_tab"),
        ("code", "json_code_tab"), ("JSON", "text_json_tab"),
    ]:
        w.switch_to_tab(name)
        assert w.view_stack.currentWidget() is getattr(w, expected), name
    current = w.view_stack.currentWidget()
    w.switch_to_tab("nonsense")  # unknown name = silent no-op
    assert w.view_stack.currentWidget() is current


def test_current_procedure_text_prefers_active_then_text_json(make_window,
                                                              tmp_path):
    # Pins the export content source: the ACTIVE tab's live editor first
    # (unsaved edits export), then text_json, then text_only — never the
    # artifact cache. The Traceability tab has no text_editor, so with it
    # active the text_json editor wins over text_only.
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    w.text_only_tab.text_editor.setPlainText("TEXT-ONLY buffer")
    w.text_json_tab.text_editor.setPlainText("TEXT-JSON buffer")

    w.switch_to_tab("traceability")
    assert w._current_procedure_text() == "TEXT-JSON buffer"

    w.switch_to_tab("text_only")
    assert w._current_procedure_text() == "TEXT-ONLY buffer"


# =========================================================================
# 8. bundle-import live-refresh seams (task #39)
# =========================================================================

def test_bundle_signature_and_stamp_change_detectors(make_window, tmp_path):
    # Pins the two task-#39 change detectors as edge-triggered latches:
    # baselined at _watch_project_config time (so the first probe is
    # False), True exactly once after the watched artifact moves, then
    # False again.
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)

    assert w._bundle_signature_changed() is False
    bundle_json = root / "bundle" / "bundle.json"
    os.utime(bundle_json, (1_700_000_000, 1_700_000_123))
    assert w._bundle_signature_changed() is True
    assert w._bundle_signature_changed() is False

    assert w._bundle_stamp_changed() is False
    # The stamp moved out of the project with the venv it describes (share slice 1).
    from procedo.studio import _project_local_state

    stamp = _project_local_state.install_record(root)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("{}", encoding="utf-8")
    assert w._bundle_stamp_changed() is True
    assert w._bundle_stamp_changed() is False


def test_dir_events_start_debounce_and_funnel_refreshes(make_window,
                                                        tmp_path):
    # Pins the watcher-event -> debounce -> reload funnel: a project-root
    # dir event with a moved bundle signature starts the 300ms single-shot
    # debounce timer (and re-arms the bundle watches); a config-dir event
    # with a new install stamp starts it too (belt-and-braces); an event
    # with NO change starts nothing. The debounced slot funnels into
    # _update_project_rules_indicators + _handle_config_change.
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)

    # no change -> no debounce
    w._on_config_dir_changed(str(root))
    assert not w._bundle_change_timer.isActive()

    os.utime(root / "bundle" / "bundle.json", (1_700_000_000, 1_700_000_456))
    w._on_config_dir_changed(str(root))
    assert w._bundle_change_timer.isActive()
    w._bundle_change_timer.stop()

    from procedo.studio import _project_local_state

    stamp = _project_local_state.install_record(root)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("{}", encoding="utf-8")
    w._on_config_dir_changed(str(root / "config"))
    assert w._bundle_change_timer.isActive()
    w._bundle_change_timer.stop()

    called = []
    w._update_project_rules_indicators = lambda: called.append("rules")
    w._handle_config_change = lambda: called.append("config")
    w._on_bundle_change_debounced()
    assert called == ["rules", "config"]


def test_handle_config_change_funnel(make_window, monkeypatch, tmp_path):
    # Pins _handle_config_change as THE single hot-reload funnel: regates
    # all three parser buttons, reloads TaskConfigManager with the project
    # root, refreshes the chat panel's validator UI for the current tab's
    # context — and NEVER lets a reload error escape (the watcher must not
    # die on a broken config.json).
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")

    calls = []
    w.text_json_tab.refresh_parser_button = lambda: calls.append("tj")
    w.text_only_tab.refresh_parser_button = lambda: calls.append("to")
    w.json_code_tab.refresh_code_parser_button = lambda: calls.append("jc")
    monkeypatch.setattr(w.task_config_manager, "reload",
                        lambda r: calls.append(("reload", r)))
    monkeypatch.setattr(w.dock.chat_panel, "_refresh_validator_ui_for_context",
                        lambda ctx: calls.append(("validator_ui", ctx)))

    w._handle_config_change()

    assert calls == [
        "tj", "to", "jc", ("reload", root),
        ("validator_ui", w.text_only_tab.tab_context)]

    # A reload failure is swallowed (logged), never raised.
    def _boom(r):
        raise RuntimeError("broken config.json")
    monkeypatch.setattr(w.task_config_manager, "reload", _boom)
    w._handle_config_change()  # must not raise


# =========================================================================
# 9. settings dialog wiring
# =========================================================================

def test_settings_dialog_accept_and_cancel_wiring(make_window, monkeypatch,
                                                  tmp_path):
    # Pins _on_settings: the dialog is constructed with (task_config_manager,
    # window, project_root=..., server_manager=...); on ACCEPT the returned
    # settings replace _settings, the backend is re-initialized, the netlist
    # reloads and button labels refresh; on CANCEL nothing happens.
    root = _make_project(tmp_path / "proj")
    w = make_window()
    _open_project(w, root)

    created = []

    class FakeSettingsDialog:
        exec_result = True
        new_settings = {"llm_backend": "none", "marker": 42}

        def __init__(self, task_config_manager, parent, project_root=None,
                     server_manager=None):
            self.args = (task_config_manager, parent, project_root,
                         server_manager)
            created.append(self)

        def exec(self):
            return type(self).exec_result

        def get_settings(self):
            return dict(type(self).new_settings)

    monkeypatch.setattr(mw_mod, "SettingsDialog", FakeSettingsDialog)
    events = []
    orig_init = w._init_llm_backend
    w._init_llm_backend = lambda: (events.append("init_backend"),
                                   orig_init())[1]
    w.refresh_all_button_labels = lambda: events.append("labels")
    w.text_only_tab.reload_netlist = lambda: events.append("netlist")

    w._on_settings()

    assert created[0].args == (w.task_config_manager, w, root,
                               w._server_manager)
    assert w._settings == {"llm_backend": "none", "marker": 42}
    assert events == ["init_backend", "netlist", "labels"]

    FakeSettingsDialog.exec_result = False
    events.clear()
    w._on_settings()
    assert events == []
    assert w._settings == {"llm_backend": "none", "marker": 42}


# =========================================================================
# 10. unsaved-changes prompt + close flow
# =========================================================================

def test_check_unsaved_changes_prompt_paths(make_window, mbox, tmp_path):
    # Pins _check_unsaved_changes: clean -> no prompt; dirty (live editor
    # content synced from the CURRENT tab only) -> Save|Discard|Cancel
    # question naming the dirty file; Cancel returns True (abort), Discard
    # returns False leaving disk untouched, Save returns False after
    # writing via _on_save_all.
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)

    assert w._check_unsaved_changes() is False
    assert mbox.question == []

    w.text_only_tab.text_editor.setPlainText("# t1\n\nDirty body.\n")

    mbox.question_answer = QMessageBox.StandardButton.Cancel
    assert w._check_unsaved_changes() is True
    mbox.question_answer = QMessageBox.StandardButton.Discard
    assert w._check_unsaved_changes() is False
    assert "Dirty body." not in \
        (t1 / "procedure_text.md").read_text(encoding="utf-8")

    mbox.question_answer = QMessageBox.StandardButton.Save
    assert w._check_unsaved_changes() is False
    assert "Dirty body." in \
        (t1 / "procedure_text.md").read_text(encoding="utf-8")

    assert [t for t, _ in mbox.question] == ["Unsaved Changes"] * 3
    assert all("procedure_text.md" in text for _, text in mbox.question)


def test_close_event_cancel_keeps_window_clean_accepts(make_window, mbox,
                                                       tmp_path):
    # Pins closeEvent: with unsaved changes and the user answering Cancel
    # the event is IGNORED (window survives); once clean it is accepted.
    # Also pins that the session state is saved on close.
    root = _make_project(tmp_path / "proj")
    t1 = root / "tests" / "t1"
    w = make_window()
    _open_project(w, root)
    w.open_test(t1)
    w.text_only_tab.text_editor.setPlainText("# t1\n\nNot saved yet.\n")

    mbox.question_answer = QMessageBox.StandardButton.Cancel
    ev = QCloseEvent()
    w.window().closeEvent(ev)
    assert not ev.isAccepted()

    mbox.question_answer = QMessageBox.StandardButton.Save
    ev2 = QCloseEvent()
    w.window().closeEvent(ev2)
    assert ev2.isAccepted()
    assert "Not saved yet." in \
        (t1 / "procedure_text.md").read_text(encoding="utf-8")
    assert (t1 / ".llm_session.json").exists()


def test_open_other_test_with_unsaved_changes_cancel_aborts(make_window,
                                                            mbox, tmp_path):
    # Pins the switch-away guard in _on_test_opened: Cancel on the unsaved
    # prompt keeps the ORIGINAL test loaded (managers untouched); the
    # workspace signal is effectively rolled back.
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    am_before = w.artifact_manager
    w.text_only_tab.text_editor.setPlainText("# t1\n\nEdited, unsaved.\n")

    mbox.question_answer = QMessageBox.StandardButton.Cancel
    w.open_test(root / "tests" / "t2")

    assert w.artifact_manager is am_before
    assert w.artifact_manager.test_dir == root / "tests" / "t1"
    assert w.header.title.text() == "t1"


# =========================================================================
# 11. test deletion clears the loaded state
# =========================================================================

def test_test_deleted_clears_loaded_state(make_window, tmp_path):
    # Pins _on_test_deleted for the CURRENTLY-open test: managers dropped,
    # all five editors cleared, views+Assistant disabled, header reset.
    # NOTE (suspected staleness, pinned by absence): the tab CONTEXTS keep
    # their references to the dead ArtifactManager/SessionState — only the
    # next _on_test_opened rewires them.
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    ctx_am = w.text_only_tab.tab_context.artifact_manager

    w._on_test_deleted(root / "tests" / "t1")

    assert w.artifact_manager is None
    assert w.session_state is None
    assert w.text_only_tab.text_editor.toPlainText() == ""
    assert w.text_json_tab.text_editor.toPlainText() == ""
    assert w.text_json_tab.json_editor.toPlainText() == ""
    assert w.json_code_tab.json_editor.toPlainText() == ""
    assert w.json_code_tab.code_editor.toPlainText() == ""
    assert not w.view_column.isEnabled()
    assert not w.dock.isEnabled()
    assert w.header.title.text() == "No test open"
    # current behavior: tab contexts still hold the retired manager
    assert w.text_only_tab.tab_context.artifact_manager is ctx_am

    # Deleting a NON-open test is a status-line-only event.
    w.open_test(root / "tests" / "t2")
    w._on_test_deleted(root / "tests" / "t1")
    assert w.artifact_manager is not None
    assert w.header.title.text() == "t2"
