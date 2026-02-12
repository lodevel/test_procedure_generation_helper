"""
Traceability Tab - Step to code mapping view.

Implements Section 9.4 of the spec.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox, 
    QPushButton, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QTextEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor, QColor, QBrush, QTextFormat
import textwrap

from .base_tab import BaseTab
from ..core import ArtifactType, StepMarkerParser


class TraceabilityTab(BaseTab):
    """
    Traceability tab for viewing step to code mapping.
    
    Features:
    - Step list from JSON (or code fallback)
    - Code block viewer for selected step
    - Mapping rebuild
    - Mismatch detection
    """
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Header with status
        header = QHBoxLayout()
        self.status_label = QLabel("Click a step to jump to its code")
        header.addWidget(self.status_label)
        header.addStretch()
        
        self.rebuild_btn = self.create_button("Rebuild Mapping", self._on_rebuild)
        self.explain_btn = self.create_button("Ask LLM to Explain", self._on_explain_mismatches)
        header.addWidget(self.rebuild_btn)
        header.addWidget(self.explain_btn)
        
        layout.addLayout(header)
        
        # Splitter: step list | code viewer
        splitter = QSplitter(Qt.Horizontal)
        
        # Step list
        steps_group = QGroupBox("Procedure Steps")
        steps_layout = QVBoxLayout(steps_group)
        
        self.step_list = QListWidget()
        self.step_list.itemClicked.connect(self._on_step_selected)
        steps_layout.addWidget(self.step_list)
        
        self.step_count_label = QLabel("")
        steps_layout.addWidget(self.step_count_label)
        
        splitter.addWidget(steps_group)
        
        # Code viewer (editable)
        code_group = QGroupBox("Full Code (with step markers)")
        code_layout = QVBoxLayout(code_group)
        
        self.code_header = QLabel("Click a step to jump to it in the code")
        code_layout.addWidget(self.code_header)
        
        self.code_editor = QPlainTextEdit()
        self.code_editor.setFont(QFont("Consolas", 10))
        self.code_editor.setLineWrapMode(QPlainTextEdit.NoWrap)  # Enable horizontal scrolling
        self.code_editor.textChanged.connect(self._on_code_changed)
        code_layout.addWidget(self.code_editor)
        
        # Code action buttons
        code_btn_layout = QHBoxLayout()
        self.save_code_btn = QPushButton("💾 Save Code")
        self.save_code_btn.setEnabled(False)
        self.save_code_btn.clicked.connect(self._on_save_code)
        code_btn_layout.addWidget(self.save_code_btn)
        code_btn_layout.addStretch()
        code_layout.addLayout(code_btn_layout)
        
        self.line_info = QLabel("")
        code_layout.addWidget(self.line_info)
        
        splitter.addWidget(code_group)
        splitter.setSizes([300, 500])
        
        layout.addWidget(splitter, stretch=1)
        
        # Mismatch summary
        self.mismatch_group = QGroupBox("Mapping Issues")
        mismatch_layout = QVBoxLayout(self.mismatch_group)
        self.mismatch_label = QLabel("No issues detected")
        mismatch_layout.addWidget(self.mismatch_label)
        self.mismatch_group.setVisible(False)
        
        layout.addWidget(self.mismatch_group)
        
        # Initialize
        self._parser = StepMarkerParser()
        self._step_blocks = []
        self._json_steps = []
        self._code_modified = False
    
    def _on_rebuild(self):
        """Rebuild the mapping."""
        self._load_mapping()
    
    def _on_explain_mismatches(self):
        """Ask LLM to explain mismatches."""
        self.main_window.run_llm_task("review_code_vs_json")
    
    def _load_mapping(self):
        """Load step mapping from JSON and code."""
        self.step_list.clear()
        
        # Get JSON steps
        json_data = self.artifact_manager.get_json_parsed()
        if json_data and "steps" in json_data:
            self._json_steps = json_data["steps"]
        else:
            self._json_steps = []
        
        # Get code step blocks
        code = self.artifact_manager.get_content(ArtifactType.TEST_CODE)
        if code:
            self._step_blocks = self._parser.parse(code)
            # Show full code (block signals to avoid triggering change flag)
            self.code_editor.blockSignals(True)
            self.code_editor.setPlainText(code)
            self.code_editor.blockSignals(False)
            self._code_modified = False
            self.save_code_btn.setEnabled(False)
        else:
            self._step_blocks = []
            self.code_editor.blockSignals(True)
            self.code_editor.setPlainText("# No code file loaded")
            self.code_editor.blockSignals(False)
        
        # Build unified step list
        self._build_step_list()
        
        # Check for mismatches
        self._check_mismatches()
    
    def _build_step_list(self):
        """Build the step list combining JSON and code info."""
        # Use JSON steps as primary source
        if self._json_steps:
            for i, step in enumerate(self._json_steps):
                step_num = i + 1
                
                if isinstance(step, dict):
                    text = step.get("text", str(step))[:60]
                else:
                    text = str(step)[:60]
                
                # Check if code has this step
                has_code = any(b.step_number == step_num for b in self._step_blocks)
                
                item = QListWidgetItem()
                if has_code:
                    item.setText(f"Step {step_num} ✓ : {text}")
                    item.setForeground(Qt.darkGreen)
                else:
                    item.setText(f"Step {step_num} ✗ : {text}")
                    item.setForeground(Qt.red)
                
                item.setData(Qt.UserRole, step_num)
                self.step_list.addItem(item)
            
            self.step_count_label.setText(f"{len(self._json_steps)} steps from JSON")
        
        # If no JSON, use code steps
        elif self._step_blocks:
            for block in self._step_blocks:
                item = QListWidgetItem(f"Step {block.step_number} (from code)")
                item.setData(Qt.UserRole, block.step_number)
                self.step_list.addItem(item)
            
            self.step_count_label.setText(f"{len(self._step_blocks)} steps from code (no JSON)")
        
        else:
            self.step_count_label.setText("No steps found")
    
    def _check_mismatches(self):
        """Check for mismatches between JSON and code."""
        if not self._json_steps:
            self.mismatch_group.setVisible(False)
            return
        
        expected_steps = list(range(1, len(self._json_steps) + 1))
        code_content = self.artifact_manager.get_content(ArtifactType.TEST_CODE)
        
        if not code_content:
            self.mismatch_label.setText("No code file to check")
            self.mismatch_group.setVisible(True)
            return
        
        missing = self._parser.find_missing_steps(code_content, expected_steps)
        extra = self._parser.find_extra_steps(code_content, expected_steps)
        
        issues = []
        if missing:
            issues.append(f"Missing in code: Steps {', '.join(map(str, missing))}")
        if extra:
            issues.append(f"Extra in code: Steps {', '.join(map(str, extra))}")
        
        if issues:
            self.mismatch_label.setText("\n".join(issues))
            self.mismatch_group.setVisible(True)
            self.status_label.setText("⚠ Mapping has issues")
            self.status_label.setStyleSheet("color: orange;")
        else:
            self.mismatch_group.setVisible(False)
            self.status_label.setText("✓ Mapping OK - All steps have code blocks")
            self.status_label.setStyleSheet("color: green;")
    
    def _on_step_selected(self, item: QListWidgetItem):
        """Handle step selection - jump to step in code with highlighting."""
        step_num = item.data(Qt.UserRole)
        
        # Find the code block
        block = None
        for b in self._step_blocks:
            if b.step_number == step_num:
                block = b
                break
        
        if block:
            # Show step text from JSON (wrapped at 80 chars)
            if self._json_steps and step_num <= len(self._json_steps):
                step_data = self._json_steps[step_num - 1]
                if isinstance(step_data, dict):
                    step_text = step_data.get("text", "")
                else:
                    step_text = str(step_data)
                # Wrap text at 80 chars for display
                wrapped_text = '\n'.join(textwrap.wrap(step_text, width=80))
                self.code_header.setText(f"STEP {step_num}: {wrapped_text}")
                self.code_header.setToolTip(step_text)  # Full text in tooltip
            else:
                self.code_header.setText(f"Jumped to STEP {step_num}")
            
            # Jump to the step marker in the full code view
            cursor = self.code_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            for _ in range(block.start_line - 1):
                cursor.movePosition(QTextCursor.MoveOperation.Down)
            self.code_editor.setTextCursor(cursor)
            self.code_editor.centerCursor()
            
            # Highlight the step marker line with light yellow background
            self._highlight_step_lines(block.start_line, block.end_line)
            
            self.line_info.setText(f"Lines {block.start_line} - {block.end_line}")
        else:
            self.code_header.setText(f"STEP {step_num}: No code block found")
            self.line_info.setText("")
            # Clear highlighting
            self.code_editor.setExtraSelections([])
    
    def _highlight_step_lines(self, start_line: int, end_line: int):
        """Highlight lines in code editor with light yellow background."""
        selections = []
        highlight_color = QColor("#fffacd")  # Light yellow
        
        doc = self.code_editor.document()
        
        for line_num in range(start_line, end_line + 1):
            block = doc.findBlockByLineNumber(line_num - 1)  # 0-indexed
            if block.isValid():
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(QBrush(highlight_color))
                selection.format.setProperty(QTextFormat.FullWidthSelection, True)
                selection.cursor = QTextCursor(block)
                selection.cursor.clearSelection()
                selections.append(selection)
        
        self.code_editor.setExtraSelections(selections)
    
    def _on_code_changed(self):
        """Handle code editor text changes."""
        self._code_modified = True
        self.save_code_btn.setEnabled(True)
    
    def _on_save_code(self):
        """Save the edited code back to the artifact."""
        code = self.code_editor.toPlainText()
        self.artifact_manager.set_content(ArtifactType.TEST_CODE, code)
        self._code_modified = False
        self.save_code_btn.setEnabled(False)
        
        # Re-parse step blocks after save
        self._step_blocks = self._parser.parse(code)
        
        # Refresh the step list
        self.step_list.clear()
        self._build_step_list()
        self._check_mismatches()
        
        self.line_info.setText("Code saved!")
    
    def on_activated(self):
        """Called when tab becomes active."""
        self._load_mapping()
    
    def refresh(self):
        """Refresh the mapping."""
        self._load_mapping()
