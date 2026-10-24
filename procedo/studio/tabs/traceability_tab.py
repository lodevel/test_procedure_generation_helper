"""
Traceability Tab - Step to code mapping view (board S4).

Implements Section 9.4 of the spec: the mapping's state in a banner, the
procedure's steps (✓ when test.py has their block) beside test.py with each
step's marker in the gutter, the mapping issues under the code. No action
bar: the code card's own Save writes test.py.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSplitter, QVBoxLayout

from procedo.ui import ui_kit

from .base_tab import BaseTab
from ..widgets.python_highlighter import PythonSyntaxHighlighter
from ..core import ArtifactType, StepMarkerParser
from ..core.procedure_steps import numbered_steps
from ..widgets.editor_card import EditorCard
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts
from ..widgets.step_list import StepListCard, StepRow, step_rows

#: The steps card's width on board S4.
_STEPS_WIDTH = 340


class TraceabilityTab(BaseTab):
    """
    Traceability tab for viewing step to code mapping.

    Features:
    - Step list from JSON (or code fallback)
    - Code viewer, a step's block highlighted when the step is clicked
    - Mapping rebuild
    - Mismatch detection
    """

    tab_id = "traceability"

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._parser = StepMarkerParser()
        self._step_blocks = []
        self._json_steps = []

        self.status_label = QLabel()
        self.status_label.setObjectName("viewBanner")
        self._show_banner("neutral", "Click a step to jump to its code")
        layout.addWidget(self.status_label)

        self.steps_card = StepListCard("Procedure steps")
        self.steps_card.step_clicked.connect(self._on_step_selected)

        self.code_card = self.add_card(EditorCard(
            self, ArtifactType.TEST_CODE, "test.py", wrap=False,
            placeholder="No code file loaded"))
        self.code_editor = self.code_card.editor
        self.code_highlighter = PythonSyntaxHighlighter(self.code_editor.document())
        self.line_info = ui_kit.note(wrap=False)
        self.code_card.add_to_header(self.line_info)
        self.code_card.changed.connect(self._show_markers)
        self.code_card.saved.connect(self._on_code_saved)
        self.mismatch_label = ui_kit.note(wrap=True)
        issues = QHBoxLayout()
        issues.setContentsMargins(12, 8, 12, 8)
        issues.setSpacing(8)
        issues.addWidget(ui_kit.bold("Mapping issues"), 0, Qt.AlignmentFlag.AlignTop)
        issues.addWidget(self.mismatch_label, 1)
        self.code_card.body.addWidget(ui_kit.row_rule())
        self.code_card.body.addLayout(issues)

        # Find/Replace bar — only the code editor is searchable here;
        # the step list has its own rows.
        self.find_bar = FindReplaceBar(self)
        layout.addWidget(self.find_bar)
        install_find_shortcuts(self, [self.code_editor], self.find_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(10)
        splitter.addWidget(self.steps_card)
        splitter.addWidget(self.code_card)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([_STEPS_WIDTH, 500])
        layout.addWidget(splitter, stretch=1)

    def _show_banner(self, tone: str, text: str) -> None:
        ui_kit.set_pill(self.status_label, tone, text)

    def load_content(self):
        """Load the step mapping from JSON and code."""
        if not self.artifact_manager:
            return
        # Get JSON steps
        json_data = self.artifact_manager.get_json_parsed()
        if json_data and "steps" in json_data:
            # Flatten macro blocks → real numbered steps so the count and
            # numbering match the generated code (a @FOR body's steps are the
            # real steps; @ALLOC/@FOR headers are not numbered steps).
            self._json_steps = numbered_steps(json_data["steps"])
        else:
            self._json_steps = []
        super().load_content()
        self.line_info.setText("")
        self.code_editor.clear_highlight()
        self._rebuild_mapping()

    def _rebuild_mapping(self) -> None:
        """The step list, the gutter's step markers and the mismatches, from the code shown."""
        self._show_markers()
        self._build_step_list()
        self._check_mismatches()

    def _show_markers(self) -> None:
        """Each step's ``▸N`` in the code's gutter, on its ``# Step N`` line."""
        self._step_blocks = self._parser.parse(self.code_editor.toPlainText())
        self.code_editor.set_marks({b.start_line: f"▸{b.step_number}" for b in self._step_blocks})

    def _build_step_list(self):
        """Build the step list combining JSON and code info."""
        blocks_by_n = {b.step_number: b for b in self._step_blocks}
        # Use JSON steps as primary source
        if self._json_steps:
            # Operator-readable step text comes from procedure_text.md
            # (v2.0.1 op-call JSON steps carry no top-level "text").
            rows = step_rows(self._json_steps, blocks_by_n,
                             self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT))
            mapped = sum(1 for row in rows if row.block)
            total = len(self._json_steps)
            self.steps_card.show_steps(
                rows, f"{total} steps from procedure.json · click one to jump to its code",
                ("success" if mapped == total else "warning", f"{mapped} of {total} mapped"))
        # If no JSON, use code steps
        elif self._step_blocks:
            rows = [StepRow(b.step_number, "(from code)", b) for b in self._step_blocks]
            self.steps_card.show_steps(rows, f"{len(rows)} steps from code (no JSON)")
        else:
            self.steps_card.show_steps([], "No steps found")

    def _check_mismatches(self):
        """Check for mismatches between JSON and code."""
        if not self._json_steps:
            self.mismatch_label.setText("No steps in procedure.json")
            self._show_banner("neutral", "Click a step to jump to its code")
            return

        # Expected = the real step numbers (n), not 1..len — macro-block
        # bodies make n run past the top-level array length.
        expected_steps = sorted(
            s["n"] for s in self._json_steps
            if isinstance(s, dict) and isinstance(s.get("n"), int)
        )
        code_content = self.code_editor.toPlainText()

        if not code_content:
            self.mismatch_label.setText("No code file to check")
            self._show_banner("warning", "⚠ Mapping has issues — no test.py")
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
            self._show_banner("warning", "⚠ Mapping has issues")
        else:
            self.mismatch_label.setText("None detected")
            self._show_banner("success", "✓ Mapping OK — all steps have code blocks")

    def _on_step_selected(self, step_num: int):
        """Handle step selection - jump to step in code with highlighting."""
        block = next((b for b in self._step_blocks if b.step_number == step_num), None)
        if block is None:
            self.line_info.setText(f"Step {step_num} · no code block")
            self.line_info.setToolTip("")
            self.code_editor.clear_highlight()
            return
        self.code_editor.jump_to_line(block.start_line)
        self.code_editor.highlight_lines(block.start_line, block.end_line)
        self.line_info.setText(f"Step {step_num} · lines {block.start_line}–{block.end_line}")
        # The step's text (procedure_text.md first) in the tooltip.
        row = next((r for r in step_rows(self._json_steps, {},
                                         self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT))
                    if r.n == step_num), None)
        self.line_info.setToolTip(row.text if row else "")

    def _on_code_saved(self):
        """Saved: re-read the step blocks and refresh the list and the mismatches."""
        self._rebuild_mapping()

    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()

    def refresh(self):
        """Refresh the mapping."""
        self.load_content()
