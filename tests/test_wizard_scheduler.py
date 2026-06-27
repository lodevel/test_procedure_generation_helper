"""Tests for the DCDC wizard concurrency scheduler (wizard/scheduler.py)."""
from __future__ import annotations

import pytest

from workflow_editor.authoring.wizard.scheduler import (
    ConcurrencyScheduler,
    SlotState,
)


def _recorder():
    """A fire-factory that records the order keys actually fire in."""
    fired: list[str] = []
    return fired, (lambda key: (lambda: fired.append(key)))


def test_submits_under_capacity_fire_immediately():
    fired, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=2)
    assert s.submit("a", fire_for("a")) is SlotState.RUNNING
    assert s.submit("b", fire_for("b")) is SlotState.RUNNING
    assert fired == ["a", "b"]
    assert s.running_count == 2 and s.queued_count == 0


def test_submit_over_capacity_queues_and_drains_on_complete():
    fired, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=2)
    s.submit("a", fire_for("a"))
    s.submit("b", fire_for("b"))
    assert s.submit("c", fire_for("c")) is SlotState.QUEUED   # full -> queued
    assert fired == ["a", "b"]                                # c not fired yet
    assert s.state_of("c") is SlotState.QUEUED
    s.complete("a")                                          # free a slot
    assert fired == ["a", "b", "c"]                          # c fires now
    assert s.running_count == 2 and s.queued_count == 0


def test_never_exceeds_capacity_even_when_user_answers_many():
    """The core guarantee: ten waiting builds the user answers at once cannot push
    live streams past the cap — they enqueue and drain `capacity` at a time."""
    fired, fire_for = _recorder()
    cap = 3
    s = ConcurrencyScheduler(capacity=cap)
    live_peak = 0
    for i in range(10):
        s.submit(f"ic{i}", fire_for(f"ic{i}"))
        live_peak = max(live_peak, s.running_count)
    assert live_peak == cap                       # never more than `cap` at once
    assert s.running_count == cap and s.queued_count == 7
    # drain: each complete fires exactly one queued turn, cap held throughout
    done = 0
    while s.running_count:
        running = list(s._running)                # internal peek for the test
        s.complete(running[0])
        done += 1
        assert s.running_count <= cap
    assert done == 10 and len(fired) == 10


def test_priority_answer_jumps_ahead_of_initial_builds():
    fired, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=1)
    s.submit("running", fire_for("running"))      # holds the only slot
    s.submit("build1", fire_for("build1"))        # queued (initial build)
    s.submit("build2", fire_for("build2"))        # queued (initial build)
    s.submit("answer", fire_for("answer"), priority=True)   # jumps the queue
    s.complete("running")
    assert fired[-1] == "answer"                  # the user's answer fired first
    s.complete("answer")
    assert fired[-1] == "build1"                  # then FIFO order resumes


def test_raising_capacity_fires_queued_immediately():
    fired, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=1)
    s.submit("a", fire_for("a"))
    s.submit("b", fire_for("b"))
    s.submit("c", fire_for("c"))
    assert fired == ["a"] and s.queued_count == 2
    s.set_capacity(3)                             # raise -> b and c fire at once
    assert fired == ["a", "b", "c"] and s.queued_count == 0


def test_lowering_capacity_while_running_is_rejected():
    _, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=3)
    s.submit("a", fire_for("a"))
    with pytest.raises(ValueError):
        s.set_capacity(1)                         # can't un-launch a running turn
    s.complete("a")
    s.set_capacity(1)                             # fine once nothing is running
    assert s.capacity == 1


def test_cancel_removes_a_queued_fire():
    fired, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=1)
    s.submit("a", fire_for("a"))
    s.submit("b", fire_for("b"))                  # queued
    assert s.cancel("b") is True
    s.complete("a")
    assert "b" not in fired                       # never fired
    assert s.queued_count == 0


def test_fire_exception_frees_the_slot():
    """If a fire() raises synchronously (e.g. backend setup blew up), its slot must
    be RELEASED, not stranded — else the cap silently shrinks and deadlocks."""
    s = ConcurrencyScheduler(capacity=1)

    def boom():
        raise RuntimeError("dispatch failed")

    with pytest.raises(RuntimeError):
        s.submit("a", boom)
    assert s.running_count == 0                  # slot freed despite the exception
    fired = []
    assert s.submit("b", lambda: fired.append("b")) is SlotState.RUNNING  # reusable
    assert fired == ["b"]


def test_queued_fire_exception_is_skipped_not_stalling():
    """A queued item whose fire() raises is dropped and the NEXT one runs — one
    broken dispatch can't stall the whole queue."""
    s = ConcurrencyScheduler(capacity=1)
    fired = []

    def boom():
        raise RuntimeError()

    s.submit("run", lambda: fired.append("run"))     # holds the slot
    s.submit("boom", boom)                           # queued; will raise when fired
    s.submit("ok", lambda: fired.append("ok"))       # queued behind boom
    s.complete("run")                                # pump boom (raises→skip) → pump ok
    assert fired == ["run", "ok"]
    assert s.state_of("ok") is SlotState.RUNNING and s.running_count == 1


def test_duplicate_submit_for_a_running_or_queued_key_is_ignored():
    fired, fire_for = _recorder()
    s = ConcurrencyScheduler(capacity=1)
    s.submit("a", fire_for("a"))
    s.submit("a", fire_for("a"))                  # already running -> ignored
    s.submit("b", fire_for("b"))                  # queued
    s.submit("b", fire_for("b"))                  # already queued -> ignored
    assert s.queued_count == 1
    s.complete("a")
    assert fired == ["a", "b"]                    # b fires exactly once
