"""Paint passive status indicators as rounded badges in item views."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem


BADGES_ROLE = Qt.ItemDataRole.UserRole + 500


class BadgeDelegate(QStyledItemDelegate):
    """Draw pill badges over the right edge of normal list items."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(painter, option, index)
        badges = _normalize_badges(index.data(BADGES_ROLE))
        if not badges:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        badge_font = option.font
        if badge_font.pointSize() > 0:
            badge_font.setPointSize(max(badge_font.pointSize() - 1, 7))
        badge_font.setBold(True)
        painter.setFont(badge_font)
        metrics = QFontMetrics(badge_font)

        sizes = [_badge_size(metrics, label) for label, _tone in badges]
        spacing = 5
        total_width = sum(size.width() for size in sizes) + spacing * (len(sizes) - 1)
        x = option.rect.right() - total_width - 10
        y = option.rect.center().y()
        dark = option.palette.color(QPalette.ColorRole.Window).lightness() < 128

        for (label, tone), size in zip(badges, sizes):
            rect = QRect(x, y - size.height() // 2, size.width(), size.height())
            colors = _badge_colors(tone, dark)
            painter.setPen(colors["border"])
            painter.setBrush(colors["bg"])
            painter.drawRoundedRect(rect, 7, 7)
            painter.setPen(colors["fg"])
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label.upper())
            x += size.width() + spacing

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        size = super().sizeHint(option, index)
        if _normalize_badges(index.data(BADGES_ROLE)):
            size.setHeight(max(size.height(), 34))
        return size


def _normalize_badges(value) -> list[tuple[str, str]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return []
    badges: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, str):
            label, tone = item, "default"
        elif isinstance(item, tuple) and item:
            label = str(item[0])
            tone = str(item[1]) if len(item) > 1 else "default"
        else:
            continue
        if label:
            badges.append((label, tone))
    return badges


def _badge_size(metrics: QFontMetrics, label: str) -> QSize:
    return QSize(metrics.horizontalAdvance(label.upper()) + 16, max(metrics.height() + 6, 20))


def _badge_colors(tone: str, dark: bool) -> dict[str, QColor]:
    palettes = _DARK_BADGES if dark else _LIGHT_BADGES
    fg, bg, border = palettes.get(tone, palettes["default"])
    return {"fg": QColor(fg), "bg": QColor(bg), "border": QColor(border)}


_DARK_BADGES = {
    "default": ("#bae6fd", "#0f2d45", "#256b99"),
    "success": ("#bbf7d0", "#123826", "#2f8f5b"),
    "warning": ("#fde68a", "#3a2b10", "#a9791c"),
    "danger": ("#fecdd3", "#3f1720", "#a83f52"),
    "purple": ("#ddd6fe", "#2b214d", "#7561bc"),
    "muted": ("#cbd5e1", "#28313d", "#515f70"),
}

_LIGHT_BADGES = {
    "default": ("#1d4ed8", "#e8f1ff", "#a9c8f5"),
    "success": ("#047857", "#e9f8f0", "#9ed9bb"),
    "warning": ("#92400e", "#fff4d8", "#e8c36d"),
    "danger": ("#be123c", "#fff0f3", "#efadb9"),
    "purple": ("#6d28d9", "#f1ebff", "#c8b6f0"),
    "muted": ("#66758a", "#f1f4f8", "#cdd5df"),
}
