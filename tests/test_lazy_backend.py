"""The lazy backend create (``backend_factory.start_backend``) of a Studio tab and of a skill
chat, one policy for both: a backend is created and started on first use and kept, except a
fallback ``NoneBackend`` (the server would not start): the next use tries the factory (and the
server) again; without a factory, a kept ``NoneBackend``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_a_tab_keeps_a_real_backend():
    backend = _Backend()
    tab = _tab(_Factory(backend))
    assert tab.backend is backend and tab.backend is backend
    assert backend.started == 1


def test_a_tab_retries_after_a_fallback_none_backend():
    fallback, real = NoneBackend(reason="server down"), _Backend()
    factory = _Factory(fallback, real)
    tab = _tab(factory)
    assert tab.backend is fallback
    assert tab.backend is real and factory.asked == ["text_json", "text_json"]


def test_a_tab_keeps_the_none_backend_it_was_configured_with():
    chosen = NoneBackend()
    factory = _Factory(chosen, backend_type="none")
    tab = _tab(factory)
    assert tab.backend is chosen and tab.backend is chosen and len(factory.asked) == 1


def test_a_tab_without_a_factory_has_a_none_backend():
    assert isinstance(_tab(None).backend, NoneBackend)


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
    fallback, real = NoneBackend(reason="server down"), _Backend()
    factory = _Factory(fallback, real)
    widget = skill_chat(factory)
    assert widget._ensure_backend() is fallback
    assert widget._ensure_backend() is real and widget._ensure_backend() is real
    assert factory.asked == ["my_wizard", "my_wizard"] and real.started == 1


def test_a_skill_chat_without_a_factory_has_a_none_backend(skill_chat):
    """It failed with an AttributeError ("Failed to start — see logs.")."""
    widget = skill_chat(None)
    backend = widget._ensure_backend()
    assert isinstance(backend, NoneBackend) and widget._ensure_backend() is backend
