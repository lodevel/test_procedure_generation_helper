"""procedure_text.md coloured as on the approved boards: headings, step numbers, ``Operator:``,
Meta keys and the Equipment block's device ids."""

from __future__ import annotations

import re

from PySide6.QtGui import QFont, QSyntaxHighlighter, QTextCharFormat

from .. import theme

#: (pattern, colour, bold): group 1 of each pattern is coloured.
_RULES = (
    (re.compile(r"^(#.*)$"), theme.text_heading, True),
    (re.compile(r"^\s*(\d+\.)\s"), theme.text_step_number, False),
    (re.compile(r"^\s*\d+\.\s+(Operator:)"), theme.text_operator, True),
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
