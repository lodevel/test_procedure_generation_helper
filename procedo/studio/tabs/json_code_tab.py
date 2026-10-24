"""
JSON-Code Tab - Paired editors for JSON↔Code transformation (board S3).

Left: JSON editor (procedure.json)
Middle: the steps (✓ when test.py has their block)
Right: Code editor (test.py)

Actions support bidirectional transformation.
"""

import logging
import json
from pathlib import Path
from typing import Any
from PySide6.QtWidgets import QVBoxLayout, QSplitter
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from .json_tab import JsonSyntaxHighlighter
from .code_tab import PythonSyntaxHighlighter
from .pack_gating import PackGatingMixin
from .pinned_constants import keep_pinned_constants
from ..core import ArtifactType, StepMarkerParser
from ..core.procedure_steps import numbered_steps
from ..llm import TabContext, LLMTask
from ..pack_ops import engine_async
from ..widgets.editor_card import EditorCard
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts
from ..widgets.step_list import StepListCard, step_rows

log = logging.getLogger(__name__)

#: The steps card's width on board S3.
_STEPS_WIDTH = 210

class JsonCodeTab(PackGatingMixin, LLMTabMixin, BaseTab):
    """
    JSON-Code tab showing both artifacts side-by-side.
    
    LEFT: JSON editor (procedure.json)
    MIDDLE: the steps, ✓ when test.py has their block (click: jump in both)
    RIGHT: Code editor (test.py)
    
    Actions:
    - JSON → Code: Primary direction (generate executable test from procedure)
    - Code → JSON: Reverse direction (reverse-engineer JSON from existing code)
    - Coherence checks between artifacts
    """
    
    # Tab identifier for button label management
    tab_id = "json_code"
    _no_pack_tip = "No code generator for {names}. Use the LLM workflow."
    _proposal_fields = (("procedure_json", "procedure_json"), ("test_code", "test_code"))
    
    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        
        # Initialize TabContext for this tab
        self.tab_context = TabContext(
            tab_id="json_code",
            backend_factory=main_window.backend_factory,
            project_manager=main_window.project_manager,
            artifact_manager=main_window.artifact_manager,
            task_config_manager=main_window.task_config_manager,
        )
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._parser = StepMarkerParser()
        # The step list is sourced from procedure_json["steps"] (canonical
        # upstream of code). This dict maps each JSON step's `n` to its
        # code-side StepBlock (if any) so click jumps can position the code
        # editor. Repopulated by _update_step_markers on every re-render.
        self._code_blocks_by_n: dict[int, Any] = {}
        # The project's functions as a Call step's tooltip shows them (by Call name).
        self._call_hints: dict[str, str] = {}
        self._hints_asked: tuple = (0, None)   # (request number, project root) of the answer awaited

        self.json_card = self.add_card(EditorCard(
            self, ArtifactType.PROCEDURE_JSON, "procedure.json", wrap=False))
        self.json_editor = self.json_card.editor
        self.json_highlighter = JsonSyntaxHighlighter(self.json_editor.document())

        self.steps_card = StepListCard("Steps")
        self.steps_card.step_clicked.connect(self._on_step_clicked)

        self.code_card = self.add_card(EditorCard(
            self, ArtifactType.TEST_CODE, "test.py", wrap=False))
        self.code_editor = self.code_card.editor
        self.code_highlighter = PythonSyntaxHighlighter(self.code_editor.document())
        self.code_card.changed.connect(self._update_step_markers)

        # Find/Replace bar — Ctrl+F / Ctrl+H target the focused editor
        # (or the leftmost: json_editor) when invoked.
        self.find_bar = FindReplaceBar(self)
        layout.addWidget(self.find_bar)
        install_find_shortcuts(
            self, [self.json_editor, self.code_editor], self.find_bar,
        )

        # Main splitter: JSON | Steps | Code
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(10)
        for card in (self.json_card, self.steps_card, self.code_card):
            splitter.addWidget(card)
        self.steps_card.setMinimumWidth(_STEPS_WIDTH)
        for index, stretch in enumerate((5, 0, 6)):  # the board's flex 1 | 210px | 1.2
            splitter.setStretchFactor(index, stretch)
        splitter.setSizes([400, _STEPS_WIDTH, 480])
        layout.addWidget(splitter, stretch=1)

        self.save_both_btn = self.create_button("Save Both", self.save_all_artifacts,
            tooltip="Save both JSON and code artifacts")
        self.format_json_btn = self.create_button("Format JSON", self._on_format_json,
            tooltip="Auto-format JSON with proper indentation")
        # Quick Code — hidden until the project provides a code generator
        self.quick_code_btn = self._pack_button(
            "⚡ Quick Code", self._on_quick_code,
            "Generate test code from JSON without LLM (rule-based, instant)")
        layout.addWidget(self._create_action_bar(
            [self.save_both_btn, self.format_json_btn], [self.quick_code_btn]))
        self.json_card.changed.connect(self._regate_pack_buttons)  # gates Quick Code above

    # Task callback maps for dynamic LLM button building (see TaskButtonRow.rebuild)
    
    def _get_task_callback_map(self) -> dict:
        """Return mapping of task_id -> (callback, tooltip)."""
        return {
            LLMTask.GENERATE_CODE_FROM_JSON.value: (self._on_generate_code, "Generate executable test code from JSON procedure"),
            LLMTask.DERIVE_JSON_FROM_CODE.value: (self._on_derive_json, "Extract JSON structure from existing test code"),
            LLMTask.REVIEW_JSON.value: (self._on_review_json, "Review JSON structure and content"),
            LLMTask.REVIEW_CODE.value: (self._on_review_code, "Review test code quality and structure"),
            LLMTask.REVIEW_CODE_VS_JSON.value: (self._on_check_coherence, "Check coherence between JSON and code artifacts"),
        }
    
    def _get_force_callback_map(self) -> dict:
        """Return mapping of task_id -> (force_callback, tooltip) for force-mode buttons."""
        return {
            LLMTask.GENERATE_CODE_FROM_JSON.value: (self._on_force_generate_code, "Force generate code (bypass validation checks)"),
            LLMTask.DERIVE_JSON_FROM_CODE.value: (self._on_force_derive_json, "Force derive JSON (bypass validation checks)"),
        }
    
    def _pack_gates(self):
        return [(self.quick_code_btn, "Deterministic JSON → Code", ("codegen",))]

    def _gated_equipment_types(self, project_root) -> list[str]:
        doc = json.loads(self.json_editor.toPlainText() or "{}")
        return [e.get("type") for e in (doc.get("equipment") or [])
                if isinstance(e, dict) and e.get("type")]

    def _on_generate_code(self):
        """JSON → Code transformation (strict mode)."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True)

    def _on_force_generate_code(self):
        """JSON → Code transformation (force mode)."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False)

    def _on_derive_json(self):
        """Code → JSON transformation (strict mode)."""
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty. Write code first.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_CODE, strict_mode=True)

    def _on_force_derive_json(self):
        """Code → JSON transformation (force mode)."""
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty. Write code first.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_CODE, strict_mode=False)

    def _on_review_json(self):
        """Review JSON with LLM."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_JSON)

    def _on_review_code(self):
        """Review code with LLM."""
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_CODE)

    def _on_check_coherence(self):
        """Check JSON↔Code coherence."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        if not self.code_editor.toPlainText():
            self.show_warning("No Code", "Code editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_CODE_VS_JSON)
    
    def _apply_proposals(self, response, task=None):
        """Dispatch per-artifact proposals from LLM response.

        ``task`` is accepted for a uniform mixin call signature but
        ignored — this tab proposes only JSON and code, neither of which
        goes through section-ownership reconstruction.
        """
        if response.procedure_json and response.procedure_json.mode:
            self._handle_json_proposal(response.procedure_json)
        if response.test_code and response.test_code.mode:
            self._handle_code_proposal(response.test_code)

    def _handle_code_proposal(self, proposal):
        """Offer a test_code proposal (``self.code_card``) as a proposal card."""
        # Accept "create" alongside "replace" — see text_json_tab for
        # rationale. "patch" stays excluded.
        if proposal.mode in ("replace", "create"):
            # Serialize dict to string if needed
            if isinstance(proposal.content, dict):
                content_str = json.dumps(proposal.content, indent=2)
            else:
                content_str = str(proposal.content)

            try:
                procedure = json.loads(self.artifact_manager.procedure_json.content or "{}")
            except json.JSONDecodeError:
                procedure = None
            content_str = keep_pinned_constants(
                content_str, self.code_editor.toPlainText(), procedure, "test.py proposal")
            self.propose_edit(self.code_card, content_str,
                              test=self._ensure_run_state().test)

    def _on_quick_code(self):
        """Deterministic JSON → Code without LLM (rule-based, instant)."""
        from ..pack_ops import pack_parsers
        sent = self.json_editor.toPlainText()
        json_text = sent.strip()
        if not json_text:
            self.show_warning("No Content", "JSON editor is empty. Add JSON before generating code.")
            return
        try:
            procedure = json.loads(json_text)
        except json.JSONDecodeError as e:
            self.show_error("Invalid JSON", f"Cannot parse procedure JSON:\n\n{e}")
            return

        project_root = getattr(self.project_manager, "project_root", None)
        engine_async.call(
            project_root, lambda: pack_parsers.generate_code(procedure, project_root),
            lambda answer: self._quick_code_done(sent, procedure, answer),
            button=self.quick_code_btn, cue=self.code_card.loading, cue_label="Generating code\u2026")

    def _quick_code_done(self, sent: str, procedure: dict, answer) -> None:
        from procedo.engine_client import engine_session
        try:
            code_str, warnings = answer()
        except engine_session.ParserUnavailable as e:
            self.show_warning("Code Generator Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Quick Code: codegen raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e,
                title="Quick Code — validator findings",
                intro=(
                    "The deterministic JSON→Code generator rejected the input. "
                    "Fix the issues below or fall back to the LLM workflow."
                ),
                parent=self,
            )
            return
        if not engine_async.unchanged(self.json_editor, sent, self.status_message.emit):
            return

        code_str = keep_pinned_constants(
            code_str, self.code_editor.toPlainText().strip(), procedure, "Quick Code")

        def done() -> None:
            if warnings:
                warn_lines = "\n".join(f"  • {w}" for w in warnings)
                self.main_window.dock.chat_panel.add_system_message(
                    f"⚡ Quick Code complete — {len(warnings)} warning(s):\n{warn_lines}",
                    tone="warning",
                )
            else:
                self.main_window.dock.chat_panel.add_system_message(
                    "⚡ Quick Code complete — no warnings.", tone="success"
                )
            self.status_message.emit("⚡ Quick Code complete")

        self.propose_edit(self.code_card, code_str, done=done,
                          rejected="✗ Quick Code — changes rejected.", ask_when_empty=False)
    
    def _on_step_clicked(self, step_num: int):
        """Click a step → jump in BOTH editors (JSON always; code when
        a matching block exists, its lines highlighted).

        Code blocks are looked up via the per-render cache populated by
        :meth:`_update_step_markers`. Per the v2.0.1 invariant (JSON
        upstream of code), the step is always in JSON; the only
        possible mismatch is "JSON has step, code doesn't yet" — the
        code jump is skipped for those rows.
        """
        block = self._code_blocks_by_n.get(step_num)
        if block:
            self.code_editor.jump_to_line(block.start_line)
            self.code_editor.highlight_lines(block.start_line, block.end_line)
        else:
            self.code_editor.clear_highlight()
        # JSON editor — always. The step exists in JSON by construction.
        self._focus_json_step(step_num)

    def _focus_json_step(self, step_num: int) -> None:
        """Scroll the JSON editor to the step's ``"n": <num>`` line and
        select it. The step is guaranteed to be in JSON (it was sourced
        from JSON); the only way to miss is an operator manually
        reformatting the JSON off the canonical ``indent=2`` shape, in
        which case we log debug and no-op rather than guess."""
        import re
        json_text = self.json_editor.toPlainText()
        # Line-anchored match on the indented JSON shape produced by
        # `json.dumps(..., indent=2)`. Word-boundary on the number
        # prevents matching `"n": 12` when looking for step 1.
        pattern = re.compile(rf'^\s*"n":\s*{step_num}\b', re.MULTILINE)
        m = pattern.search(json_text)
        if m is None:
            log.debug(
                "Could not locate step %d in JSON editor (non-canonical "
                "indent?); no-op'ing the jump.",
                step_num,
            )
            return
        line_start = json_text.rfind("\n", 0, m.start()) + 1
        cursor = self.json_editor.textCursor()
        cursor.setPosition(line_start)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine,
            QTextCursor.MoveMode.KeepAnchor,
        )
        self.json_editor.setTextCursor(cursor)
        self.json_editor.centerCursor()

    def _update_step_markers(self):
        """Rebuild the step list from procedure.json's ``steps[]`` (the
        canonical upstream of code). Code markers are consulted only to
        annotate which steps codegen has produced: ✓ when test.py has a
        ``# Step N`` block for the step, ✗ when not (regenerate test.py).
        """
        self._code_blocks_by_n = {}

        # JSON is the source of truth for the step list.
        json_steps: list[Any] = []
        try:
            json_content = self.json_editor.toPlainText()
            if json_content.strip():
                json_data = json.loads(json_content)
                # Loop / if bodies hold numbered steps too (codegen emits them).
                json_steps = numbered_steps(json_data.get("steps", []) or [])
        except (json.JSONDecodeError, AttributeError):
            # best-effort: editor JSON may be mid-edit/invalid or non-dict;
            # empty json_steps falls through to the "No steps in JSON" note.
            pass
        if not json_steps:
            self.steps_card.show_steps([], "No steps in JSON")
            return

        # Secondary: code-marker map for click→code-jump.
        code = self.code_editor.toPlainText()
        if code:
            for block in self._parser.parse(code):
                self._code_blocks_by_n[block.step_number] = block

        rows = step_rows(json_steps, self._code_blocks_by_n,
                         self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT), self._call_hints)
        with_code = sum(1 for row in rows if row.block)
        total = len(json_steps)
        pill = (("success", f"{total} = {with_code}") if with_code == total
                else ("warning", f"{with_code} of {total}"))
        self.steps_card.show_steps(rows, f"{total} steps in JSON · {with_code} in code", pill)

    def load_content(self):
        """Load both artifacts into editors."""
        super().load_content()
        if self.artifact_manager:
            self._update_step_markers()
            self._fetch_call_hints()

    def _fetch_call_hints(self) -> None:
        """Asks the project's engine for its functions' signatures, off the GUI thread, each
        time the tab loads (a function file may have changed); the steps re-show with them."""
        from ..pack_ops import call_hints
        root = getattr(self.project_manager, "project_root", None)
        asked = self._hints_asked = (self._hints_asked[0] + 1, root)
        if root is None or not (Path(root) / "functions").is_dir():
            self._call_hints = {}
            return
        engine_async.call(root, lambda: call_hints.function_signatures(Path(root)),
                          lambda answer: self._show_call_hints(asked, answer),
                          cue=self.main_window.loading, cue_label="Reading function signatures\u2026")

    def _show_call_hints(self, asked: tuple, answer) -> None:
        """The signatures asked as ``asked``; a late answer (asked again since, another project)
        is dropped, and so is one for a tab already closed."""
        root = getattr(self.project_manager, "project_root", None)
        if asked != self._hints_asked or asked[1] != root:
            return
        self._call_hints = answer()
        try:
            self._update_step_markers()
        except RuntimeError:  # the window closed meanwhile: its widgets are gone
            pass

    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh both artifacts."""
        self.load_content()
