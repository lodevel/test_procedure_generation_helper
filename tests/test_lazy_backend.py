"""The backend start (``backend_factory.start_backend``) of a Studio tab and of a skill chat on
first use, one policy for both: a backend is created and started and kept, except a fallback
``NoneBackend`` (the server would not start): the next use tries the factory (and the server)
again; without a factory, a kept ``NoneBackend``. The start runs off the GUI thread
(``test_backend_start_off_gui``)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from procedo.studio.dock.backend_request import request_backend
from procedo.studio.llm.backend_base import NoneBackend
from procedo.studio.llm.backend_factory import start_backend
from procedo.studio.llm.tab_context import TabContext

from .test_main_window_characterization import qapp as qapp


class _Backend:
    def __init__(self):
        self.started = 0
        self.stopped = False

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped = True


class _Factory:
    def __init__(self, *made, backend_type="opencode"):
        self.made = list(made)
        self.asked = []
        self.backend_type = backend_type

    def create_backend(self, tab_id):
        self.asked.append(tab_id)
        return self.made.pop(0)


def _tab(factory):
    pm = SimpleNamespace(project_root=None)
    return TabContext("text_json", factory, pm, None, None)


def test_start_backend_creates_for_the_tab_id_and_starts_it():
    backend = _Backend()
    factory = _Factory(backend)
    assert start_backend(factory, "skill_chat") == (backend, True)
    assert factory.asked == ["skill_chat"] and backend.started == 1


class _Answers(QObject):
    """Takes the answers of a backend request (a QObject's bound method, as the tab's is)."""

    def __init__(self):
        super().__init__()
        self.got = []

    def take(self, answer):
        self.got.append(answer)

    def first(self):
        deadline = time.monotonic() + 5
        while not self.got and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        return self.got[0]


def _started(tab):
    """The tab's backend start, its answer adopted (as the tab does on the GUI thread)."""
    answers = _Answers()
    request_backend(tab.backend_factory, tab.tab_id, answers.take)
    return tab.adopt_backend(answers.first())


def test_a_tab_keeps_a_real_backend(qapp):
    backend = _Backend()
    tab = _tab(_Factory(backend))
    assert tab.backend is None
    assert _started(tab) is backend and tab.backend is backend
    assert backend.started == 1


def test_a_tab_retries_after_a_fallback_none_backend(qapp):
    fallback, real = NoneBackend(reason="server down"), _Backend()
    factory = _Factory(fallback, real)
    tab = _tab(factory)
    assert _started(tab) is fallback and tab.backend is None
    assert _started(tab) is real and tab.backend is real
    assert factory.asked == ["text_json", "text_json"]


def test_a_tab_keeps_the_none_backend_it_was_configured_with(qapp):
    """No server involved: answered at once."""
    chosen = NoneBackend()
    factory = _Factory(chosen, backend_type="none")
    tab = _tab(factory)
    answers = _Answers()
    request_backend(tab.backend_factory, tab.tab_id, answers.take)
    assert answers.got and tab.adopt_backend(answers.got[0]) is chosen and tab.backend is chosen
    assert len(factory.asked) == 1


def test_a_tab_without_a_factory_has_a_none_backend(qapp):
    tab = _tab(None)
    backend = _started(tab)
    assert isinstance(backend, NoneBackend) and tab.backend is backend


def test_a_backend_started_for_a_replaced_factory_is_stopped_never_used(qapp):
    """A Settings save during the start: the send asks the new factory."""
    backend = _Backend()
    tab = _tab(_Factory(backend, backend_type="none"))
    answers = _Answers()
    request_backend(tab.backend_factory, tab.tab_id, answers.take)
    tab.update_backend_factory(_Factory(_Backend()))
    assert tab.adopt_backend(answers.first()) is None and tab.backend is None
    assert backend.stopped


@pytest.fixture
def skill_chat(qapp, monkeypatch):
    """A skill chat over *factory* (its backend tab id ``my_wizard``)."""
    from procedo.studio.dock import skill_chat_widget as scw
    from .test_project_rules_skills import _delete_now
    from .test_skill_needed_context import _skill

    monkeypatch.setattr("procedo.studio.core.studio_settings.load_settings", lambda: {})
    made = []

    def make(factory, **kwargs):
        made.append(scw.SkillChatWidget([_skill()], factory, backend_tab_id="my_wizard", **kwargs))
        return made[-1]

    yield make
    for widget in made:
        widget.shutdown()
        _delete_now(widget)


def test_a_skill_chat_retries_after_a_fallback_none_backend(skill_chat):
    """It kept the fallback: "server down" on every send until Restart. A backend nobody
    keeps or waits for is stopped."""
    fallback, real, later = NoneBackend(reason="server down"), _Backend(), _Backend()
    factory = _Factory()
    llm = skill_chat(factory)._llm
    llm._on_started((factory, fallback, False))
    assert llm.backend is None
    llm._on_started((factory, real, True))
    llm._on_started((factory, later, True))  # a second start never replaces it
    assert llm.backend is real and not real.stopped and later.stopped


def test_a_skill_chat_without_a_factory_has_a_none_backend(skill_chat):
    """It failed with an AttributeError ("Failed to start — see logs.")."""
    fired = []
    llm = skill_chat(None)._llm
    llm.await_backend(fired.append)  # no server involved: fired at once
    assert isinstance(llm.backend, NoneBackend) and fired == [llm.backend]


def test_a_skill_chat_turn_queued_by_the_host_is_busy_and_never_doubled(skill_chat):
    """A turn the wizard's scheduler holds (``dispatch_gate``) read as not busy: a second
    programmatic turn was accepted, and the scheduler dropped it silently."""
    queued = []
    chat = skill_chat(_Factory(), dispatch_gate=lambda fire, *, interactive: queued.append(fire))
    assert chat.send_user_turn("hello")

    assert chat.is_busy
    assert not chat.send_user_turn("again")
    assert len(queued) == 1
    assert [turn.content for turn in chat._session.turns] == ["hello"]


def test_a_skill_chat_follows_a_replaced_factory(skill_chat):
    """It kept its creation-time factory: after a project switch its turns went to the
    retired server. A start the old factory still had out is stopped; the turn asks the new."""
    old_backend, stale, new_backend = _Backend(), _Backend(), _Backend()
    old, new = _Factory(), _Factory(new_backend, backend_type="none")
    llm = skill_chat(old)._llm
    llm._on_started((old, old_backend, True))
    llm.set_factory(new)
    assert old_backend.stopped and llm.backend is None

    fired = []
    llm._requested = True  # the old factory's start is still out
    llm.await_backend(fired.append)
    llm._on_started((old, stale, True))
    assert stale.stopped and not new_backend.stopped
    assert fired == [new_backend] and llm.backend is new_backend and new.asked == ["my_wizard"]


def test_stop_ends_a_turn_the_host_still_holds(skill_chat):
    """The wizard's Abandon dropped its queued fire and stopped the chat: no worker and
    nothing waiting for the server, so the chat stayed busy (locked) for good."""
    queued = []
    chat = skill_chat(_Factory(), dispatch_gate=lambda fire, *, interactive: queued.append(fire))
    went = []
    chat.busy_changed.connect(went.append)
    assert chat.send_user_turn("hello")

    chat.stop()

    assert not chat.is_busy and went == [True, False]
    assert chat._blocks[-1] == ("System", "Not sent: cancelled.") and chat._session.turns == []
    assert chat.send_user_turn("again") and len(queued) == 2


def test_a_turn_that_fails_to_start_once_the_backend_answers_unlocks_the_chat(
    skill_chat, monkeypatch
):
    factory = _Factory()
    chat = skill_chat(factory)
    failed = []
    chat.reply_failed.connect(failed.append)

    def broken(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat, "_fire", broken)
    chat._llm._requested = True  # a start is out: the turn waits for it
    assert chat.send_user_turn("hello") and chat.is_busy
    chat._llm._on_started((factory, _Backend(), True))

    assert not chat.is_busy and failed == ["Failed to start — see logs."]
