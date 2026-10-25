"""Engine calls that never freeze Studio's window (docs/plans/engine_process.md slice 5).

Each engine op is a round trip to the project's TOOLS instance (:mod:`.engine`): ~3 ms warm,
but up to its deadline when the instance is busy, starting or wedged. :func:`call` runs it on a
daemon thread and hands the answer back on the GUI thread through a :mod:`procedo.core.off_gui`
relay (``QTimer.singleShot`` from a worker thread never fires). :func:`wait` is the same for a
caller that needs the result before it returns (the wizards' authoring API is synchronous).

Without a project there is no engine: those calls refuse at once (``ParserUnavailable``), on the
GUI thread.
"""

from __future__ import annotations

import itertools
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject

from procedo.core.off_gui import answer_to

#: The name of the threads that run engine calls.
THREAD = "engine-call"
#: The status line when the text changed during a call that would have replaced it.
STALE = "The text changed while it ran; run it again."

Answer = Callable[[], Any]
"""What a call hands back: returns the call's result, or raises the exception it raised."""


def _run(fn: Callable[[], Any]) -> Answer:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — raised again where the answer is read
        error = exc

        def answer():
            raise error

        return answer
    return lambda: result


class _Inbox(QObject):
    """Where each call's answer lands, on the GUI thread: its ``done`` waits here, so the
    worker holds only the call and the inbox's relay."""

    def __init__(self) -> None:
        super().__init__()
        self._done: dict[int, Callable[[Answer], None]] = {}
        self._tickets = itertools.count()
        self.send = answer_to(self._deliver)

    def expect(self, done: Callable[[Answer], None]) -> int:
        ticket = next(self._tickets)
        self._done[ticket] = done
        return ticket

    def _deliver(self, answer) -> None:
        ticket, result = answer
        self._done.pop(ticket)(result)


_inbox: Optional[_Inbox] = None
_busy: set = set()


def _the_inbox() -> _Inbox:
    """Made by the first :func:`call`, so on the GUI thread whichever thread imported this
    module first (a lazy import may run on a worker)."""
    global _inbox
    if _inbox is None:
        _inbox = _Inbox()
    return _inbox


def _settle(widget_call: Callable[..., Any], *args: Any) -> None:
    """A call's widget put back, unless the window closed meanwhile and took it along."""
    try:
        widget_call(*args)
    except RuntimeError:  # the C++ widget is gone
        pass


def call(
    project_root: Optional[Path],
    fn: Callable[[], Any],
    on_done: Callable[[Answer], None],
    *,
    button=None,
    cue=None,
    cue_label: str = "",
) -> None:
    """Run ``fn()`` (an engine call for ``project_root``, its inputs already read) off the GUI
    thread, then ``on_done(answer)`` on it. While it runs ``button`` is disabled and another
    call for it is ignored, and ``cue`` (a :class:`procedo.ui.ui_kit.LoadingCue`, where the
    answer will show) says ``cue_label`` — once the call has taken a moment: a warm engine
    answers before it shows. Call it on the GUI thread."""
    if button is not None:
        if button in _busy:
            return
        _busy.add(button)
        button.setEnabled(False)
    if cue is not None:
        cue.start(cue_label or None)

    def done(answer: Answer) -> None:
        _busy.discard(button)
        if button is not None:
            _settle(button.setEnabled, True)
        if cue is not None:
            _settle(cue.done)
        on_done(answer)

    if project_root is None:
        done(_run(fn))
        return
    inbox = _the_inbox()
    ticket, send = inbox.expect(done), inbox.send
    threading.Thread(target=lambda: send((ticket, _run(fn))), name=THREAD, daemon=True).start()


def busy(button) -> bool:
    """``button``'s call is running (its gating leaves it disabled until it answers)."""
    return button in _busy


def wait(project_root: Optional[Path], fn: Callable[[], Any]) -> Any:
    """``fn()``'s result (or its exception), run off the GUI thread while the window repaints;
    input waits until it returns, so nothing can be pressed twice."""
    if project_root is None:
        return fn()
    box: list[Answer] = []
    worker = threading.Thread(target=lambda: box.append(_run(fn)), name=THREAD, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.01)
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    return box[0]()


def unchanged(editor, sent: str, say: Callable[[str], None]) -> bool:
    """The stale-text guard: True when ``editor`` still holds ``sent``, the text a result was
    made from; else the result is dropped, and ``say`` tells the user so."""
    if editor.toPlainText() == sent:
        return True
    say(STALE)
    return False
