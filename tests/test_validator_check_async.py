"""The Studio proposal check (validate + reconstruct + meta sync: several engine round trips) runs on
a worker, never on the GUI thread: the chat is locked while it runs, its answer applies only to the
run it belongs to (a newer run or a closed test drops it), and a failed check ends the run with the
error shown."""

import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

import procedo.studio.tabs.llm_tab_mixin as llm_tab_mixin
from procedo.studio.llm import LLMTask
from procedo.studio.llm.validator_dispatch import ValidationOutcome

from .test_proposal_cards import opened as opened  # noqa: F401
from .test_main_window_characterization import (  # noqa: F401
    make_window as make_window, mbox as mbox, qapp as qapp,
)

OK = ValidationOutcome(ok=True, skipped=False, issues=[])


class _Asked:
    def __init__(self, *args, **kwargs):
        self.finished = self.error = self.thinking_chunk = self.text_chunk = SimpleNamespace(
            connect=lambda slot: None)

    def start(self):
        pass

    def isRunning(self):
        return False


class _Check:
    """A slow stand-in for validate_response: waits for release(), records its thread."""

    def __init__(self, outcome=OK, error=None):
        self.outcome, self.error = outcome, error
        self.calls, self.threads = 0, []
        self.entered, self._release = threading.Event(), threading.Event()

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.threads.append(threading.get_ident())
        self.entered.set()
        assert self._release.wait(10)
        if self.error:
            raise self.error
        return self.outcome

    def release(self):
        self._release.set()


def _pump(until, timeout=10):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    assert until()


@pytest.fixture
def asked(opened, monkeypatch):  # noqa: F811
    """A run asked on t1, its reply about to be checked; the check is slow, applying is recorded."""
    w = opened
    view = w.text_only_tab
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Asked)
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)
    view.check = _Check()
    monkeypatch.setattr(llm_tab_mixin, "validate_response", view.check)
    view.applied = []
    monkeypatch.setattr(view, "_apply_proposals", lambda r, task=None: view.applied.append(r))
    view.reply = SimpleNamespace(total_tokens=1)
    return w, view


def _start(view):
    view._start_validator_check(view.reply)


def test_the_check_runs_off_the_gui_thread_and_locks_the_chat_meanwhile(asked):
    w, view = asked
    _start(view)  # returns at once although the check is still blocked
    assert view.check.entered.wait(10)
    assert view.check.threads[0] != threading.get_ident()
    chat = w.dock.chat_panel
    assert not chat.send_btn.isEnabled() and not chat.input_field.isEnabled()
    assert not chat.cancel_btn.isEnabled()
    assert view.applied == [] and view._ensure_run_state().state.name == "VALIDATING"

    view.check.release()
    _pump(lambda: view.applied)

    assert view.applied == [view.reply] and view.check.calls == 1  # applied once, checked once
    assert chat.send_btn.isEnabled() and chat.input_field.isEnabled()


def test_a_newer_run_drops_the_answer_of_the_check_it_replaced(asked):
    w, view = asked
    _start(view)
    assert view.check.entered.wait(10)
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # a new proposal request
    state = view._ensure_run_state().state.name

    view.check.release()
    time.sleep(0.2)
    QCoreApplication.processEvents()
    QCoreApplication.processEvents()

    run_state = view._ensure_run_state()
    assert view.applied == [] and run_state.state.name == state == "LLM_REQUESTED"
    assert run_state.last_outcome is None  # the stale outcome never reached the new run's FSM


def test_a_check_answering_after_its_test_closed_is_dropped(asked):
    w, view = asked
    _start(view)
    assert view.check.entered.wait(10)
    w.open_test(w.root / "tests" / "t2")

    view.check.release()
    _pump(lambda: w.dock.chat_panel.send_btn.isEnabled())

    assert view.applied == [] and view._ensure_run_state().state.name == "IDLE"


def test_a_failed_check_ends_the_run_and_shows_the_error(asked, mbox):
    w, view = asked
    view.check.error = RuntimeError("engine gone")
    _start(view)
    view.check.release()
    _pump(lambda: mbox.critical)

    assert mbox.critical[0][0] == "Validator Error" and "engine gone" in mbox.critical[0][1]
    assert view.applied == [] and view._ensure_run_state().state.name == "IDLE"
    assert w.dock.chat_panel.send_btn.isEnabled()
