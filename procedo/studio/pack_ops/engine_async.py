"""Engine calls that never freeze Studio's window (docs/plans/engine_process.md slice 5).

Each engine op is a round trip to the project's TOOLS instance (:mod:`.engine`): ~3 ms warm,
but up to its deadline when the instance is busy, starting or wedged. :func:`call` runs it on a
daemon thread and hands the answer back on the GUI thread through a queued signal
(``QTimer.singleShot`` from a worker thread never fires). :func:`wait` is the same for a caller
that needs the result before it returns (the wizards' authoring API is synchronous).

Without a project the wheel runs in this process, and importing it installs a signal handler,
which fails off the main thread: those calls run at once, on the GUI thread.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Signal, Slot

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


class _Relay(QObject):
    """Lives on the GUI thread: a worker's emit is queued and delivered there."""

    answered = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.answered.connect(self._deliver)

    @Slot(object)
    def _deliver(self, delivery) -> None:
        on_done, answer = delivery
        on_done(answer)


_relay: Optional[_Relay] = None
_busy: set = set()


def _gui_relay() -> _Relay:
    """The relay, on the application's (GUI) thread whichever thread asks first."""
    global _relay
    if _relay is None:
        relay = _Relay()
        app = QCoreApplication.instance()
        if app is not None:
            relay.moveToThread(app.thread())
        _relay = relay
    return _relay


def call(project_root: Optional[Path], fn: Callable[[], Any], on_done: Callable[[Answer], None],
         *, button=None) -> None:
    """Run ``fn()`` (an engine call for ``project_root``, its inputs already read) off the GUI
    thread, then ``on_done(answer)`` on it. While it runs ``button`` is disabled and another
    call for it is ignored."""
    if button is not None:
        if button in _busy:
            return
        _busy.add(button)
        button.setEnabled(False)

    def done(answer: Answer) -> None:
        if button is not None:
            _busy.discard(button)
            try:
                button.setEnabled(True)
            except RuntimeError:  # the window closed meanwhile: its button is gone
                pass
        on_done(answer)

    if project_root is None:
        done(_run(fn))
        return
    relay = _gui_relay()
    threading.Thread(target=lambda: relay.answered.emit((done, _run(fn))), name=THREAD,
                     daemon=True).start()


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
