"""
Diff Viewer - Show diffs before applying proposals.

Implements Section 11.4 of the spec.
"""

import difflib
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSplitter
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCharFormat, QColor, QTextCursor, QSyntaxHighlighter
from ..theme import (
    diff_added_bg, diff_added_fg, diff_removed_bg, diff_removed_fg,
    diff_header_color,
)


class DiffHighlighter(QSyntaxHighlighter):
    """Syntax highlighter for diff output."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
    
    def highlightBlock(self, text: str):
        if text.startswith('+') and not text.startswith('+++'):
            fmt = QTextCharFormat()
            fmt.setBackground(diff_added_bg())
            fmt.setForeground(diff_added_fg())
            self.setFormat(0, len(text), fmt)
        elif text.startswith('-') and not text.startswith('---'):
            fmt = QTextCharFormat()
            fmt.setBackground(diff_removed_bg())
            fmt.setForeground(diff_removed_fg())
            self.setFormat(0, len(text), fmt)
        elif text.startswith('@@') or text.startswith('---') or text.startswith('+++'):
            fmt = QTextCharFormat()
            fmt.setForeground(diff_header_color())
            fmt.setFontWeight(QFont.Bold)
            self.setFormat(0, len(text), fmt)


class DiffViewer(QDialog):
    """
    Diff viewer dialog for showing proposed changes.
    
    Features:
    - Side-by-side or unified diff view
    - Accept/Reject buttons
    - Syntax highlighting for diffs
    """
    
    def __init__(self, 
                 original: str, 
                 proposed: str, 
                 title: str = "Review Changes",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(800, 500)
        
        self._original = original
        self._proposed = proposed
        self._accepted = False
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Review the proposed changes:</b>"))
        header.addStretch()
        layout.addLayout(header)
        
        # Splitter for side-by-side view
        h_splitter = QSplitter(Qt.Horizontal)
        
        # Original
        from PySide6.QtWidgets import QGroupBox
        original_group = QGroupBox("Original")
        original_layout = QVBoxLayout(original_group)
        self.original_view = QPlainTextEdit()
        self.original_view.setFont(QFont("Consolas", 10))
        self.original_view.setReadOnly(True)
        self.original_view.setPlainText(self._original)
        original_layout.addWidget(self.original_view)
        h_splitter.addWidget(original_group)
        
        # Proposed
        proposed_group = QGroupBox("Proposed")
        proposed_layout = QVBoxLayout(proposed_group)
        self.proposed_view = QPlainTextEdit()
        self.proposed_view.setFont(QFont("Consolas", 10))
        self.proposed_view.setReadOnly(True)
        self.proposed_view.setPlainText(self._proposed)
        proposed_layout.addWidget(self.proposed_view)
        h_splitter.addWidget(proposed_group)
        
        # Unified diff view
        diff_group = QGroupBox("Unified Diff")
        diff_layout = QVBoxLayout(diff_group)
        
        self.diff_view = QPlainTextEdit()
        self.diff_view.setFont(QFont("Consolas", 10))
        self.diff_view.setReadOnly(True)
        
        # Add diff highlighter
        self.diff_highlighter = DiffHighlighter(self.diff_view.document())
        
        # Generate diff
        diff_text = self._generate_diff()
        self.diff_view.setPlainText(diff_text)
        
        diff_layout.addWidget(self.diff_view)
        
        # Vertical splitter: side-by-side on top, unified diff on bottom
        v_splitter = QSplitter(Qt.Vertical)
        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(diff_group)
        v_splitter.setStretchFactor(0, 2)
        v_splitter.setStretchFactor(1, 1)
        
        layout.addWidget(v_splitter, stretch=1)
        
        # Stats
        stats = self._calculate_stats()
        stats_label = QLabel(stats)
        layout.addWidget(stats_label)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.reject_btn)
        
        self.accept_btn = QPushButton("Accept Changes")
        self.accept_btn.clicked.connect(self._on_accept)
        self.accept_btn.setDefault(True)
        btn_layout.addWidget(self.accept_btn)
        
        layout.addLayout(btn_layout)
    
    def _generate_diff(self) -> str:
        """Generate unified diff."""
        original_lines = self._original.splitlines(keepends=True)
        proposed_lines = self._proposed.splitlines(keepends=True)
        
        diff = difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile='Original',
            tofile='Proposed',
            lineterm=''
        )
        
        return ''.join(diff)
    
    def _calculate_stats(self) -> str:
        """Calculate diff statistics."""
        original_lines = self._original.splitlines()
        proposed_lines = self._proposed.splitlines()
        
        added = 0
        removed = 0
        
        # Simple line count comparison
        diff = difflib.unified_diff(original_lines, proposed_lines)
        for line in diff:
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                removed += 1
        
        return f"Changes: +{added} lines, -{removed} lines"
    
    def _on_accept(self):
        """Accept the proposed changes."""
        self._accepted = True
        self.accept()
    
    def was_accepted(self) -> bool:
        """Check if changes were accepted."""
        return self._accepted
    
    def get_proposed(self) -> str:
        """Get the proposed content."""
        return self._proposed
    
    @staticmethod
    def show_diff(original: str, proposed: str, title: str = "Review Changes", parent=None) -> tuple:
        """
        Show diff dialog and return (accepted, proposed_content).
        
        Returns:
            Tuple of (accepted: bool, content: str)
        """
        dialog = DiffViewer(original, proposed, title, parent)
        result = dialog.exec()
        
        if result == QDialog.Accepted and dialog.was_accepted():
            return (True, proposed)
        return (False, original)
