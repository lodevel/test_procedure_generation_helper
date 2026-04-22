"""
Python syntax highlighter for test.py editors.
"""

from PySide6.QtGui import QFont, QSyntaxHighlighter, QTextCharFormat

from ..theme import (
    syntax_keyword, syntax_string, syntax_comment,
    syntax_step_marker, syntax_step_bg, syntax_function,
)


class PythonSyntaxHighlighter(QSyntaxHighlighter):
    """Simple Python syntax highlighter."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_formats()
        
        self.keywords = [
            "def", "class", "if", "elif", "else", "for", "while", "try",
            "except", "finally", "with", "as", "import", "from", "return",
            "yield", "raise", "pass", "break", "continue", "and", "or",
            "not", "in", "is", "True", "False", "None", "async", "await"
        ]
    
    def _setup_formats(self):
        """Build text-char formats from current theme colours."""
        # Keywords
        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(syntax_keyword())
        self.keyword_format.setFontWeight(QFont.Bold)

        # Strings
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(syntax_string())

        # Comments
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(syntax_comment())
        self.comment_format.setFontItalic(True)

        # Step markers
        self.step_format = QTextCharFormat()
        self.step_format.setForeground(syntax_step_marker())
        self.step_format.setFontWeight(QFont.Bold)
        self.step_format.setBackground(syntax_step_bg())

        # Functions
        self.function_format = QTextCharFormat()
        self.function_format.setForeground(syntax_function())

    def highlightBlock(self, text: str):
        import re
        
        # Comments (before other patterns)
        comment_match = re.search(r'#.*$', text)
        if comment_match:
            # Check if it's a step marker
            step_match = re.match(r'^\s*#\s*Step\s+\d+', text, re.IGNORECASE)
            if step_match:
                self.setFormat(0, len(text), self.step_format)
            else:
                self.setFormat(comment_match.start(), len(text) - comment_match.start(), self.comment_format)
        
        # Keywords
        for keyword in self.keywords:
            pattern = rf'\b{keyword}\b'
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)
        
        # Strings (simple version)
        for match in re.finditer(r'(["\'])(?:(?!\1).)*\1', text):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        
        # Function definitions
        for match in re.finditer(r'\bdef\s+(\w+)', text):
            self.setFormat(match.start(1), match.end(1) - match.start(1), self.function_format)

