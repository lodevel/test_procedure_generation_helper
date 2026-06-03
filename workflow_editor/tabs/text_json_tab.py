"""
Text-JSON Tab - Paired editors for Text↔JSON transformation.

Left: Text editor (procedure_text.md)
Right: JSON editor (procedure.json) + preview

Actions support bidirectional transformation.
"""

import logging
import json

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QLabel, QPlainTextEdit, QWidget
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from .json_tab import JsonSyntaxHighlighter
from ..core import ArtifactType
from ..llm import TabContext, LLMTask
from ..llm.reconstruction import reconstructed_or_error
from ..dialogs import DiffViewer
from ..theme import status_modified, status_saved
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts

log = logging.getLogger(__name__)

class TextJsonTab(LLMTabMixin, BaseTab):
    """
    Text-JSON tab showing both artifacts side-by-side.
    
    LEFT: Text editor (procedure_text.md)
    RIGHT: JSON editor (procedure.json) + preview panel
    
    Actions:
    - Text → JSON: Primary direction (derive structured JSON from natural language)
    - JSON → Text: Reverse direction (render human-readable text from JSON)
    - Coherence checks between both artifacts
    """
    
    content_changed = Signal()
    
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
            session_state=main_window.session_state
        )
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Main splitter: Text | JSON
        splitter = QSplitter(Qt.Horizontal)
        
        # LEFT: Text editor panel
        text_widget = self._create_text_panel()
        splitter.addWidget(text_widget)
        
        # RIGHT: JSON editor + preview panel
        json_widget = self._create_json_panel()
        splitter.addWidget(json_widget)
        
        splitter.setSizes([400, 600])  # Favor JSON side slightly
        layout.addWidget(splitter, stretch=1)

        # Find/Replace bar — Ctrl+F / Ctrl+H target the focused editor
        # (or the leftmost: text_editor) when invoked.
        self.find_bar = FindReplaceBar(self)
        layout.addWidget(self.find_bar)
        install_find_shortcuts(
            self, [self.text_editor, self.json_editor], self.find_bar,
        )

        # Actions row
        actions_layout = self._create_actions()
        layout.addLayout(actions_layout)

        # Initialize
        self._text_dirty = False
        self._json_dirty = False
    
    def _create_text_panel(self):
        """Create text editor panel (left side)."""
        text_group = QGroupBox("Procedure Text (Draft)")
        layout = QVBoxLayout(text_group)
        
        self.text_editor = QPlainTextEdit()
        self.text_editor.setFont(QFont("Consolas", 10))
        self.text_editor.setPlaceholderText(
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
        )
        self.text_editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.text_editor)
        
        self.text_status = QLabel("")
        layout.addWidget(self.text_status)
        
        return text_group
    
    def _create_json_panel(self):
        """Create JSON editor panel (right side)."""
        json_group = QGroupBox("procedure.json")
        json_layout = QVBoxLayout(json_group)
        
        self.json_editor = QPlainTextEdit()
        self.json_editor.setFont(QFont("Consolas", 10))
        self.json_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.json_editor.textChanged.connect(self._on_json_changed)
        
        # Syntax highlighting
        self.json_highlighter = JsonSyntaxHighlighter(self.json_editor.document())
        
        json_layout.addWidget(self.json_editor)
        
        self.json_status = QLabel("")
        json_layout.addWidget(self.json_status)
        
        return json_group
    
    # Task callback maps for dynamic LLM button building (see BaseTab._build_llm_buttons)
    
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
    
    def _create_actions(self):
        """Create action buttons organized into functional groups."""
        layout = QHBoxLayout()
        
        # File Operations Group (light blue)
        file_group = self.create_action_group("File Operations", "file")
        file_layout = QVBoxLayout(file_group)
        
        # Save buttons
        save_row = QHBoxLayout()
        self.save_text_btn = self.create_button("Save Text", self._on_save_text,
            tooltip="Save procedure text to disk")
        self.save_json_btn = self.create_button("Save JSON", self._on_save_json,
            tooltip="Save procedure JSON to disk")
        self.save_both_btn = self.create_button("Save Both", self._on_save_both,
            tooltip="Save both text and JSON artifacts")
        save_row.addWidget(self.save_text_btn)
        save_row.addWidget(self.save_json_btn)
        save_row.addWidget(self.save_both_btn)
        self.renumber_steps_btn = self.create_button(
            "Renumber steps", self._on_renumber_steps,
            tooltip="Rewrite the leading N. on every step in ## Steps to be sequential 1..N — useful after inserting a step in the middle",
        )
        save_row.addWidget(self.renumber_steps_btn)
        self.sync_equipment_btn = self.create_button(
            "Sync Equipment", self._on_sync_equipment,
            tooltip=(
                "Rescan ## Steps and REGENERATE ## Equipment from device "
                "references. The existing block is replaced — operator "
                "customizations are overwritten. Edit manually after Sync "
                "if you want bench headroom."
            ),
        )
        save_row.addWidget(self.sync_equipment_btn)
        self.sync_meta_btn = self.create_button(
            "Sync Meta", self._on_sync_meta,
            tooltip=(
                "Regenerate ## Meta (format_version / board / rules_pack / "
                "labscpi_pack / fncore_pack) from the active bundle — no LLM. "
                "board and format_version are preserved; pack versions re-pinned."
            ),
        )
        # Package-delivered feature: hidden until the availability refresh confirms
        # the installed wheel provides sync_meta_text (old wheel → no button).
        self.sync_meta_btn.setVisible(False)
        save_row.addWidget(self.sync_meta_btn)
        save_row.addStretch()
        file_layout.addLayout(save_row)
        
        # Format + validator buttons. Format is hard-coded (tab-local
        # behavior, no registry involvement); validator buttons come
        # from the registry via TaskConfigManager (Phase 2/3).
        format_row = QHBoxLayout()
        self.format_json_btn = self.create_button("Format JSON", self._on_format_json,
            tooltip="Auto-format JSON with proper indentation")
        format_row.addWidget(self.format_json_btn)
        self._build_validator_buttons(format_row)
        format_row.addStretch()
        file_layout.addLayout(format_row)

        # Deterministic ⚡ row — bidirectional. Both buttons hidden
        # until the rules_packager_base wheel is importable in the
        # project venv (see refresh_parser_button).
        parse_row = QHBoxLayout()
        self.quick_parse_btn = self.create_button(
            "⚡ Text → JSON", self._on_quick_parse,
            tooltip="Deterministic Text → JSON (rule-based, instant, no LLM)"
        )
        self.quick_parse_btn.setVisible(False)
        parse_row.addWidget(self.quick_parse_btn)
        self.quick_render_btn = self.create_button(
            "⚡ JSON → Text", self._on_quick_render,
            tooltip="Deterministic JSON → Text (rule-based, instant, no LLM)"
        )
        self.quick_render_btn.setVisible(False)
        parse_row.addWidget(self.quick_render_btn)
        self.parse_and_generate_btn = self.create_button(
            "⚡ Parse + Generate", self._on_parse_and_generate,
            tooltip="One click: Text → JSON → test.py (strict; aborts on any warning)"
        )
        self.parse_and_generate_btn.setVisible(False)
        parse_row.addWidget(self.parse_and_generate_btn)
        parse_row.addStretch()
        file_layout.addLayout(parse_row)
        
        layout.addWidget(file_group)
        
        # LLM Actions Group — built dynamically from BaseTab
        layout.addWidget(self._create_llm_action_group())
        
        return layout
    
    # Event handlers
    def _on_text_changed(self):
        """Handle text editor changes."""
        # Mirror live editor into artifact_manager — see text_only_tab
        # for the rationale. Empty-checks in _on_review_* read this.
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText()
        )
        self._text_dirty = True
        self._update_text_status()
        self.tab_context.mark_artifact_modified("procedure_text")
        self.content_changed.emit()
        # Re-gate the ⚡ buttons: the equipment set (hence pack capabilities
        # needed) can change as the operator edits the ## Equipment block.
        if getattr(self, "quick_parse_btn", None) is not None and self.quick_parse_btn.isVisible():
            project_root = getattr(self.project_manager, "project_root", None)
            self._apply_capability_gating(project_root)

    def _on_json_changed(self):
        """Handle JSON editor changes."""
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText()
        )
        self._json_dirty = True
        self._update_json_status()
        self.tab_context.mark_artifact_modified("procedure_json")
        self.content_changed.emit()
    
    def _on_save_text(self):
        """Save text artifact."""
        try:
            content = self.text_editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.PROCEDURE_TEXT, content)
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_TEXT)
            self.artifact_manager.procedure_text.mark_clean()
            self._text_dirty = False
            self._update_text_status()
            self.status_message.emit("Text saved successfully")
            self.artifact_saved.emit()
        except Exception as e:
            self.show_error("Save Failed", str(e))

    def _on_sync_equipment(self) -> None:
        """Rescan ``## Steps`` and regenerate ``## Equipment`` from
        scratch. Acts only on the LEFT (text) editor — the JSON
        editor's Equipment is downstream of the text via parse.
        Operator customizations on the existing block are
        overwritten. Confirmation + warnings posted to the chat panel."""
        from ..llm import pack_parsers
        from .text_only_tab import _post_to_chat, _format_chat_warnings
        original = self.text_editor.toPlainText()
        if not original:
            return
        try:
            project_root = getattr(self.project_manager, "project_root", None)
            new_text, warnings = pack_parsers.sync_equipment_from_steps(
                original, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as exc:
            self.show_error("Sync Equipment failed", str(exc))
            return
        if new_text == original:
            self.status_message.emit(
                "Sync Equipment: no changes (everything already declared)"
            )
            _post_to_chat(
                self.main_window,
                "🔧 Sync Equipment: no changes (## Equipment already matches "
                "every device referenced in steps)."
                + _format_chat_warnings(warnings),
            )
            return
        self.text_editor.setPlainText(new_text)
        self.status_message.emit("Equipment synced from steps")
        _post_to_chat(
            self.main_window,
            "🔧 Sync Equipment applied — ## Equipment regenerated from steps. "
            "Operator customizations were overwritten; re-add bench headroom "
            "manually if needed."
            + _format_chat_warnings(warnings),
        )

    def _on_sync_meta(self) -> None:
        """Regenerate the ``## Meta`` block (format_version / board + pack pins)
        from the active bundle, deterministically (no LLM). Acts only on the
        LEFT (text) editor; board / format_version are preserved."""
        from ..llm import pack_parsers
        from .text_only_tab import _post_to_chat, _format_chat_warnings
        original = self.text_editor.toPlainText()
        if not original:
            return
        try:
            project_root = getattr(self.project_manager, "project_root", None)
            new_text, warnings = pack_parsers.sync_meta_text(
                original, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as exc:
            self.show_error("Sync Meta failed", str(exc))
            return
        if new_text == original:
            self.status_message.emit(
                "Sync Meta: no changes (## Meta already matches the bundle)"
            )
            _post_to_chat(
                self.main_window,
                "🏷️ Sync Meta: no changes (## Meta already matches the active bundle)."
                + _format_chat_warnings(warnings),
            )
            return
        self.text_editor.setPlainText(new_text)
        self.status_message.emit("Meta synced from bundle")
        _post_to_chat(
            self.main_window,
            "🏷️ Sync Meta applied — ## Meta regenerated from the active bundle "
            "(pack versions re-pinned; board / format_version preserved)."
            + _format_chat_warnings(warnings),
        )

    def _on_renumber_steps(self) -> None:
        """Rewrite the leading ``N.`` on every step in ``## Steps`` to be
        sequential 1..N. Routed through the bundle's parser so the
        numbering convention stays bundle-defined. Confirmation in chat."""
        from ..llm import pack_parsers
        from .text_only_tab import _post_to_chat
        original = self.text_editor.toPlainText()
        if not original:
            return
        try:
            project_root = getattr(self.project_manager, "project_root", None)
            renumbered = pack_parsers.renumber_steps_text(
                original, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as exc:
            self.show_error("Renumber failed", str(exc))
            return
        if renumbered == original:
            self.status_message.emit("Steps already sequential")
            _post_to_chat(
                self.main_window,
                "🔢 Renumber Steps: already sequential — no changes.",
            )
            return
        self.text_editor.setPlainText(renumbered)
        self.status_message.emit("Steps renumbered")
        _post_to_chat(
            self.main_window,
            "🔢 Renumber Steps applied — every step in ## Steps is now "
            "sequential 1..N.",
        )
    
    def _on_save_json(self):
        """Save JSON artifact."""
        try:
            content = self.json_editor.toPlainText()
            self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, content)
            self.artifact_manager.save_artifact(ArtifactType.PROCEDURE_JSON)
            self.artifact_manager.procedure_json.mark_clean()
            self._json_dirty = False
            self._update_json_status()
            self.status_message.emit("JSON saved successfully")
            self.artifact_saved.emit()
        except Exception as e:
            self.show_error("Save Failed", str(e))
    
    def _on_save_both(self):
        """Save both artifacts."""
        self._on_save_text()
        self._on_save_json()
    
    def sync_editors_to_artifacts(self):
        """Sync editor content to ArtifactManager without saving to disk."""
        if not self.artifact_manager:
            return
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText()
        )
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText()
        )
    
    def save_all_artifacts(self):
        """Save both text and JSON artifacts (sync + save + reset dirty)."""
        self._on_save_both()
    
    def has_unsaved_changes(self) -> bool:
        """Check if either editor has been modified since last save."""
        return self._text_dirty or self._json_dirty
    
    def _on_format_json(self):
        """Format JSON with proper indentation."""
        try:
            content = self.json_editor.toPlainText()
            parsed = json.loads(content)
            formatted = json.dumps(parsed, indent=2)
            self.json_editor.setPlainText(formatted)
            self.status_message.emit("JSON formatted successfully")
        except json.JSONDecodeError as e:
            self.show_error("Format Failed", f"Invalid JSON: {e}")
        except Exception as e:
            self.show_error("Format Failed", str(e))
    
    def _get_artifact_for_validation(self, name: str) -> str | None:
        """Override the base implementation to read live editor content
        (the on-disk artifact may lag behind unsaved edits)."""
        if name == "procedure_text":
            return self.text_editor.toPlainText() or None
        if name == "procedure_json":
            return self.json_editor.toPlainText() or None
        return super()._get_artifact_for_validation(name)


    def refresh_parser_button(self):
        """Gate the deterministic ⚡ buttons in two tiers:

        - install-tier (visibility): hidden unless the rules_packager_base
          wheel imports cleanly — then the LLM workflow is the only path.
        - capability-tier (enable + tooltip): when visible, each button is
          enabled only if every equipment type the current procedure uses has
          a pack providing that button's capability (parse / emit / codegen).
          Otherwise it is disabled with a tooltip naming the missing pack, so
          an unsupported-equipment procedure cannot dump a cryptic
          EQP_TYPE_UNKNOWN / ValueError — the operator is told to use the LLM.
        """
        from ..llm import pack_parsers
        project_root = getattr(self.project_manager, "project_root", None)
        available, _ = pack_parsers.is_available(project_root)
        self.quick_parse_btn.setVisible(available)
        self.quick_render_btn.setVisible(available)
        self.parse_and_generate_btn.setVisible(available)
        # Sync Meta gates on the function itself, not just wheel-imports: an old
        # wheel imports (available=True) yet lacks sync_meta_text.
        self.sync_meta_btn.setVisible(pack_parsers.supports_sync_meta(project_root))
        if available:
            self._apply_capability_gating(project_root)

    def _apply_capability_gating(self, project_root):
        """Enable/disable the ⚡ buttons per the current procedure's equipment
        capabilities. Best-effort: any failure leaves buttons enabled (gating
        is UX, never a correctness gate)."""
        from ..llm import pack_parsers
        try:
            text = self.text_editor.toPlainText()
        except Exception:
            return

        def gate(btn, default_label, *caps):
            try:
                missing = []
                for cap in caps:
                    ok, miss = pack_parsers.can(cap, text, project_root)
                    if not ok:
                        missing.extend(miss)
                if missing:
                    names = ", ".join(f"{e} ({p})" for e, p in dict(missing).items())
                    btn.setEnabled(False)
                    btn.setToolTip(
                        f"No deterministic support for {names}. Use the LLM workflow."
                    )
                else:
                    btn.setEnabled(True)
                    btn.setToolTip(default_label)
            except Exception:
                btn.setEnabled(True)  # never break the editor over gating

        gate(self.quick_parse_btn, "Deterministic Text → JSON", "parse")
        gate(self.quick_render_btn, "Deterministic JSON → Text", "parse", "emit")
        gate(self.parse_and_generate_btn, "Parse + generate code", "parse", "codegen")

    def _on_quick_parse(self):
        """Deterministic Text → JSON without LLM (rule-based, instant)."""
        from ..llm import pack_parsers
        text = self.text_editor.toPlainText().strip()
        if not text:
            self.show_warning("No Content", "Procedure text is empty. Add text before parsing.")
            return

        # pack_parsers.parse_text raises ParseFailure on grammar errors
        # (with .code, .fix_hint, .findings — same shape the legacy
        # ParseError carried) or ParserUnavailable if the wheel isn't
        # importable. Route both through the structured dialog.
        project_root = getattr(self.project_manager, "project_root", None)
        try:
            result, warnings = pack_parsers.parse_text(text, project_root=project_root)
        except pack_parsers.ParserUnavailable as e:
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

        # Populate media[] on each step from the project's
        # config/text_parser.py extractor. The v2 deterministic text
        # parser produces v2 op shapes without media; this post-process
        # adds them so the procedure GUI can render PCB images.
        # Always re-extracts (media is derived from current step text).
        from ..llm.media_extraction import populate_media_on_steps
        populate_media_on_steps(result, self.project_manager.project_root)

        result_str = json.dumps(result, indent=2)

        current_json = self.json_editor.toPlainText().strip()
        if current_json:
            # Non-empty JSON — show diff so user can review
            accepted, final_content = DiffViewer.show_diff(
                current_json,
                result_str,
                "Review Changes: procedure.json (Quick Parse)",
                self,
            )
            if not accepted:
                self.main_window.dock.chat_panel.add_system_message(
                    "✗ Quick Parse — changes rejected."
                )
                return
            result_str = final_content
        else:
            # Empty JSON — apply directly
            pass

        self.json_editor.setPlainText(result_str)
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, result_str)
        self._json_dirty = True
        self._update_json_status()

        if warnings:
            warn_lines = "\n".join(f"  • {w}" for w in warnings)
            self.main_window.dock.chat_panel.add_system_message(
                f"⚡ Quick Parse complete — {len(warnings)} warning(s):\n{warn_lines}"
            )
        else:
            self.main_window.dock.chat_panel.add_system_message(
                "⚡ Quick Parse complete — no warnings."
            )
        self.status_message.emit("⚡ Quick Parse complete")

    def _on_quick_render(self):
        """Deterministic JSON → Text without LLM (rule-based, instant)."""
        from ..llm import pack_parsers
        json_text = self.json_editor.toPlainText().strip()
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
        try:
            result_text = pack_parsers.render_text(
                procedure, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as e:
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

        current_text = self.text_editor.toPlainText().strip()
        if current_text:
            accepted, final_content = DiffViewer.show_diff(
                current_text,
                result_text,
                "Review Changes: procedure_text.md (Quick Render)",
                self,
            )
            if not accepted:
                self.main_window.dock.chat_panel.add_system_message(
                    "✗ Quick Render — changes rejected."
                )
                return
            result_text = final_content

        self.text_editor.setPlainText(result_text)
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, result_text,
        )
        self._text_dirty = True
        self._update_text_status()

        self.main_window.dock.chat_panel.add_system_message(
            "⚡ Quick Render complete."
        )
        self.status_message.emit("⚡ Quick Render complete")

    def _on_parse_and_generate(self):
        """Strict one-click: Text → JSON → test.py with diff review.
        Delegates to the shared LLMTabMixin helper."""
        self._run_deterministic_parse_and_generate()

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

    def _on_review_text(self):
        """Review text with LLM."""
        if not self.text_editor.toPlainText():
            self.show_warning("No Text", "Text editor is empty. Write text first.")
            return
        self._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)

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
    
    def _get_task_description(self, task: LLMTask, user_message: str = None, custom_task_id: str = None) -> str:
        """Generate user-facing task description.
        
        Args:
            task: The LLM task being executed
            user_message: Optional user-provided message (for AD_HOC_CHAT)
            custom_task_id: Optional custom task ID for looking up task name
            
        Returns:
            Human-readable description of the task
        """
        # For custom tasks, look up the task name from config
        if custom_task_id:
            manager = self.task_config_manager
            if manager:
                task_config = manager.get_task_config(self.tab_id, custom_task_id)
                if task_config:
                    return f"Run: {task_config.name}"
        
        # For ad-hoc chat, use the user's actual message
        if task == LLMTask.AD_HOC_CHAT and user_message:
            return user_message
        
        task_descriptions = {
            LLMTask.DERIVE_JSON_FROM_TEXT: "Generate structured JSON from procedure text",
            LLMTask.RENDER_TEXT_FROM_JSON: "Generate human-readable text from JSON",
            LLMTask.REVIEW_TEXT_PROCEDURE: "Review procedure text for quality",
            LLMTask.REVIEW_JSON: "Review JSON structure and content",
            LLMTask.REVIEW_TEXT_VS_JSON: "Check coherence between text and JSON",
            LLMTask.AD_HOC_CHAT: "General assistance"
        }
        return task_descriptions.get(task, f"Run {task.name}")

    def _sync_editors_for_llm(self):
        """Sync editor widgets to artifact manager before LLM task."""
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText())
        self.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, self.json_editor.toPlainText())

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

    def _handle_text_proposal(self, proposal, task=None):
        """Handle procedure_text proposal."""
        # Accept "create" alongside "replace" — semantically identical
        # (full content for the artifact). The LLM emits "create" for
        # fresh authoring (empty editor) and "replace" for overwriting
        # existing content; both paths land the same way in the diff
        # dialog. "patch" is intentionally NOT included — partial-edit
        # semantics need a different handler.
        if proposal.mode in ("replace", "create"):
            # Serialize dict to string if needed
            if isinstance(proposal.content, dict):
                content_str = json.dumps(proposal.content, indent=2)
            else:
                content_str = str(proposal.content)

            # Show diff dialog for user to accept/reject
            current_content = self.text_editor.toPlainText()
            # Reconstruct operator-owned sections (test id, description, Meta)
            # from the prior before showing the diff — the LLM authors only the
            # body sections; identity is parser-owned.
            project_root = getattr(self.project_manager, "project_root", None)
            override = self._task_section_override(task)
            reconstructed, err = reconstructed_or_error(
                content_str, current_content,
                task_override=override, project_root=project_root,
            )
            if err:
                self.main_window.dock.chat_panel.add_system_message(
                    f"⚠ Could not reconstruct procedure_text.md: {err}"
                )
                return
            content_str = reconstructed
            accepted, final_content = DiffViewer.show_diff(
                current_content,
                content_str,
                "Review Changes: procedure_text.md",
                self
            )
            
            if accepted:
                self.text_editor.setPlainText(final_content)
                self.artifact_manager.procedure_text.content = final_content
                self._text_dirty = True
                self._update_text_status()
                self.main_window.dock.chat_panel.add_system_message("✓ Applied changes to procedure_text.md")
            else:
                self.main_window.dock.chat_panel.add_system_message("✗ Rejected changes to procedure_text.md")
    
    def _handle_json_proposal(self, proposal):
        """Handle procedure_json proposal."""
        # Accept "create" alongside "replace" — semantically identical
        # (full content for the artifact). The LLM emits "create" for
        # fresh authoring (empty editor) and "replace" for overwriting
        # existing content; both paths land the same way in the diff
        # dialog. "patch" is intentionally NOT included — partial-edit
        # semantics need a different handler.
        if proposal.mode in ("replace", "create"):
            # Serialize dict to JSON string if needed
            if isinstance(proposal.content, dict):
                proposed_dict = dict(proposal.content)
            else:
                try:
                    proposed_dict = json.loads(str(proposal.content))
                except json.JSONDecodeError:
                    proposed_dict = None

            # Populate `media` arrays on each step from the project's
            # config/text_parser.py extractor (per the v1 media-restoration
            # amendment 2026-04-28). The LLM doesn't emit media; the
            # workflow editor injects it post-application so the procedure
            # GUI can render PCB images via the ODB CLI.
            if proposed_dict is not None:
                from ..llm.media_extraction import populate_media_on_steps
                project_root = self.project_manager.project_root
                populate_media_on_steps(proposed_dict, project_root)
                content_str = json.dumps(proposed_dict, indent=2, ensure_ascii=False)
            else:
                content_str = str(proposal.content)

            # Show diff dialog for user to accept/reject
            current_content = self.json_editor.toPlainText()
            accepted, final_content = DiffViewer.show_diff(
                current_content,
                content_str,
                "Review Changes: procedure.json",
                self
            )
            
            if accepted:
                self.json_editor.setPlainText(final_content)
                self.artifact_manager.procedure_json.content = final_content
                self._json_dirty = True
                self._update_json_status()
                self.main_window.dock.chat_panel.add_system_message("✓ Applied changes to procedure.json")
            else:
                self.main_window.dock.chat_panel.add_system_message("✗ Rejected changes to procedure.json")
    
    def _update_text_status(self):
        """Update text status label."""
        if self._text_dirty:
            self.text_status.setText("● Modified")
            self.text_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.text_status.setText("✓ Saved")
            self.text_status.setStyleSheet(f"color: {status_saved()};")
    
    def _update_json_status(self):
        """Update JSON status label."""
        if self._json_dirty:
            self.json_status.setText("● Modified")
            self.json_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.json_status.setText("✓ Saved")
            self.json_status.setStyleSheet(f"color: {status_saved()};")
    
    def load_content(self):
        """Load both artifacts into editors."""
        if not self.artifact_manager:
            return
        
        # Load text
        text_content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT)
        self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(text_content)
        self.text_editor.blockSignals(False)
        self._text_dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_TEXT)
        self._update_text_status()
        
        # Load JSON
        json_content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_JSON)
        self.json_editor.blockSignals(True)
        self.json_editor.setPlainText(json_content)
        self.json_editor.blockSignals(False)
        self._json_dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_JSON)
        self._update_json_status()
    
    def on_activated(self):
        """Called when tab becomes active."""
        self.load_content()
    
    def refresh(self):
        """Refresh both artifacts."""
        self.load_content()
    
