"""A small elapsed-time ticker for "the LLM is working" indicators.

Both chat surfaces show how long the current request has been running — the dock
tab chat's "💭 Thinking…" header and the skill chat's status line. Rather than
each owning a ``QTimer`` + a mm:ss formatter (the kind of duplication that drifts),
they share this one seam: construct a :class:`WorkTimer` with an ``on_tick``
callback that writes the formatted elapsed string into whatever label the surface
uses, call :meth:`start` when the request goes out and :meth:`stop` when it
returns. The clock is monotonic wall-time (``QElapsedTimer``), so it measures real
elapsed seconds regardless of system-clock changes.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, QElapsedTimer


def format_elapsed(ms: int) -> str:
    """Format milliseconds as a compact clock: ``0:07`` / ``1:23`` / ``1:02:05``.

    Seconds are always two digits; minutes pad to two digits only once hours
    appear; hours are shown only past 60 min. Negative input clamps to ``0:00``.
    """
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class WorkTimer(QObject):
    """Ticks once a second while a request is in flight.

    ``on_tick(formatted)`` fires immediately on :meth:`start` (so the label shows
    ``0:00`` at once, not after a 1 s wait) and then every second. :meth:`stop`
    halts the ticking and returns the final formatted elapsed — the caller decides
    whether to show "done in X" or clear the label. Idempotent: a second
    :meth:`start` restarts the clock; :meth:`stop` on a stopped timer is a no-op
    that still returns the last elapsed.
    """

    def __init__(
        self,
        on_tick: Callable[[str], None],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._on_tick = on_tick
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._emit)

    def start(self) -> None:
        self._clock.restart()
        self._timer.start()
        self._emit()  # paint 0:00 right away

    def stop(self) -> str:
        self._timer.stop()
        return format_elapsed(self._clock.elapsed()) if self._clock.isValid() else "0:00"

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def _emit(self) -> None:
        try:
            self._on_tick(format_elapsed(self._clock.elapsed()))
        except Exception:
            # A UI callback must never crash the ticker (or the timeout signal).
            pass
