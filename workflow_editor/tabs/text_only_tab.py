"""
Text-only Tab - Single-editor view for procedure_text.md.

A trimmed-down sibling of :class:`TextJsonTab` that only edits the text
artifact and only allows the LLM to propose updates to ``procedure_text``.
Loads its own subset of rules from ``config/tab_contexts.json`` under the
``text_only`` key, keeping the LLM context (and token cost) smaller than
the paired Text-JSON tab.
"""

import logging
import json

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPlainTextEdit
)
from PySide6.QtCore import Signal
from PySide6.QtGui import QFont

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from ..core import ArtifactType
from ..llm import TabContext, LLMTask
from ..llm.reconstruction import reconstructed_or_error
from ..dialogs import DiffViewer
from ..theme import status_modified, status_saved
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts

log = logging.getLogger(__name__)


def _post_to_chat(main_window, content: str) -> None:
    """Post a system-styled message to the chat panel. Silent no-op
    when the dock or chat panel isn't available (e.g. headless tests)."""
    dock = getattr(main_window, "dock", None)
    chat = getattr(dock, "chat_panel", None) if dock else None
    if chat is None or not hasattr(chat, "add_system_message"):
        return
    try:
        chat.add_system_message(content)
    except Exception:
        log.exception("chat notification failed; ignoring")


def _format_chat_warnings(warnings: list[str]) -> str:
    """Append a warning block to a chat message, or empty string if none."""
    if not warnings:
        return ""
    return "\n\n⚠ Warnings:\n" + "\n".join(f"• {w}" for w in warnings)


class TextOnlyTab(LLMTabMixin, BaseTab):
    """
    Text-only tab — single editor on procedure_text.md.

    The artifact is shared with :class:`TextJsonTab`; both tabs read and
    write the same ``procedure_text.md`` via the ArtifactManager. The
    smaller LLM context comes from the rule selection under the
    ``text_only`` key in ``config/tab_contexts.json`` and from the
    output contract that forbids JSON / code proposals on this tab.
    """

    content_changed = Signal()

    tab_id = "text_only"

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)

        self.tab_context = TabContext(
            tab_id="text_only",
            backend_factory=main_window.backend_factory,
            project_manager=main_window.project_manager,
            artifact_manager=main_window.artifact_manager,
            task_config_manager=main_window.task_config_manager,
            session_state=main_window.session_state
        )

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        text_group = QGroupBox("Procedure Text")
        text_layout = QVBoxLayout(text_group)

        self.text_editor = QPlainTextEdit()
        self.text_editor.setFont(QFont("Consolas", 10))
        self.text_editor.setPlaceholderText(
            "Write your test procedure here in natural language...\n\n"
            "This tab loads a smaller rule context (text DSL + grammars only) "
            "to keep token usage low."
        )
        self.text_editor.textChanged.connect(self._on_text_changed)
        text_layout.addWidget(self.text_editor)

        self.text_status = QLabel("")
        text_layout.addWidget(self.text_status)

        layout.addWidget(text_group, stretch=1)

        self.find_bar = FindReplaceBar(self)
        layout.addWidget(self.find_bar)
        install_find_shortcuts(self, [self.text_editor], self.find_bar)

        actions_layout = self._create_actions()
        layout.addLayout(actions_layout)

        self._text_dirty = False

    def _get_task_callback_map(self) -> dict:
        return {
            LLMTask.REVIEW_TEXT_PROCEDURE.value: (
                self._on_review_text,
                "Review procedure text for quality and completeness",
            ),
        }

    def _get_force_callback_map(self) -> dict:
        return {}

    def _create_actions(self):
        layout = QHBoxLayout()

        file_group = self.create_action_group("File Operations", "file")
        file_layout = QVBoxLayout(file_group)

        save_row = QHBoxLayout()
        self.save_text_btn = self.create_button(
            "Save Text", self._on_save_text, tooltip="Save procedure text to disk"
        )
        save_row.addWidget(self.save_text_btn)
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
                "customizations (controller subtype, extra headroom on "
                "max_current, etc.) are overwritten. Edit manually after "
                "Sync if you want bench headroom."
            ),
        )
        save_row.addWidget(self.sync_equipment_btn)
        self.sync_meta_btn = self.create_button(
            "Sync Meta", self._on_sync_meta,
            tooltip=(
                "Regenerate ## Meta (format_version / board / rules_pack / "
                "labscpi_pack / fncore_pack) from the active bundle — no LLM. "
                "board and format_version are preserved; pack versions are "
                "re-pinned to the bundle."
            ),
        )
        save_row.addWidget(self.sync_meta_btn)
        self.parse_and_generate_btn = self.create_button(
            "⚡ Parse + Generate", self._on_parse_and_generate,
            tooltip="One click: Text → JSON → test.py (strict; aborts on any warning)",
        )
        self.parse_and_generate_btn.setVisible(False)
        save_row.addWidget(self.parse_and_generate_btn)
        save_row.addStretch()
        file_layout.addLayout(save_row)

        validate_row = QHBoxLayout()
        self._build_validator_buttons(validate_row)
        validate_row.addStretch()
        file_layout.addLayout(validate_row)

        layout.addWidget(file_group)

        layout.addWidget(self._create_llm_action_group())

        return layout

    def _on_text_changed(self):
        # Mirror live editor content into the artifact_manager so callers
        # that read `procedure_text.content` (Review/Validate empty-checks,
        # token estimators, dock widgets) see what the user just typed,
        # not the last-saved snapshot. Without this, hitting Review on
        # unsaved text yielded "No Text" because the check ran against
        # disk-loaded content.
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText()
        )
        self._text_dirty = True
        self._update_text_status()
        self.tab_context.mark_artifact_modified("procedure_text")
        self.content_changed.emit()
        # Re-gate ⚡ Parse + Generate: the equipment set (hence pack
        # capabilities needed) can change as the operator edits the text.
        btn = getattr(self, "parse_and_generate_btn", None)
        if btn is not None and btn.isVisible():
            project_root = getattr(self.project_manager, "project_root", None)
            self._apply_capability_gating(project_root)

    def _on_save_text(self):
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
        scratch. Operator customizations on the existing block are
        overwritten — Sync makes Equipment reflect the steps
        as-written. Confirmation + any warnings posted to the chat
        panel."""
        from ..llm import pack_parsers
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
        """Regenerate the ``## Meta`` block (format_version / board + the
        rules_pack / labscpi_pack / fncore_pack version pins) from the active
        bundle, deterministically (no LLM). board / format_version and extra
        keys are preserved; pack versions are re-pinned to the bundle."""
        from ..llm import pack_parsers
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
        """Rewrite the leading ``N.`` on every step in the ``## Steps``
        block to be sequential 1..N. Pure text transform routed
        through the bundle's parser so the convention stays
        bundle-defined. Skips setPlainText when nothing changed so
        the dirty flag doesn't flip needlessly. Confirmation in chat."""
        from ..llm import pack_parsers
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

    def sync_editors_to_artifacts(self):
        if not self.artifact_manager:
            return
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText()
        )

    def save_all_artifacts(self):
        self._on_save_text()

    def has_unsaved_changes(self) -> bool:
        return self._text_dirty

    def _get_artifact_for_validation(self, name: str) -> str | None:
        """Override the base implementation to read live editor content.

        The Text tab intentionally exposes only ``procedure_text`` —
        validators that need JSON/code receive ``None`` so they can
        ``skip`` cleanly (see ``ValidatorContext`` contract).
        """
        if name == "procedure_text":
            return self.text_editor.toPlainText() or None
        return None  # never expose JSON/code from this tab

    def _on_review_text(self):
        if not self.text_editor.toPlainText():
            self.show_warning("No Text", "Text editor is empty. Write text first.")
            return
        self._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)

    def refresh_parser_button(self):
        """Gate ⚡ Parse + Generate in two tiers (mirrors text_json_tab):
        visible iff the rules_packager_base wheel imports, and — when visible —
        enabled iff every equipment type in the current procedure has a pack
        providing parse + codegen; else disabled with a tooltip naming the
        missing pack so an unsupported-equipment procedure can't dump a cryptic
        EQP_TYPE_UNKNOWN / ValueError.
        """
        from ..llm import pack_parsers
        project_root = getattr(self.project_manager, "project_root", None)
        available, _ = pack_parsers.is_available(project_root)
        self.parse_and_generate_btn.setVisible(available)
        if available:
            self._apply_capability_gating(project_root)

    def _apply_capability_gating(self, project_root):
        """Enable ⚡ Parse + Generate only if every equipment type the procedure
        uses has a parse+codegen-capable pack. Best-effort: any failure leaves
        the button enabled (gating is UX, never a correctness gate)."""
        from ..llm import pack_parsers
        try:
            text = self.text_editor.toPlainText()
            missing = []
            for cap in ("parse", "codegen"):
                ok, miss = pack_parsers.can(cap, text, project_root)
                if not ok:
                    missing.extend(miss)
            if missing:
                names = ", ".join(f"{e} ({p})" for e, p in dict(missing).items())
                self.parse_and_generate_btn.setEnabled(False)
                self.parse_and_generate_btn.setToolTip(
                    f"No deterministic support for {names}. Use the LLM workflow."
                )
            else:
                self.parse_and_generate_btn.setEnabled(True)
                self.parse_and_generate_btn.setToolTip("Parse + generate code")
        except Exception:
            self.parse_and_generate_btn.setEnabled(True)

    def _on_parse_and_generate(self):
        """Strict one-click: Text → JSON → test.py with diff review.
        Delegates to the shared LLMTabMixin helper."""
        self._run_deterministic_parse_and_generate()

    def _get_task_description(
        self, task: LLMTask, user_message: str = None, custom_task_id: str = None
    ) -> str:
        if custom_task_id:
            manager = self.task_config_manager
            if manager:
                task_config = manager.get_task_config(self.tab_id, custom_task_id)
                if task_config:
                    return f"Run: {task_config.name}"

        if task == LLMTask.AD_HOC_CHAT and user_message:
            return user_message

        task_descriptions = {
            LLMTask.REVIEW_TEXT_PROCEDURE: "Review procedure text for quality",
            LLMTask.AD_HOC_CHAT: "General assistance",
        }
        return task_descriptions.get(task, f"Run {task.name}")

    def _sync_editors_for_llm(self):
        self.artifact_manager.set_content(
            ArtifactType.PROCEDURE_TEXT, self.text_editor.toPlainText()
        )

    def _apply_proposals(self, response, task=None):
        # ``task`` carries the per-task section-ownership override into the
        # text-proposal reconstruction (None → bundle default).
        if response.procedure_text and response.procedure_text.mode:
            self._handle_text_proposal(response.procedure_text, task)

    def _get_expected_artifact_fields(self) -> list[str]:
        return ["text_procedure"]

    def _parse_response_to_dict(self, response) -> dict:
        parsed = {
            "assistant_message": response.assistant_message,
            "open_questions": response.session_delta.get("open_questions", []),
        }
        parsed["propose_update"] = False
        if response.procedure_text and response.procedure_text.mode:
            parsed["propose_update"] = True
            parsed["text_procedure"] = response.procedure_text.content
        return parsed

    def _handle_text_proposal(self, proposal, task=None):
        # Accept "create" alongside "replace" — see text_json_tab for
        # rationale. "patch" stays excluded.
        if proposal.mode not in ("replace", "create"):
            return

        if isinstance(proposal.content, dict):
            content_str = json.dumps(proposal.content, indent=2)
        else:
            content_str = str(proposal.content)

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
            self,
        )

        if accepted:
            self.text_editor.setPlainText(final_content)
            self.artifact_manager.procedure_text.content = final_content
            self._text_dirty = True
            self._update_text_status()
            self.main_window.dock.chat_panel.add_system_message(
                "✓ Applied changes to procedure_text.md"
            )
        else:
            self.main_window.dock.chat_panel.add_system_message(
                "✗ Rejected changes to procedure_text.md"
            )

    def _update_text_status(self):
        if self._text_dirty:
            self.text_status.setText("● Modified")
            self.text_status.setStyleSheet(f"color: {status_modified()};")
        else:
            self.text_status.setText("✓ Saved")
            self.text_status.setStyleSheet(f"color: {status_saved()};")

    def load_content(self):
        if not self.artifact_manager:
            return

        text_content = self.artifact_manager.get_content(ArtifactType.PROCEDURE_TEXT)
        self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(text_content)
        self.text_editor.blockSignals(False)
        self._text_dirty = self.artifact_manager.is_dirty(ArtifactType.PROCEDURE_TEXT)
        self._update_text_status()

    def on_activated(self):
        self.load_content()

    def refresh(self):
        self.load_content()
