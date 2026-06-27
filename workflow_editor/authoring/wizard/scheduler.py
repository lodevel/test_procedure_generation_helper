"""Concurrency scheduler for the DCDC wizard's parallel per-IC builds.

Caps the number of CONCURRENTLY IN-FLIGHT *fires* (LLM turns) at ``capacity``.
Every fire — an initial build, the user's answer to the skill's question, or a
modification request — is submitted here. If a slot is free it fires at once;
otherwise it is QUEUED and fires when a slot frees. So user interactions can
never push the number of live LLM streams past ``capacity``: answering ten
waiting builds enqueues ten turns that drain ``capacity`` at a time.

Design (decided with the user):
- The cap counts CONCURRENT STREAMING turns. Idle states (waiting on a question,
  a generated-but-unvalidated test) hold NO slot, so the LLM keeps churning.
- Capacity may be RAISED at any time (immediately fires newly-eligible queued
  turns). It may NOT be lowered while fires are in flight — you can't un-launch a
  running turn.
- A user's interactive answer may jump the queue ahead of not-yet-started
  initial builds (``priority=True``), so engaging a test feels responsive.

PURE: no Qt, no threads, no I/O. The scheduler calls a stored zero-arg ``fire``
callable to actually start a turn; the Qt layer supplies one that dispatches an
``LLMWorker``. Per-key state is observable so the UI can render status badges.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Optional

__all__ = ["SlotState", "ConcurrencyScheduler"]


class SlotState(str, Enum):
    """Where a key sits relative to the slots — the basis for its UI badge."""

    IDLE = "idle"        # nothing pending or running for this key
    QUEUED = "queued"    # a fire is waiting for a slot to free
    RUNNING = "running"  # a fire is in flight (holds a slot)


@dataclass
class _Pending:
    key: str
    fire: Callable[[], None]


class ConcurrencyScheduler:
    """Gate ``capacity`` concurrent fires; queue the rest (FIFO, priority-first)."""

    def __init__(self, capacity: int = 5):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._running: set[str] = set()
        self._queue: Deque[_Pending] = deque()

    # -- observed state -------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    def state_of(self, key: str) -> SlotState:
        if key in self._running:
            return SlotState.RUNNING
        if any(p.key == key for p in self._queue):
            return SlotState.QUEUED
        return SlotState.IDLE

    # -- submit / complete ----------------------------------------------------

    def submit(self, key: str, fire: Callable[[], None],
               *, priority: bool = False) -> SlotState:
        """Submit a fire for ``key``. Fires immediately (→ ``RUNNING``) when a slot
        is free, else queues (→ ``QUEUED``). A key already running or queued is
        ignored (one pending fire per key — the widget guards too). ``priority``
        jumps the queue: use it for the user's interactive answers so they land
        ahead of not-yet-started initial builds."""
        if key in self._running or self._is_queued(key):
            return self.state_of(key)
        if len(self._running) < self._capacity:
            self._run(key, fire)
            return SlotState.RUNNING
        pending = _Pending(key, fire)
        self._queue.appendleft(pending) if priority else self._queue.append(pending)
        return SlotState.QUEUED

    def complete(self, key: str) -> Optional[str]:
        """Mark the in-flight fire for ``key`` finished → free its slot → fire the
        next queued item (if any). Returns the key that was started, or ``None``.
        Safe to call for a key that isn't running (no-op on the slot)."""
        self._running.discard(key)
        return self._pump()

    def cancel(self, key: str) -> bool:
        """Drop a QUEUED (not-yet-fired) fire for ``key`` — e.g. the user abandoned
        it before it started. Returns True if something was removed. Does NOT touch
        a running fire (that needs ``complete`` once its worker is stopped)."""
        before = len(self._queue)
        self._queue = deque(p for p in self._queue if p.key != key)
        return len(self._queue) != before

    def set_capacity(self, n: int) -> None:
        """Raise (or, only when nothing is running, lower) the cap. Raising fires
        every newly-eligible queued turn. Lowering while fires are in flight is
        rejected — a running turn can't be un-launched."""
        if n < 1:
            raise ValueError("capacity must be >= 1")
        if n < self._capacity and self._running:
            raise ValueError("cannot lower capacity while fires are in flight")
        self._capacity = n
        while self._pump() is not None:  # a raise may free several slots at once
            pass

    # -- internals ------------------------------------------------------------

    def _is_queued(self, key: str) -> bool:
        return any(p.key == key for p in self._queue)

    def _run(self, key: str, fire: Callable[[], None]) -> None:
        """Occupy a slot and fire. If ``fire`` raises synchronously (e.g. backend
        setup failed) the slot is RELEASED before the exception propagates, so a
        failed dispatch can never permanently strand a slot (the cap recovers)."""
        self._running.add(key)
        try:
            fire()
        except Exception:
            self._running.discard(key)
            raise

    def _pump(self) -> Optional[str]:
        """Fire the next queued item if a slot is free. Returns its key, or None
        when nothing is eligible. A queued item whose ``fire`` raises is dropped
        (its slot was released in :meth:`_run`) and the next item is tried, so one
        broken dispatch can't stall the whole queue."""
        while self._queue and len(self._running) < self._capacity:
            pending = self._queue.popleft()
            try:
                self._run(pending.key, pending.fire)
                return pending.key
            except Exception:  # noqa: BLE001 — slot already freed; skip to the next
                continue
        return None
