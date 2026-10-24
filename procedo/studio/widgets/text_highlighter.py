"""procedure_text.md coloured as on the approved boards: headings, step numbers, ``Operator:``,
Meta keys and the Equipment block's device ids."""

from __future__ import annotations

import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from procedo.ui.theme import muted_color

from .. import theme

#: (pattern, colour, bold): group 1 of each pattern is coloured.
_RULES = (
    (re.compile(r"^(#.*)$"), theme.text_heading, True),
    (re.compile(r"^\s*(\d+\.)\s"), lambda: QColor(muted_color()), False),
    (re.compile(r"^\s*\d+\.\s+(Operator:)"), lambda: theme.tone("pill_warning_fg"), True),
    (re.compile(r"^([a-z_]+):"), theme.text_meta_key, False),
    (re.compile(r"^([A-Z][A-Z0-9_]*)\s+:"), theme.text_device, True),
)


class ProcedureTextHighlighter(QSyntaxHighlighter):
    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules = []
        for pattern, colour, bold in _RULES:
            fmt = QTextCharFormat()
            fmt.setForeground(colour())
            if bold:
                fmt.setFontWeight(QFont.Weight.DemiBold)
            self._rules.append((pattern, fmt))

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt)
        for pattern, fmt in self._rules:
            match = pattern.match(text)
            if match:
                self.setFormat(match.start(1), match.end(1) - match.start(1), fmt)
