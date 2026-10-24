"""
Text-JSON Tab - Paired editors for Text↔JSON transformation (board S2).

Left: Text editor (procedure_text.md)
Right: JSON editor (procedure.json)

Actions support bidirectional transformation.
"""

import logging
import json

from PySide6.QtWidgets import QVBoxLayout, QSplitter, QLabel
from PySide6.QtCore import Qt

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from .json_tab import JsonSyntaxHighlighter
from .text_actions import TextActionsMixin
from ..core import ArtifactType
from ..llm import TabContext, LLMTask
from ..pack_ops import engine_async
from ..widgets.editor_card import EditorCard
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts
from ..widgets.text_highlighter import ProcedureTextHighlighter

from procedo.ui import ui_kit

log = logging.getLogger(__name__)

class TextJsonTab(TextActionsMixin, LLMTabMixin, BaseTab):
    """
    Text-JSON tab showing both artifacts side-by-side.

    LEFT: Text editor (procedure_text.md)
    RIGHT: JSON editor (procedure.json)

    Actions:
    - Text → JSON: Primary direction (derive structured JSON from natural language)
    - JSON → Text: Reverse direction (render human-readable text from JSON)
    - Coherence checks between both artifacts
    """

    # Tab identifier for button label management
    tab_id = "text_json"

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)

        # Initialize TabContext for this tab
        self.tab_context = TabContext(
            tab_id="text_json",
            backend_factory=main_window.backend_factory,
            project_manager=main_window.project_manager,
            artifact_manager=main_window.artifact_manager,
            task_config_manager=main_window.task_config_manager,
        )

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.text_card = self.add_card(EditorCard(
            self, ArtifactType.PROCEDURE_TEXT, "procedure_text.md",
            placeholder=(
                "Write your test procedure here in natural language...\n\n"
                "Example:\n"
                "Test Name: DC Voltage Measurement\n\n"
                "Equipment needed:\n"
                "- DMM (Digital Multimeter)\n"
                "- Power supply\n\n"
                "Steps:\n"
                "1. Connect DMM to test point TP1\n"
                "2. Apply 5V from power supply\n"
                "3. Measure voltage with DMM\n\n"
                "Expected:\n"
                "- Voltage reading: 5.0V ± 0.1V"
            ),
        ))
        self.text_card.add_to_header(ui_kit.set_pill(QLabel(), "neutral", "draft"))
        self.text_editor = self.text_card.editor
        self.text_highlighter = ProcedureTextHighlighter(self.text_editor.document())

        self.json_card = self.add_card(EditorCard(
            self, ArtifactType.PROCEDURE_JSON, "procedure.json", wrap=False))
        self.json_editor = self.json_card.editor
        self.json_highlighter = JsonSyntaxHighlighter(self.json_editor.document())

        # Find/Replace bar — Ctrl+F / Ctrl+H target the focused editor
        # (or the leftmost: text_editor) when invoked.
        self.find_bar = FindReplaceBar(self)
        layout.addWidget(self.find_bar)
        install_find_shortcuts(
            self, [self.text_editor, self.json_editor], self.find_bar,
        )

        # Main splitter: Text | JSON
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(10)
        splitter.addWidget(self.text_card)
        splitter.addWidget(self.json_card)
        layout.addWidget(splitter, stretch=1)

        self.save_both_btn = self.create_button("Save Both", self.save_all_artifacts,
            tooltip="Save both text and JSON artifacts")
        self.format_json_btn = self.create_button("Format JSON", self._on_format_json,
            tooltip="Auto-format JSON with proper indentation")
        # Deterministic ⚡ buttons — bidirectional, hidden until the
        # rules_packager_base wheel is importable in the project venv
        # (see refresh_parser_button).
        self.quick_parse_btn = self._pack_button(
            "⚡ Text → JSON", self._on_quick_parse,
            "Deterministic Text → JSON (rule-based, instant, no LLM)")
        self.quick_render_btn = self._pack_button(
            "⚡ JSON → Text", self._on_quick_render,
            "Deterministic JSON → Text (rule-based, instant, no LLM)")
        self.parse_and_generate_btn = self._pack_button(
            "⚡ Parse + Generate", self._on_parse_and_generate,
            "One click: Text → JSON → test.py (strict; aborts on any warning)")
        layout.addWidget(self._create_action_bar(
            [self.save_both_btn, *self._text_file_buttons(), self.format_json_btn],
            [self.quick_parse_btn, self.quick_render_btn, self.parse_and_generate_btn]))
        self.text_card.changed.connect(self._on_text_edited)  # gates the ⚡ buttons above

    # Task callback maps for dynamic LLM button building (see TaskButtonsMixin._build_llm_buttons)

    def _get_task_callback_map(self) -> dict:
        """Return mapping of task_id -> (callback, tooltip)."""
        return {
            LLMTask.DERIVE_JSON_FROM_TEXT.value: (self._on_derive_json, "Generate structured JSON from procedure text"),
            LLMTask.RENDER_TEXT_FROM_JSON.value: (self._on_render_text, "Generate human-readable text from JSON"),
            LLMTask.REVIEW_TEXT_PROCEDURE.value: (self._on_review_text, "Review procedure text for quality and completeness"),
            LLMTask.REVIEW_JSON.value: (self._on_review_json, "Review JSON structure and content"),
            LLMTask.REVIEW_TEXT_VS_JSON.value: (self._on_check_coherence, "Check coherence between text and JSON artifacts"),
        }

    def _get_force_callback_map(self) -> dict:
        """Return mapping of task_id -> (force_callback, tooltip) for force-mode buttons."""
        return {
            LLMTask.DERIVE_JSON_FROM_TEXT.value: (self._on_force_derive_json, "Force generate JSON (bypass validation checks)"),
        }

    def _pack_gates(self):
        return [
            (self.quick_parse_btn, "Deterministic Text → JSON", ("parse",)),
            (self.quick_render_btn, "Deterministic JSON → Text", ("parse", "emit")),
            (self.parse_and_generate_btn, "Parse + generate code", ("parse", "codegen")),
        ]

    def _on_quick_parse(self):
        """Deterministic Text → JSON without LLM (rule-based, instant)."""
        from ..pack_ops import pack_parsers
        sent = self.text_editor.toPlainText()
        text = sent.strip()
        if not text:
            self.show_warning("No Content", "Procedure text is empty. Add text before parsing.")
            return
        project_root = getattr(self.project_manager, "project_root", None)
        engine_async.call(
            project_root, lambda: pack_parsers.parse_text(text, project_root=project_root),
            lambda answer: self._quick_parse_done(sent, answer), button=self.quick_parse_btn,
            cue=self.json_card.loading, cue_label="Parsing\u2026")

    def _quick_parse_done(self, sent: str, answer) -> None:
        from procedo.engine_client import engine_session
        # pack_parsers.parse_text raises ParseFailure on grammar errors
        # (with .code, .fix_hint, .findings — same shape the legacy
        # ParseError carried) or ParserUnavailable if the wheel isn't
        # importable. Route both through the structured dialog.
        try:
            result, warnings = answer()
        except engine_session.ParserUnavailable as e:
            self.show_warning("Parser Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Quick Parse: parser raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e,
                title="Quick Parse — validator findings",
                intro=(
                    "The deterministic Text→JSON parser rejected the input. "
                    "Fix the issues below or fall back to the LLM workflow."
                ),
                parent=self,
            )
            return
        if not engine_async.unchanged(self.text_editor, sent, self.status_message.emit):
            return

        def done() -> None:
            if warnings:
                warn_lines = "\n".join(f"  • {w}" for w in warnings)
                self.main_window.dock.chat_panel.add_system_message(
                    f"⚡ Quick Parse complete — {len(warnings)} warning(s):\n{warn_lines}",
                    tone="warning",
                )
            else:
                self.main_window.dock.chat_panel.add_system_message(
                    "⚡ Quick Parse complete — no warnings.", tone="success"
                )
            self.status_message.emit("⚡ Quick Parse complete")

        # A JSON already there waits for the user's Accept (a proposal card); an empty one
        # takes the result at once.
        self.propose_edit(self.json_card, json.dumps(result, indent=2), done=done,
                          rejected="✗ Quick Parse — changes rejected.", ask_when_empty=False)

    def _on_quick_render(self):
        """Deterministic JSON → Text without LLM (rule-based, instant)."""
        from ..pack_ops import pack_parsers
        sent = self.json_editor.toPlainText()
        json_text = sent.strip()
        if not json_text:
            self.show_warning(
                "No JSON",
                "JSON editor is empty. Write JSON first or load a test "
                "with existing JSON.",
            )
            return

        try:
            procedure = json.loads(json_text)
        except json.JSONDecodeError as e:
            self.show_warning("Invalid JSON", f"JSON parse failed:\n{e}")
            return

        project_root = getattr(self.project_manager, "project_root", None)
        engine_async.call(
            project_root, lambda: pack_parsers.render_text(procedure, project_root=project_root),
            lambda answer: self._quick_render_done(sent, answer), button=self.quick_render_btn,
            cue=self.text_card.loading, cue_label="Rendering\u2026")

    def _quick_render_done(self, sent: str, answer) -> None:
        from procedo.engine_client import engine_session
        try:
            result_text = answer()
        except engine_session.ParserUnavailable as e:
            self.show_warning("Parser Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Quick Render: emitter raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e,
                title="Quick Render — emitter findings",
                intro=(
                    "The deterministic JSON→Text emitter rejected the "
                    "input. Fix the JSON or fall back to the LLM workflow."
                ),
                parent=self,
            )
            return
        if not engine_async.unchanged(self.json_editor, sent, self.status_message.emit):
            return

        def done() -> None:
            self.main_window.dock.chat_panel.add_system_message(
                "⚡ Quick Render complete.", tone="success"
            )
            self.status_message.emit("⚡ Quick Render complete")

        self.propose_edit(self.text_card, result_text, done=done,
                          rejected="✗ Quick Render — changes rejected.", ask_when_empty=False)

    def _on_derive_json(self):
        """Text → JSON transformation (strict mode)."""
        if not self.text_editor.toPlainText():
            self.show_warning("No Content", "Procedure text is empty. Add text before generating JSON.")
            return
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_TEXT, strict_mode=True)

    def _on_force_derive_json(self):
        """Text → JSON transformation (force mode)."""
        self._run_task_async(LLMTask.DERIVE_JSON_FROM_TEXT, strict_mode=False)

    def _on_render_text(self):
        """JSON → Text transformation."""
        if not self.json_editor.toPlainText():
            self.show_warning(
                "No JSON",
                "JSON editor is empty. Write JSON first or load a test with existing JSON."
            )
            return
        self._run_task_async(LLMTask.RENDER_TEXT_FROM_JSON)

    def _on_review_json(self):
        """Review JSON with LLM."""
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty. Write JSON first.")
            return
        self._run_task_async(LLMTask.REVIEW_JSON)

    def _on_check_coherence(self):
        """Check Text↔JSON coherence."""
        if not self.text_editor.toPlainText():
            self.show_warning("No Text", "Text editor is empty.")
            return
        if not self.json_editor.toPlainText():
            self.show_warning("No JSON", "JSON editor is empty.")
            return
        self._run_task_async(LLMTask.REVIEW_TEXT_VS_JSON)

    def _apply_proposals(self, response, task=None):
        """Dispatch per-artifact proposals from LLM response.

        ``task`` carries the per-task section-ownership override into the
        text-proposal reconstruction (None → bundle default). JSON
        proposals don't reconstruct, so the task isn't threaded there.
        """
        if response.procedure_text and response.procedure_text.mode:
            self._handle_text_proposal(response.procedure_text, task)
        if response.procedure_json and response.procedure_json.mode:
            self._handle_json_proposal(response.procedure_json)

    def _get_expected_artifact_fields(self) -> list[str]:
        """Get expected artifact fields for this tab."""
        return ["text_procedure", "procedure_json"]

    def _parse_response_to_dict(self, response) -> dict:
        """Parse LLMResponse into the dict format expected by _validate_output_contract."""
        parsed = {
            "assistant_message": response.assistant_message,
            "open_questions": response.session_delta.get("open_questions", []),
        }
        parsed["propose_update"] = False
        if response.procedure_text and response.procedure_text.mode:
            parsed["propose_update"] = True
            parsed["text_procedure"] = response.procedure_text.content
        if response.procedure_json and response.procedure_json.mode:
            parsed["propose_update"] = True
            parsed["procedure_json"] = response.procedure_json.content
        return parsed

    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()

    def refresh(self):
        """Refresh both artifacts."""
        self.load_content()
