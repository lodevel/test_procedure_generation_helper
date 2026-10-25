"""A send that needs the AI server never waits for its start on the GUI thread: the start runs on
a worker, ONE attempt shared by every tab and skill chat asking meanwhile; the chat shows the
loading cue ("Starting the AI server…") and the send goes once the server is up, or is answered
with the server's failure. Closing Studio during a start leaves no server process and no pid
file. Never a real ``opencode`` server: the manager is a fake, or the real one on a fake spawn."""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from procedo.studio.dock import skill_chat_widget as scw
from procedo.studio.dock.backend_request import request_backend
from procedo.studio.llm import backend_factory
from procedo.studio.llm.backend_base import LLMTask, NoneBackend
from procedo.studio.llm.backend_factory import BACKEND_TYPE_OPENCODE, BackendConfig, BackendFactory
from procedo.studio.llm.opencode_backend import OpenCodeConfig
from procedo.studio.tabs import llm_tab_mixin

from .test_lazy_backend import _Answers
from .test_lazy_backend import skill_chat as skill_chat
from .test_main_window_characterization import _make_project, _open_project
from .test_main_window_characterization import make_window as make_window
from .test_main_window_characterization import mbox as mbox
from .test_main_window_characterization import qapp as qapp
from .test_proposal_cards import opened as opened
from .test_server_lifecycle import _fake_spawn, _mgr, _pid_file, _write_record

STARTING = "Starting the AI server…"
DOWN = "The OpenCode server did not start in time."
#: How long the fake server takes to start (the real one: up to its 60 s timeout).
START_S = 1.0


class _SlowServer:
    """A fake server manager whose start takes :data:`START_S` and then succeeds (or not)."""

    def __init__(self, ok: bool = True):
        self.ok, self.up, self.starts, self.relaunched = ok, False, 0, 0
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

    def relaunch(self):
        self.relaunched += 1


class _Backend:
    """The OpenCode backend a started server gives (no network)."""

    def __init__(self, **_config):
        self.started = self.stopped = False
        _Backend.made.append(self)

    def start(self):
        self.started = True
        return True

    def get_context_window(self):
        return None

    def stop(self):
        self.stopped = True


class _Worker:
    """The LLM worker: records its backend; a ``NoneBackend``'s reply (the server's failure) is
    delivered on the GUI thread, as the real worker's is; any other never answers."""

    def __init__(self, backend, request, parent=None):
        self.backend, self.request, self.slots = backend, request, {}
        for name in ("finished", "error", "thinking_chunk", "text_chunk"):
            setattr(
                self,
                name,
                SimpleNamespace(connect=lambda slot, name=name: self.slots.__setitem__(name, slot)),
            )

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
    _Backend.made = []
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
    opened, skill_chat, monkeypatch
):
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
    queued = (
        _Worker.made == [],
        cue.active,
        cue.label.text(),
        chat._loading.active,
        chat._loading.label.text(),
    )
    _spin(lambda: len(_Worker.made) == 2)
    timer.stop()

    assert _longest_gap(stamps) < START_S / 2  # measured: the GUI thread kept ticking
    assert server.starts == 1
    assert queued == (True, True, STARTING, True, STARTING)
    assert not cue.active  # the chat's cue went with the start
    assert chat._loading.label.text() == "Working…"  # the turn runs now
    sent = {id(worker.backend) for worker in _Worker.made}
    assert sent == {id(view.tab_context.backend), id(chat._llm.backend)}
    assert all(
        isinstance(worker.backend, _Backend) and worker.backend.started for worker in _Worker.made
    )


def test_a_failed_start_answers_the_queued_send_with_the_server_s_failure(
    opened, monkeypatch, mbox
):
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


def test_studio_closed_during_a_start_leaves_no_server_and_no_pid_file(qapp, tmp_path):
    """The exit hook stops the manager while the shared start still waits for the server: the
    start gives up, and the server it spawned is stopped, its pid file removed."""
    from procedo.studio.controllers.llm_server_controller import LLMServerController

    mgr = _mgr(tmp_path)
    factory = BackendFactory(
        BackendConfig(backend_type=BACKEND_TYPE_OPENCODE, opencode=mgr.config), mgr
    )
    proc = _fake_spawn(["__OC_PID__31337\n"])
    healthy = MagicMock()
    healthy.ok = True
    answers = _Answers()
    with (
        patch.object(mgr, "_diagnose_installation", return_value=healthy),
        patch("procedo.studio.llm.server_manager.find_free_port", return_value=5005),
        patch(
            "procedo.studio.llm.server_manager.subprocess.Popen",
            side_effect=lambda *a, **k: _write_record(mgr, 31337) or proc,
        ),
        patch("procedo.studio.llm.server_manager.subprocess.run", return_value=MagicMock()),
        patch(
            "procedo.studio.llm.server_manager.requests.get",
            side_effect=requests.exceptions.ConnectionError(),
        ),
    ):
        request_backend(factory, "text_json", answers.take)
        deadline = time.monotonic() + 10
        while mgr._spawned is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mgr._await_spawned() == (31337, 5005)  # spawned, still starting
        assert mgr._orphans.read_pid_file()["pid"] == 31337

        LLMServerController._stop_current_server(SimpleNamespace(server_manager=mgr))
        _spin(lambda: answers.got)

    ((_factory_, backend, keep),) = answers.got
    assert isinstance(backend, NoneBackend) and not keep
    proc.terminate.assert_called()
    assert mgr._server_process is None and not mgr.is_running
    assert mgr._orphans.read_pid_file() is None and not _pid_file(tmp_path).exists()


# --- what the user sent always stays in the chat -------------------------------------------


def _lines(view):
    return [m.content for m in view.tab_context.messages]


def test_a_send_a_test_switch_dropped_stays_in_the_chat_and_says_why(opened, monkeypatch):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    view.tab_context.update_backend_factory(_factory(monkeypatch, _SlowServer()))

    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")
    shown = _lines(view)  # at once, while the server starts
    w.open_test(w.root / "tests" / "t2")
    _spin(lambda: not w.dock.chat_panel.loading.active)

    assert shown[-1] == "check U5"
    assert _lines(view)[-2:] == [
        "check U5",
        "Not sent: the test changed while the AI server was starting.",
    ]
    assert _Worker.made == []


def test_a_task_that_replaces_a_waiting_send_says_so_and_goes_once(opened, monkeypatch):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    server = _SlowServer()
    view.tab_context.update_backend_factory(_factory(monkeypatch, server))
    review = view._get_task_description(LLMTask.REVIEW_TEXT_PROCEDURE)

    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # a task button, while it waits
    shown = _lines(view)
    _spin(lambda: _Worker.made)

    assert shown[-3:] == ["check U5", f"Not sent: replaced by {review}.", review]
    assert _lines(view)[-3:] == shown[-3:]  # the review's line is not added twice
    assert server.starts == 1 and len(_Worker.made) == 1
    assert _Worker.made[0].request.task == LLMTask.REVIEW_TEXT_PROCEDURE
    assert view.tab_context.messages[-1].full_prompt == "PROMPT"


# --- a waiting turn: Stop, Restart, close; a Settings save; a failed request ---------------


def _system_lines(chat):
    return [text for speaker, text in chat._blocks if speaker == "System"]


def test_stop_while_the_server_starts_drops_the_turn_and_unlocks_the_chat(skill_chat, monkeypatch):
    chat = skill_chat(_factory(monkeypatch, _SlowServer()))
    failed = []
    chat.reply_failed.connect(failed.append)
    chat.send_user_turn("hello")
    assert chat._stop_btn.isEnabled()

    chat._stop_btn.click()

    reason = "Not sent: cancelled."
    assert failed == [reason] and _system_lines(chat)[-1] == reason
    assert ("You", "hello") in chat._blocks  # what was typed stays
    assert chat._send_btn.isEnabled() and not chat._loading.active
    _spin(lambda: chat._llm.backend is not None)  # the start goes on, for the next turn
    assert _Worker.made == []


def test_restart_while_the_server_starts_drops_the_turn(skill_chat, monkeypatch):
    server = _SlowServer()
    chat = skill_chat(_factory(monkeypatch, server))
    chat.send_user_turn("hello")

    chat._on_restart_backend()

    assert _system_lines(chat)[-1] == "Not sent: the AI server is restarting."
    assert not chat._loading.active and chat._send_btn.isEnabled()
    _spin(lambda: server.relaunched and not chat._llm._requested)
    assert _Worker.made == []


def test_a_chat_closed_while_the_server_starts_stops_what_it_answers(skill_chat, monkeypatch):
    server = _SlowServer()
    chat = skill_chat(_factory(monkeypatch, server))
    chat.send_user_turn("hello")
    chat.shutdown()
    _spin(lambda: not chat._llm._requested)

    assert chat._llm.backend is None and _Worker.made == []
    assert [b.stopped for b in _Backend.made] == [True]  # started, nobody's: stopped
    assert not chat._llm._starting.on  # its "Starting the AI server…" went with the close


def test_a_settings_save_during_the_start_sends_on_the_new_server(opened, monkeypatch, mbox):
    w = opened
    view = w.text_only_tab
    old, new = _SlowServer(), _SlowServer()
    view.tab_context.update_backend_factory(_factory(monkeypatch, old))
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)
    replacement = _factory(monkeypatch, new)
    view.tab_context.update_backend_factory(replacement)  # what a Settings save does

    _spin(lambda: _Worker.made)

    assert mbox.critical == [] and old.starts == new.starts == 1
    (worker,) = _Worker.made
    stale, fresh = _Backend.made  # the replaced factory's (stopped), the new one's
    assert worker.backend is fresh is view.tab_context.backend and fresh.started
    assert stale.stopped and not fresh.stopped
    assert not w.dock.chat_panel.loading.active


def test_a_request_that_fails_unlocks_the_chat_and_says_why(opened, monkeypatch):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    view.tab_context.update_backend_factory(_factory(monkeypatch, _SlowServer()))

    def refuse(factory, tab_id, on_answer):
        raise RuntimeError("no QApplication")

    monkeypatch.setattr(llm_tab_mixin, "request_backend", refuse)

    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")

    chat = w.dock.chat_panel
    assert chat.send_btn.isEnabled() and not chat.loading.active
    assert view._queued_send is None
    assert _lines(view)[-2:] == ["check U5", "Not sent: the AI server could not be started."]


def test_an_opencode_factory_without_a_server_manager_spawns_no_server():
    """The server manager is the one spawner: only it stops the server it started by its
    Linux pid, so a factory without one hands out the disabled backend instead."""
    factory = BackendFactory(
        BackendConfig(backend_type=BACKEND_TYPE_OPENCODE, opencode=OpenCodeConfig())
    )
    with (
        patch("opencode_client.lifecycle._health_ok", return_value=False),
        patch("opencode_client.lifecycle.subprocess.Popen") as popen,
    ):
        backend = factory.create_backend(tab_id="text_json")
        backend.start()
    popen.assert_not_called()
    assert isinstance(backend, NoneBackend)


def test_a_re_request_that_fails_unlocks_the_chat_and_says_why(opened, monkeypatch):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    view.tab_context.update_backend_factory(_factory(monkeypatch, _SlowServer()))
    calls = []

    def refuse_second(factory, tab_id, on_answer):
        calls.append(on_answer)
        if len(calls) > 1:
            raise RuntimeError("no QApplication")

    monkeypatch.setattr(llm_tab_mixin, "request_backend", refuse_second)
    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")
    monkeypatch.setattr(view.tab_context, "adopt_backend", lambda answer: None)  # factory replaced

    view._on_backend_started(object())

    chat = w.dock.chat_panel
    assert chat.send_btn.isEnabled() and not chat.loading.active
    assert view._queued_send is None
    assert _lines(view)[-2:] == ["check U5", "Not sent: the AI server could not be started."]


# --- a waiting send: Cancel, a second programmatic turn, a project switch -----------------


def test_cancel_while_the_server_starts_drops_the_send_and_the_next_waits_for_that_start(
    opened, monkeypatch
):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    server = _SlowServer()
    view.tab_context.update_backend_factory(_factory(monkeypatch, server))
    chat = w.dock.chat_panel

    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")
    assert chat.cancel_btn.isEnabled()
    chat.cancel_btn.click()

    assert _lines(view)[-2:] == ["check U5", "Not sent: cancelled."]
    assert view._queued_send is None and not chat.loading.active
    assert chat.send_btn.isEnabled() and not chat.cancel_btn.isEnabled()
    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U6")  # the start still runs
    _spin(lambda: _Worker.made)

    assert server.starts == 1 and len(_Backend.made) == 1 and len(_Worker.made) == 1
    assert _lines(view)[-3:] == ["check U5", "Not sent: cancelled.", "check U6"]


def test_cancel_on_another_view_drops_the_send_waiting_there(opened, monkeypatch):
    """The chat locks on every view: Cancel on JSON+Code drops the send Text is waiting with."""
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    view.tab_context.update_backend_factory(_factory(monkeypatch, _SlowServer()))
    chat = w.dock.chat_panel
    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")
    w.view_stack.setCurrentWidget(w.json_code_tab)

    chat.cancel_btn.click()

    assert _lines(view)[-2:] == ["check U5", "Not sent: cancelled."]
    assert view._queued_send is None and chat.send_btn.isEnabled() and not chat.loading.active


def test_restart_backend_says_the_waiting_send_is_not_sent_because_of_the_restart(
    opened, monkeypatch
):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    server = _SlowServer()
    view.tab_context.update_backend_factory(_factory(monkeypatch, server))
    w.llm_server.server_manager = server  # the server Restart restarts
    w._cli_args_processed = True
    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")

    w._assistant_controller.on_restart_backend()

    assert "Not sent: cancelled." not in _lines(view)
    assert "Not sent: the AI server is restarting." in _lines(view)[-3:]
    assert view._queued_send is None and not w.dock.chat_panel.loading.active
    _spin(lambda: not view._backend_requested)  # the old start answers: nobody's
    assert _Worker.made == []


def test_a_skill_chat_turn_waiting_for_the_server_is_never_replaced(skill_chat, monkeypatch):
    """A second programmatic turn meanwhile is refused (False), as Send is locked for the
    user: it replaced the waiting one silently, which then never went."""
    chat = skill_chat(_factory(monkeypatch, _SlowServer()))
    assert chat.send_user_turn("hello")

    assert chat.is_busy
    assert not chat.send_user_turn("again")
    _spin(lambda: _Worker.made)

    (worker,) = _Worker.made
    assert worker.request.raw_prompt.endswith("hello")
    assert ("You", "again") not in chat._blocks
    assert [turn.content for turn in chat._session.turns] == ["hello"]


def test_a_project_switch_drops_the_waiting_send_at_once(opened, monkeypatch, tmp_path):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    server = _SlowServer()
    view.tab_context.update_backend_factory(_factory(monkeypatch, server))
    w.llm_server.server_manager = server  # the server the switch restarts
    w._cli_args_processed = True
    chat = w.dock.chat_panel
    raised = []  # what a slot raises reaches only sys.excepthook
    monkeypatch.setattr(sys, "excepthook", lambda *exc: raised.append(exc))
    view._run_task_async(LLMTask.AD_HOC_CHAT, user_message="check U5")

    _open_project(w, _make_project(tmp_path / "other"))

    assert _lines(view)[-2:] == ["check U5", "Not sent: the AI server is restarting."]
    assert chat.send_btn.isEnabled() and not chat.loading.active
    _spin(lambda: not view._backend_requested)  # the old start answers: nobody's, stopped
    assert _Worker.made == [] and [b.stopped for b in _Backend.made] == [True]
    assert raised == []
    assert _lines(view)[-1] == "Not sent: the AI server is restarting."
