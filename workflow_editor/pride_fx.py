"""Playful Pride-theme effects: a pink cursor and a little sparkle burst on click.

Purely cosmetic and fully self-contained. ``set_enabled(app, True)`` pushes a pink
arrow cursor over the whole app and starts spawning sparkles wherever the user
clicks; ``set_enabled(app, False)`` restores the normal cursor and removes the
click filter. It is idempotent, so calling it repeatedly (e.g. switching between
the light/dark Pride variants) never stacks cursors or filters.

Theme code calls this inside a try/except: any failure here must never affect the
rest of the app.
"""
from __future__ import annotations

import math
import random

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QWidget

# Pink leads (cursor + most glitter); the rest are rainbow accents for flair.
_PINK = "#FF49C7"
_GLITTER_COLORS = (
    _PINK, _PINK, "#FF8AE2", "#FFFFFF", "#FFD43B",
    "#FF6B6B", "#4ECDC4", "#5C7CFA", "#9775FA",
)

_override_pushed = False
_filter: "_GlitterFilter | None" = None


# ── public API ───────────────────────────────────────────────────────

def set_enabled(app: QApplication | None, on: bool) -> None:
    """Turn the pink cursor + click sparkles on or off (idempotent)."""
    global _override_pushed, _filter
    app = app or QApplication.instance()
    if app is None:
        return

    if on:
        if not _override_pushed:
            app.setOverrideCursor(_build_pink_cursor())
            _override_pushed = True
        if _filter is None:
            _filter = _GlitterFilter(app)
            app.installEventFilter(_filter)
    else:
        if _override_pushed:
            app.restoreOverrideCursor()
            _override_pushed = False
        if _filter is not None:
            app.removeEventFilter(_filter)
            _filter.clear()
            _filter = None


# ── pink cursor ──────────────────────────────────────────────────────

def _build_pink_cursor() -> QCursor:
    """A normal-sized arrow pointer in pink, hotspot at the tip, with a tiny sparkle.

    Rendered at a supersampled size then smoothly downscaled to ``side`` px. We do
    NOT set devicePixelRatio: Windows builds the native cursor from the raw pixmap
    and ignores DPR, so a 2x pixmap becomes an oversized, clipped cursor.
    """
    side = 24
    ss = 3  # supersample for crisp edges, then downscale to `side`
    big = QPixmap(side * ss, side * ss)
    big.fill(Qt.GlobalColor.transparent)

    p = QPainter(big)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(ss, ss)

    arrow = QPolygonF([
        QPointF(1, 1), QPointF(1, 17), QPointF(5, 13), QPointF(8, 20),
        QPointF(11, 19), QPointF(8, 12), QPointF(15, 12),
    ])
    pen = p.pen()
    pen.setColor(QColor("#FFFFFF"))
    pen.setWidthF(1.0)
    p.setPen(pen)
    p.setBrush(QColor(_PINK))
    p.drawPolygon(arrow)

    # a little sparkle riding on the cursor (kept well inside the bounds)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawPolygon(_star_polygon(16.0, 16.5, 2.4, 0.9, 4, 0.0))

    p.end()

    pm = big.scaled(
        side, side,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return QCursor(pm, 1, 1)


# ── click sparkles ───────────────────────────────────────────────────

class _GlitterFilter(QObject):
    """App-wide event filter that drops a sparkle burst on each mouse press.

    The filter sits on the QApplication, so a press on a widget that does not
    accept it bubbles up the parent chain and re-enters the filter once per
    ancestor — every copy carrying the SAME event timestamp. We therefore spawn at
    most one burst per timestamp, so a single physical click is a single burst
    regardless of how deep the widget tree is (this is what made the editor's deep
    hierarchy sparkle and lag far more than the flat main window). ``_MAX_OVERLAYS``
    is a hard backstop against a rapid click storm.
    """

    _MAX_OVERLAYS = 10

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._overlays: set[_SparkleBurst] = set()
        self._last_ts = -1

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            try:
                ts = int(event.timestamp())
                if ts != self._last_ts:          # ignore the propagation echoes
                    self._last_ts = ts
                    if len(self._overlays) < self._MAX_OVERLAYS:
                        burst = _SparkleBurst(
                            event.globalPosition().toPoint(), self._overlays.discard
                        )
                        self._overlays.add(burst)
            except Exception:
                # best-effort: cosmetic sparkle burst on a hot mouse-event path;
                # a failure (e.g. synthesized event without globalPosition) must
                # never affect click handling, and logging per-click is noise.
                pass
        return False  # never consume — clicks behave normally

    def clear(self) -> None:
        for burst in list(self._overlays):
            burst.close()
        self._overlays.clear()


class _SparkleBurst(QWidget):
    """A short-lived, click-through top-level widget that animates a glitter pop."""

    _BOX = 160          # widget side; center is (80, 80)
    _TICKS = 30         # ~480ms at 16ms/tick
    _INTERVAL_MS = 16

    def __init__(self, global_center: QPoint, on_done) -> None:
        super().__init__(None)
        self._on_done = on_done
        self._cx = self._cy = self._BOX / 2
        self._tick = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(
            global_center.x() - self._BOX // 2,
            global_center.y() - self._BOX // 2,
            self._BOX, self._BOX,
        )

        self._sparkles = [self._make_sparkle() for _ in range(random.randint(11, 15))]

        self._timer = QTimer(self)
        self._timer.setInterval(self._INTERVAL_MS)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

        self.show()
        self.raise_()

    @staticmethod
    def _make_sparkle() -> dict:
        return {
            "ang": random.uniform(0, 2 * math.pi),
            "dist": random.uniform(8, 38),
            "size": random.uniform(3.0, 7.5),
            "color": random.choice(_GLITTER_COLORS),
            "delay": random.uniform(0.0, 0.25),
            "rot": random.uniform(0, math.pi),
            "spin": random.uniform(-2.0, 2.0),
        }

    def _advance(self) -> None:
        self._tick += 1
        if self._tick >= self._TICKS:
            self._finish()
        else:
            self.update()

    def _finish(self) -> None:
        self._timer.stop()
        if self._on_done is not None:
            self._on_done(self)
        self.close()
        self.deleteLater()

    def paintEvent(self, _event) -> None:
        progress = min(1.0, self._tick / self._TICKS)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for sp in self._sparkles:
            delay = sp["delay"]
            if progress <= delay:
                continue
            lt = (progress - delay) / (1.0 - delay)
            if lt >= 1.0:
                continue
            ease = 1.0 - (1.0 - lt) ** 2          # fly outward, decelerating
            dist = sp["dist"] * ease
            x = self._cx + math.cos(sp["ang"]) * dist
            y = self._cy + math.sin(sp["ang"]) * dist
            if lt < 0.3:                           # pop in, then gently shrink
                size = sp["size"] * (lt / 0.3)
            else:
                size = sp["size"] * (1.0 - (lt - 0.3) / 0.7 * 0.55)
            color = QColor(sp["color"])
            color.setAlpha(max(0, min(255, int(255 * (1.0 - lt) ** 1.2))))
            p.setBrush(color)
            p.drawPolygon(
                _star_polygon(x, y, size, size * 0.4, 4, sp["rot"] + lt * sp["spin"])
            )
        p.end()


def _star_polygon(cx: float, cy: float, outer: float, inner: float,
                  points: int, rotation: float) -> QPolygonF:
    """A 2*points-vertex star centred at (cx, cy)."""
    verts: list[QPointF] = []
    for i in range(points * 2):
        radius = outer if i % 2 == 0 else inner
        ang = rotation + math.pi * i / points
        verts.append(QPointF(cx + math.cos(ang) * radius, cy + math.sin(ang) * radius))
    return QPolygonF(verts)
