"""Studio's engine calls never freeze the window (docs/plans/engine_process.md slice 5): every
button that asks the project's TOOLS engine (⚡ Parse / Render / Code, Renumber, Sync Equipment,
Sync Meta), the reconstruction of a text proposal, the save's media refresh and the wizards' parse
and meta sync run the round trip off the GUI thread. While a button's call runs, the button is
disabled and a second press starts nothing; a result that replaces a text applies only if the
text is still the one sent; an engine error shows as it always did; a save writes the file
before its media are refreshed."""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QTimer

import procedo.studio.tabs.text_actions as text_actions
from procedo.studio.authoring.wizard_api import StudioAuthoringApi
from procedo.studio.core import ArtifactType
from procedo.studio.llm import reconstruction
from procedo.studio.pack_ops import engine

from .test_proposal_cards import _cards, _llm, opened as opened  # noqa: F401
from .test_main_window_characterization import (  # noqa: F401
    make_window as make_window, mbox as mbox, qapp as qapp,
)

STALE = "The text changed while it ran; run it again."
PROPOSED = "# t1\n\nBody of t1.\n\n## Steps\n1. Set PSU1 CH1 voltage = 28 V.\n"


def _answer(spec):
    """What the fake TOOLS engine answers per op (the shapes pack_parsers maps)."""
    op = spec["op"]
    if op == "parse_text":
        return {"ok": True, "json": {"id": "t1", "steps": [{"n": 1}]}, "warnings": []}
    if op == "render_text":
        return {"ok": True, "text": "# t1\n\nRendered.\n"}
    if op == "generate_code":
        return {"ok": True, "code": "def main():\n    return 1\n", "warnings": []}
    if op in ("renumber_steps", "sync_equipment", "sync_meta"):
        return {"ok": True, "text": spec["text"] + f"{op}\n", "warnings": []}
    if op == "refresh_media":
        return {"ok": True, "json": {**spec["procedure_json"], "media": "refreshed"}}
    if op == "section_ownership":
        return {"ok": True, "ownership": {"Equipment": "llm", "Steps": "llm", "Meta": "parser"}}
    if op == "reconstruct":
        return {"ok": True, "success": True, "text": spec["fragment"], "findings": []}
    raise AssertionError(f"unexpected op {op}")


class _Tools:
    """The project's TOOLS engine, slow: each op takes ``delay`` s, and the ``gated`` ones wait
    for release(); records each op and the thread that asked it."""

    def __init__(self, delay=0.0, gated=(), reply=_answer):
        self.delay, self.gated, self.reply = delay, gated, reply
        self.ops, self.threads, self.seen = [], [], []
        self.entered, self.returned, self._go = threading.Event(), threading.Event(), threading.Event()

    def __call__(self, project_root, spec, timeout):
        if spec["op"] == "is_available":  # the views' gating probe (cached, not this slice's)
            return {"ok": True, "available": True, "reason": ""}
        self.ops.append(spec["op"])
        self.threads.append(threading.get_ident())
        self.entered.set()
        if spec["op"] in self.gated:
            assert self._go.wait(10)
        time.sleep(self.delay)
        try:
            return self.reply(spec)
        finally:
            self.returned.set()

    def release(self):
        self._go.set()


def _pump(until, timeout=10):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.002)
    assert until()


def _view(w, name):
    view = getattr(w, name)
    w.view_stack.setCurrentWidget(view)
    return view


# --- each caller: off the GUI thread, the window keeps painting ---------------------------------

def _text_after(view, op):
    return lambda: view.text_card.editor.toPlainText().endswith(f"{op}\n")


def _saved_media(w):
    path = w.root / "tests" / "t1" / "procedure.json"
    return lambda: json.loads(path.read_text(encoding="utf-8")).get("media") == "refreshed"


def _case(w, name):
    """(start the caller, has its result landed) for each caller."""
    tj, jc = w.text_json_tab, w.json_code_tab
    one_card = lambda: len(_cards(w)) == 1  # noqa: E731
    if name == "parse":
        return _view(w, "text_json_tab")._on_quick_parse, one_card
    if name == "render":
        return _view(w, "text_json_tab")._on_quick_render, one_card
    if name == "code":
        return _view(w, "json_code_tab")._on_quick_code, one_card
    if name in ("renumber_steps", "sync_equipment", "sync_meta"):
        handler = {"renumber_steps": "_on_renumber_steps", "sync_equipment": "_on_sync_equipment",
                   "sync_meta": "_on_sync_meta"}[name]
        return getattr(_view(w, "text_json_tab"), handler), _text_after(tj, name)
    if name == "parse_generate":
        return _view(w, "text_only_tab")._on_parse_and_generate, one_card
    if name == "reconstruct":
        view = _view(w, "text_only_tab")
        return lambda: view._handle_text_proposal(_llm(PROPOSED)), one_card
    if name == "save":
        _view(w, "json_code_tab")
        jc.json_card.set_text(json.dumps({"id": "t1", "steps": [{"n": 1}]}, indent=2))
        return jc.json_card.save, _saved_media(w)
    raise AssertionError(name)


@pytest.fixture
def real_reconstruct(monkeypatch):
    # ``opened`` hands proposals back as they are; here the real reconstruction asks the engine.
    monkeypatch.setattr(text_actions, "reconstructed_or_error", reconstruction.reconstructed_or_error)


def _longest_pause(start, landed):
    """The longest gap between the ticks of a 10 ms timer from just before ``start()`` until the
    result landed (the GUI thread's longest freeze)."""
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    _pump(lambda: len(ticks) >= 3)
    start()
    _pump(landed)
    begin = len(ticks)
    _pump(lambda: len(ticks) > begin + 1)
    timer.stop()
    return max(b - a for a, b in zip(ticks, ticks[1:]))


@pytest.mark.parametrize("name", ["parse", "render", "code", "parse_generate", "renumber_steps",
                                  "sync_equipment", "sync_meta", "reconstruct", "save"])
def test_each_engine_call_runs_off_the_gui_thread(opened, monkeypatch, real_reconstruct, name):
    w = opened
    tools = _Tools(delay=0.5)
    monkeypatch.setattr(engine, "tools_op", tools)
    start, landed = _case(w, name)

    pause = _longest_pause(start, landed)

    assert tools.ops and threading.main_thread().ident not in tools.threads
    assert pause < 0.25, f"the window froze {pause * 1000:.0f} ms during a 500 ms engine op"


def test_the_wizards_parse_and_meta_sync_run_off_the_gui_thread(qapp, tmp_path, monkeypatch):
    (tmp_path / "A" / "tests").mkdir(parents=True)
    pm = SimpleNamespace(project_root=tmp_path / "A")
    api = StudioAuthoringApi(SimpleNamespace(project_manager=pm, backend_factory=None))
    tools = _Tools(delay=0.5)
    monkeypatch.setattr(engine, "tools_op", tools)
    out = {}

    pause = _longest_pause(lambda: out.update(meta=api.sync_meta_text("# A\n"),
                                              parsed=api.parse_procedure_text("# A\n")),
                           lambda: "parsed" in out)

    assert out == {"meta": "# A\nsync_meta\n", "parsed": {"id": "t1", "steps": [{"n": 1}]}}
    assert tools.ops == ["sync_meta", "parse_text"]
    assert threading.main_thread().ident not in tools.threads
    assert pause < 0.25, f"the window froze {pause * 1000:.0f} ms during a 500 ms engine op"


def test_the_wizards_api_raises_the_engines_error(qapp, tmp_path, monkeypatch):
    from procedo.studio.pack_ops import engine_session
    pm = SimpleNamespace(project_root=tmp_path)
    api = StudioAuthoringApi(SimpleNamespace(project_manager=pm, backend_factory=None))
    monkeypatch.setattr(engine, "tools_op", _Tools(
        reply=lambda spec: {"ok": False, "kind": "ParserUnavailable", "error": "no venv here"}))
    with pytest.raises(engine_session.ParserUnavailable, match="no venv here"):
        api.parse_procedure_text("# A\n")


# --- a text-replacing result applies only to the text it was made from -------------------------

#: The op each case waits in (only that one: an edit's gating may ask the engine meanwhile).
_OP = {"parse": "parse_text", "parse_generate": "parse_text"}

@pytest.mark.parametrize("name", ["renumber_steps", "sync_equipment", "sync_meta", "reconstruct",
                                  "parse", "parse_generate"])
def test_an_edit_during_the_call_drops_its_result_and_says_so(
        opened, monkeypatch, real_reconstruct, name):
    w = opened
    tools = _Tools(gated=(_OP.get(name, name),))
    monkeypatch.setattr(engine, "tools_op", tools)
    start, _landed = _case(w, name)
    view = w.view_stack.currentWidget()
    said = []
    view.status_message.connect(said.append)

    start()
    assert tools.entered.wait(5)
    view.text_card.editor.setPlainText("# t1\n\nEdited meanwhile.\n")
    tools.release()
    _pump(lambda: STALE in said)

    assert view.text_card.editor.toPlainText() == "# t1\n\nEdited meanwhile.\n"
    assert not _cards(w)


# --- no double submit -------------------------------------------------------------------------

def test_a_button_is_disabled_while_its_call_runs_and_a_second_press_starts_nothing(
        opened, monkeypatch):
    w = opened
    tools = _Tools(gated=("sync_equipment",))
    monkeypatch.setattr(engine, "tools_op", tools)
    view = _view(w, "text_json_tab")
    button = view.sync_equipment_btn

    view._on_sync_equipment()
    assert tools.entered.wait(5)
    assert not button.isEnabled()
    view._on_sync_equipment()
    tools.release()
    _pump(_text_after(view, "sync_equipment"))

    assert tools.ops == ["sync_equipment"]
    assert button.isEnabled()
    assert view.text_card.editor.toPlainText().count("sync_equipment") == 1


def test_a_gated_button_stays_disabled_while_its_call_runs(opened, monkeypatch):
    w = opened
    tools = _Tools(gated=("parse_text",))
    monkeypatch.setattr(engine, "tools_op", tools)
    view = _view(w, "text_json_tab")
    monkeypatch.setattr(type(view), "_pack_gates", lambda self: [(view.quick_parse_btn, "tip", ())])

    view._on_quick_parse()
    assert tools.entered.wait(5)
    view._apply_capability_gating(w.root)  # what a keystroke does
    assert not view.quick_parse_btn.isEnabled()
    tools.release()
    _pump(lambda: len(_cards(w)) == 1)
    assert view.quick_parse_btn.isEnabled()


# --- errors show as they always did ---------------------------------------------------------

def _unavailable(spec):
    return {"ok": False, "kind": "ParserUnavailable", "error": "no venv here"}


@pytest.mark.parametrize("name, title, kind", [
    ("sync_equipment", "Sync Equipment failed", "critical"),
    ("renumber_steps", "Renumber failed", "critical"),
    ("sync_meta", "Sync Meta failed", "critical"),
    ("parse", "Parser Unavailable", "warning"),
    ("render", "Parser Unavailable", "warning"),
    ("code", "Code Generator Unavailable", "warning"),
])
def test_an_engine_error_shows_as_it_always_did(opened, monkeypatch, mbox, name, title, kind):
    w = opened
    monkeypatch.setattr(engine, "tools_op", _Tools(reply=_unavailable))
    start, _landed = _case(w, name)
    before = w.text_json_tab.text_card.editor.toPlainText()

    start()
    _pump(lambda: getattr(mbox, kind))

    assert getattr(mbox, kind) == [(title, "no venv here")]
    assert w.text_json_tab.text_card.editor.toPlainText() == before
    assert not _cards(w)


def test_a_parse_failure_opens_the_findings_dialog(opened, monkeypatch):
    from procedo.studio.dialogs.validator_error_dialog import ValidatorErrorDialog
    w = opened
    shown = []
    monkeypatch.setattr(ValidatorErrorDialog, "show_from_exception",
                        lambda exc, **kw: shown.append((type(exc).__name__, kw["title"])))
    monkeypatch.setattr(engine, "tools_op", _Tools(reply=lambda spec: {
        "ok": False, "kind": "ParseFailure", "error": "bad", "errors": [], "findings": []}))
    start, _landed = _case(w, "parse")

    start()
    _pump(lambda: shown)

    assert shown == [("ParseFailure", "Quick Parse — validator findings")]


# --- the save writes the file first ---------------------------------------------------------

def test_the_save_writes_the_file_before_its_media_are_refreshed(opened, monkeypatch):
    w = opened
    path = w.root / "tests" / "t1" / "procedure.json"
    on_disk = []

    def reply(spec):
        on_disk.append(path.read_text(encoding="utf-8"))
        return _answer(spec)

    monkeypatch.setattr(engine, "tools_op", _Tools(delay=0.2, reply=reply))
    start, landed = _case(w, "save")
    card = w.json_code_tab.json_card

    start()
    written = path.read_text(encoding="utf-8")
    _pump(landed)

    assert json.loads(written) == {"id": "t1", "steps": [{"n": 1}]}
    assert on_disk == [written]
    refreshed = path.read_text(encoding="utf-8")
    assert json.loads(refreshed)["media"] == "refreshed"
    assert card.editor.toPlainText() == refreshed
    assert not card.dirty


def test_an_edit_typed_while_the_media_refresh_runs_stays_unsaved(opened, monkeypatch):
    w = opened
    path = w.root / "tests" / "t1" / "procedure.json"
    tools = _Tools(gated=("refresh_media",))
    monkeypatch.setattr(engine, "tools_op", tools)
    start, _landed = _case(w, "save")
    card = w.json_code_tab.json_card

    start()
    written = path.read_text(encoding="utf-8")
    assert tools.entered.wait(5)
    card.editor.setPlainText('{"id": "t1", "typed": true}')
    tools.release()
    assert tools.returned.wait(5)
    end = time.monotonic() + 0.2  # the answer's delivery
    _pump(lambda: time.monotonic() > end)

    assert path.read_text(encoding="utf-8") == written
    assert card.editor.toPlainText() == '{"id": "t1", "typed": true}'
    assert card.dirty
    assert w.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON) == '{"id": "t1", "typed": true}'


def test_a_refresh_landing_after_a_test_switch_leaves_the_new_tests_editor_alone(
        opened, monkeypatch):
    w = opened
    tools = _Tools(gated=("refresh_media",))
    monkeypatch.setattr(engine, "tools_op", tools)
    start, landed = _case(w, "save")

    start()  # t1's procedure.json saved, its media refresh still running
    assert tools.entered.wait(5)
    w.open_test(w.root / "tests" / "t2")
    t2_json = w.json_code_tab.json_card.editor.toPlainText()
    tools.release()
    _pump(landed)  # t1's file got its refreshed media
    end = time.monotonic() + 0.2
    _pump(lambda: time.monotonic() > end)

    for tab in w._get_llm_tabs():
        for card in tab.cards:
            if card.kind is ArtifactType.PROCEDURE_JSON:
                assert card.editor.toPlainText() == t2_json
    assert "refreshed" not in t2_json


# --- delivery ---------------------------------------------------------------------------------

_IMPORTED_ON_A_WORKER = """
import os, sys, threading, time
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
importer = threading.Thread(target=lambda: __import__("procedo.studio.pack_ops.engine_async"))
importer.start()
importer.join()
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from procedo.studio.pack_ops import engine_async
got = []
engine_async.call(Path("."), lambda: 7,
                  lambda a: got.append((a(), threading.current_thread() is threading.main_thread())))
end = time.monotonic() + 10
while not got and time.monotonic() < end:
    app.processEvents()
    time.sleep(0.005)
print(got)
"""


def test_answers_reach_the_gui_thread_when_a_worker_imported_the_module_first():
    """A lazy import may run on a worker (artifact_manager, MCP threads): the module's first
    import must not tie the answers to that thread."""
    import os
    import subprocess
    import sys
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    out = subprocess.run([sys.executable, "-c", _IMPORTED_ON_A_WORKER], capture_output=True,
                         text=True, timeout=60, env=env).stdout
    assert out.strip().splitlines()[-1] == "[(7, True)]"


def test_an_answer_for_a_button_destroyed_meanwhile_still_lands(qapp, tmp_path):
    import shiboken6
    from PySide6.QtWidgets import QPushButton
    from procedo.studio.pack_ops import engine_async
    from procedo.core.ui_kit import LoadingCue
    button, cue, go, answered = QPushButton("Sync"), LoadingCue(), threading.Event(), []

    engine_async.call(tmp_path, lambda: go.wait(5), answered.append, button=button, cue=cue)
    shiboken6.delete(button)  # the button's card closed while the call ran; the cue lives on
    go.set()
    _pump(lambda: answered)

    assert answered[0]() is True and not engine_async.busy(button) and not cue.active


# --- the loading cue where the answer will show (owner 2026-10-23) --------------------------

_CUES = {  # caller -> (the view's attribute holding the cue's owner, its card or the window, label)
    "parse": ("text_json_tab", "json_card", "Parsing…"),
    "render": ("text_json_tab", "text_card", "Rendering…"),
    "code": ("json_code_tab", "code_card", "Generating code…"),
    "renumber_steps": ("text_json_tab", "text_card", "Renumbering steps…"),
    "sync_equipment": ("text_json_tab", "text_card", "Syncing equipment…"),
    "sync_meta": ("text_json_tab", "text_card", "Syncing meta…"),
    "parse_generate": ("text_only_tab", "text_card", "Parsing…"),
    "reconstruct": ("text_only_tab", "text_card", "Preparing the proposal…"),
    "save": (None, None, "Refreshing media…"),
}
_OPS = {"parse": "parse_text", "render": "render_text", "code": "generate_code", "parse_generate": "parse_text",
        "reconstruct": "reconstruct", "save": "refresh_media"}


@pytest.mark.parametrize("name", sorted(_CUES))
def test_each_engine_call_says_it_runs_where_its_answer_will_show(opened, monkeypatch, real_reconstruct, name):
    w = opened
    tools = _Tools(gated=(_OPS.get(name, name),))
    monkeypatch.setattr(engine, "tools_op", tools)
    start, landed = _case(w, name)
    view, card, label = _CUES[name]
    cue = getattr(getattr(w, view), card).loading if view else w.loading  # the save's: the footer's

    start()
    assert tools.entered.wait(5)
    assert cue.active and cue.label.text() == label
    tools.release()
    _pump(landed)
    _pump(lambda: not cue.active)


def test_a_failed_engine_call_takes_its_cue_away(qapp, tmp_path):
    from procedo.core.ui_kit import LoadingCue
    from procedo.studio.pack_ops import engine_async
    cue, answered = LoadingCue(), []

    def broken():
        raise RuntimeError("engine down")

    engine_async.call(tmp_path, broken, answered.append, cue=cue, cue_label="Parsing…")
    assert cue.active
    _pump(lambda: answered)
    assert not cue.active
    with pytest.raises(RuntimeError, match="engine down"):
        answered[0]()


def test_a_session_compaction_shows_in_the_footer_until_it_answers(opened, monkeypatch):
    w = opened
    go = threading.Event()
    tab = SimpleNamespace(tab_context=SimpleNamespace(backend=SimpleNamespace(compact=lambda: go.wait(5))))
    monkeypatch.setattr(w.view_stack, "currentWidget", lambda: tab)
    w._assistant_controller.on_compact_session()
    assert w.loading.active and w.loading.label.text() == "Compacting session…"
    go.set()
    _pump(lambda: not w.loading.active)
