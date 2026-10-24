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

from procedo.studio.llm.backend_base import NoneBackend
from procedo.studio.llm.backend_factory import start_backend
from procedo.studio.llm.tab_context import TabContext

from .test_main_window_characterization import qapp as qapp


class _Backend:
    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1


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
    tab.request_backend(answers.take)
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
    tab.request_backend(answers.take)
    assert answers.got and tab.adopt_backend(answers.got[0]) is chosen and tab.backend is chosen
    assert len(factory.asked) == 1


def test_a_tab_without_a_factory_has_a_none_backend(qapp):
    tab = _tab(None)
    backend = _started(tab)
    assert isinstance(backend, NoneBackend) and tab.backend is backend


def test_a_backend_started_for_a_replaced_factory_serves_its_send_only(qapp):
    """A settings swap during the start: the tab's next send starts on the new factory."""
    backend = _Backend()
    tab = _tab(_Factory(backend, backend_type="none"))
    answers = _Answers()
    tab.request_backend(answers.take)
    tab.update_backend_factory(_Factory(_Backend()))
    assert tab.adopt_backend(answers.first()) is backend and tab.backend is None


@pytest.fixture
def skill_chat(qapp, monkeypatch):
    """A skill chat over *factory* (its backend tab id ``my_wizard``)."""
    from procedo.studio.dock import skill_chat_widget as scw
    from .test_project_rules_skills import _delete_now
    from .test_skill_needed_context import _skill

    monkeypatch.setattr("procedo.studio.core.studio_settings.load_settings", lambda: {})
    made = []

    def make(factory):
        made.append(scw.SkillChatWidget([_skill()], factory, backend_tab_id="my_wizard"))
        return made[-1]

    yield make
    for widget in made:
        widget.shutdown()
        _delete_now(widget)


def test_a_skill_chat_retries_after_a_fallback_none_backend(skill_chat):
    """It kept the fallback: "server down" on every send until Restart."""
    fallback, real, later = NoneBackend(reason="server down"), _Backend(), _Backend()
    widget = skill_chat(_Factory())
    widget._on_backend_started((None, fallback, False))
    assert widget._llm.backend is None
    widget._on_backend_started((None, real, True))
    widget._on_backend_started((None, later, True))  # a second start meanwhile never replaces it
    assert widget._llm.backend is real


def test_a_skill_chat_without_a_factory_has_a_none_backend(skill_chat, monkeypatch):
    """It failed with an AttributeError ("Failed to start — see logs.")."""
    fired = []
    widget = skill_chat(None)
    real = widget._really_dispatch
    monkeypatch.setattr(widget, "_really_dispatch", lambda prompt, files, backend=None: (
        real(prompt, files) if backend is None else fired.append(backend)))
    widget._really_dispatch("hello", [])  # no server involved: fired at once
    assert isinstance(widget._llm.backend, NoneBackend) and fired == [widget._llm.backend]
