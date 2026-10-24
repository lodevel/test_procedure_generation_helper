"""A send that needs the AI server never waits for its start on the GUI thread: the start runs on
a worker, ONE attempt shared by every tab and skill chat asking meanwhile; the chat shows the
loading cue ("Starting the AI server…") and the send goes once the server is up, or is answered
with the server's failure. Closing Studio during a start leaves no server process and no pid
file. Never a real ``opencode`` server: the manager is a fake, or the real one on a fake spawn."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from procedo.studio.dock import skill_chat_widget as scw
from procedo.studio.llm import backend_factory
from procedo.studio.llm.backend_base import LLMTask, NoneBackend
from procedo.studio.llm.backend_factory import BACKEND_TYPE_OPENCODE, BackendConfig, BackendFactory
from procedo.studio.llm.opencode_backend import OpenCodeConfig
from procedo.studio.tabs import llm_tab_mixin

from .test_lazy_backend import skill_chat as skill_chat
from .test_main_window_characterization import make_window as make_window
from .test_main_window_characterization import mbox as mbox
from .test_main_window_characterization import qapp as qapp
from .test_proposal_cards import opened as opened
from .test_server_lifecycle import _fake_spawn, _mgr, _pid_file

STARTING = "Starting the AI server…"
DOWN = "The OpenCode server did not start in time."
#: How long the fake server takes to start (the real one: up to its 60 s timeout).
START_S = 1.0


class _SlowServer:
    """A fake server manager whose start takes :data:`START_S` and then succeeds (or not)."""

    def __init__(self, ok: bool = True):
        self.ok, self.up, self.starts = ok, False, 0
        self.last_status = None
        self.config = OpenCodeConfig()

    @property
    def is_running(self) -> bool:
        return self.up

    def start(self) -> bool:
        self.starts += 1
        time.sleep(START_S)
        self.up = self.ok
        if not self.ok:
            self.last_status = SimpleNamespace(message=DOWN)
        return self.ok


class _Backend:
    """The OpenCode backend a started server gives (no network)."""

    def __init__(self, **_config):
        self.started = False

    def start(self):
        self.started = True
        return True

    def get_context_window(self):
        return None

    def stop(self):
        pass


class _Worker:
    """The LLM worker: records its backend; a ``NoneBackend``'s reply (the server's failure) is
    delivered on the GUI thread, as the real worker's is; any other never answers."""

    def __init__(self, backend, request, parent=None):
        self.backend, self.request, self.slots = backend, request, {}
        for name in ("finished", "error", "thinking_chunk", "text_chunk"):
            setattr(self, name, SimpleNamespace(
                connect=lambda slot, name=name: self.slots.__setitem__(name, slot)))

    def start(self):
        _Worker.made.append(self)
        if isinstance(self.backend, NoneBackend):
            reply = self.backend.send_request(self.request)
            QTimer.singleShot(0, lambda: self.slots["finished"](reply))

    def isRunning(self):
        return False

    def cancel(self):
        pass

    def wait(self, _ms=None):
        return True

    def deleteLater(self):
        pass


def _factory(monkeypatch, server) -> BackendFactory:
    monkeypatch.setattr(backend_factory, "OpenCodeBackend", _Backend)
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Worker)
    monkeypatch.setattr(scw, "LLMWorker", _Worker)
    # The test project's bundle declares no prompt: the tab's prompt is this one.
    monkeypatch.setattr(llm_tab_mixin.PromptBuilder, "build", lambda self, request, **kw: "PROMPT")
    _Worker.made = []
    config = BackendConfig(backend_type=BACKEND_TYPE_OPENCODE, opencode=server.config)
    return BackendFactory(config, server)


def _ticking():
    """A 10 ms GUI timer and the times it ticked: the longest gap is how long the GUI thread
    was held."""
    stamps = [time.monotonic()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: stamps.append(time.monotonic()))
    timer.start()
    return timer, stamps


def _spin(until, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert until()


def _longest_gap(stamps) -> float:
    stamps = stamps + [time.monotonic()]
    return max(b - a for a, b in zip(stamps, stamps[1:]))


def test_two_sends_wait_for_one_server_start_without_holding_the_gui(
        opened, skill_chat, monkeypatch):
    w = opened
    view = w.text_only_tab
    server = _SlowServer()
    factory = _factory(monkeypatch, server)
    view.tab_context.update_backend_factory(factory)
    chat = skill_chat(factory)
    cue = w.dock.chat_panel.loading
    timer, stamps = _ticking()
    _spin(lambda: len(stamps) > 3)

    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # a tab send
    chat.send_user_turn("hello")  # a skill chat send, while the start runs
    queued = (_Worker.made == [], cue.active, cue.label.text(),
              chat._loading.active, chat._loading.label.text())
    _spin(lambda: len(_Worker.made) == 2)
    timer.stop()

    assert _longest_gap(stamps) < START_S / 2  # measured: the GUI thread kept ticking
    assert server.starts == 1
    assert queued == (True, True, STARTING, True, STARTING)
    assert not cue.active  # the chat's cue went with the start
    assert chat._loading.label.text() == "Working…"  # the turn runs now
    sent = {id(worker.backend) for worker in _Worker.made}
    assert sent == {id(view.tab_context.backend), id(chat._llm.backend)}
    assert all(isinstance(worker.backend, _Backend) and worker.backend.started
               for worker in _Worker.made)


def test_a_failed_start_answers_the_queued_send_with_the_server_s_failure(
        opened, monkeypatch, mbox):
    """One start for the send (the tab tried 2-3 times, each holding the GUI), its failure
    the reply, as before; the next send tries again."""
    w = opened
    view = w.text_only_tab
    server = _SlowServer(ok=False)
    view.tab_context.update_backend_factory(_factory(monkeypatch, server))
    timer, stamps = _ticking()

    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)
    _spin(lambda: mbox.critical)
    timer.stop()

    assert _longest_gap(stamps) < START_S / 2
    assert server.starts == 1
    assert mbox.critical == [("LLM Error", DOWN)]
    assert view.tab_context.messages[-1].content.endswith(DOWN)
    assert not w.dock.chat_panel.loading.active and view.tab_context.backend is None

    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)
    _spin(lambda: len(mbox.critical) == 2)
    assert server.starts == 2


def test_a_skill_chat_s_failed_start_is_its_reply(skill_chat, monkeypatch):
    server = _SlowServer(ok=False)
    chat = skill_chat(_factory(monkeypatch, server))
    failed = []
    chat.reply_failed.connect(failed.append)

    chat.send_user_turn("hello")
    assert chat._loading.label.text() == STARTING
    _spin(lambda: failed)

    assert failed == [DOWN] and server.starts == 1
    assert chat._llm.backend is None and not chat._loading.active


def test_studio_closed_during_a_start_leaves_no_server_and_no_pid_file(tmp_path):
    """The exit hook stops the manager while the shared start still waits for the server: the
    start gives up, and the server it spawned is stopped, its pid file removed."""
    from procedo.studio.controllers.llm_server_controller import LLMServerController

    mgr = _mgr(tmp_path)
    factory = BackendFactory(
        BackendConfig(backend_type=BACKEND_TYPE_OPENCODE, opencode=mgr.config), mgr)
    proc = _fake_spawn(["__OC_PID__31337\n"])
    healthy = MagicMock()
    healthy.ok = True
    answers = []
    with patch.object(mgr, "_diagnose_installation", return_value=healthy), \
         patch("procedo.studio.llm.server_manager.find_free_port", return_value=5005), \
         patch("procedo.studio.llm.server_manager.subprocess.Popen", return_value=proc), \
         patch("procedo.studio.llm.server_manager.subprocess.run", return_value=MagicMock()), \
         patch("procedo.studio.llm.server_manager.requests.get",
               side_effect=requests.exceptions.ConnectionError()):
        backend_factory.request_backend(factory, "text_json", answers.append)
        deadline = time.monotonic() + 10
        while mgr._read_pid_file() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mgr._read_pid_file()["pid"] == 31337  # spawned, still starting

        LLMServerController._stop_current_server(SimpleNamespace(server_manager=mgr))
        while not answers and time.monotonic() < deadline:
            time.sleep(0.01)

    (backend, keep), = answers
    assert isinstance(backend, NoneBackend) and not keep
    proc.terminate.assert_called()
    assert mgr._server_process is None and not mgr.is_running
    assert mgr._read_pid_file() is None and not _pid_file(tmp_path).exists()
