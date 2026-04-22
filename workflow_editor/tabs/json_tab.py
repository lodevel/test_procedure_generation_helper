"""
JSON syntax highlighter for procedure.json editors.
"""

from PySide6.QtGui import QFont, QSyntaxHighlighter, QTextCharFormat

from ..theme import json_key, json_string, json_number, json_keyword


class JsonSyntaxHighlighter(QSyntaxHighlighter):
    """Simple JSON syntax highlighter."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_formats()

    def _setup_formats(self):
        """Build text-char formats from current theme colours."""
        self.key_format = QTextCharFormat()
        self.key_format.setForeground(json_key())
        self.key_format.setFontWeight(QFont.Bold)

        self.string_format = QTextCharFormat()
        self.string_format.setForeground(json_string())

        self.number_format = QTextCharFormat()
        self.number_format.setForeground(json_number())

        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(json_keyword())
    
    def highlightBlock(self, text: str):
        import re
        
        # Highlight keys (before colon)
        for match in re.finditer(r'"([^"]+)"\s*:', text):
            self.setFormat(match.start(), match.end() - match.start() - 1, self.key_format)
        
        # Highlight strings (after colon or in arrays)
        for match in re.finditer(r':\s*"([^"]*)"', text):
            self.setFormat(match.start() + 1, match.end() - match.start() - 1, self.string_format)
        
        # Highlight numbers
        for match in re.finditer(r'\b(\d+\.?\d*)\b', text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)
        
        # Highlight keywords
        for match in re.finditer(r'\b(true|false|null)\b', text):
            self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)


