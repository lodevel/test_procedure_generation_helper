"""
Base Tab Widget - Common functionality for all editor tabs.
"""

import json
from collections.abc import Iterable
from PySide6.QtWidgets import QWidget, QPushButton, QMessageBox
from PySide6.QtCore import Signal
from typing import TYPE_CHECKING, Optional, Callable

from ..core.proposal import FileChange, Proposal, current_test
from ..llm.backend_base import LLMTask
from ..widgets.action_bar import ActionBar
from ..widgets.editor_card import EditorCard
from .task_buttons import TaskButtonRow
from .validator_buttons import ValidatorButtonRow

if TYPE_CHECKING:
    from ..main_window import MainWindow


class BaseTab(QWidget):
    """
    Base class for all editor tabs.

    Provides common functionality:
    - Access to main window and managers
    - Button creation helpers
    - Status updates
    - Error handling

    A view with an action bar holds its LLM task buttons (``task_buttons``, a
    ``TaskButtonRow``) and its validator buttons (``validator_buttons``, a
    ``ValidatorButtonRow``); a view without one has None for both.
    """

    # Signals
    status_message = Signal(str)  # Emit status bar messages
    content_changed = Signal()  # Emit when content is modified
    artifact_saved = Signal()  # Emit after any artifact is written to disk

    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self.main_window = main_window
        #: The view's editor cards (``add_card``): loaded, synced and saved together.
        self.cards: list[EditorCard] = []
        self.task_buttons: Optional[TaskButtonRow] = None
        self.validator_buttons: Optional[ValidatorButtonRow] = None
        self._setup_ui()

    def _setup_ui(self):
        """Override in subclasses to setup UI."""
        pass

    @property
    def artifact_manager(self):
        """Get the artifact manager."""
        return self.main_window.artifact_manager

    @property
    def session_state(self):
        """Get the session state."""
        return self.main_window.session_state

    @property
    def project_manager(self):
        """Get the project manager."""
        return self.main_window.project_manager

    @property
    def task_config_manager(self):
        """Get the task configuration manager."""
        from ..core.task_config import TaskConfigManager

        manager = getattr(self.main_window, "task_config_manager", None)
        return manager if isinstance(manager, TaskConfigManager) else None

    def create_button(
        self, text: str, callback=None, enabled: bool = True, tooltip: str = ""
    ) -> QPushButton:
        """Create a button with optional callback."""
        button = QPushButton(text)
        button.setEnabled(enabled)

        if tooltip:
            button.setToolTip(tooltip)

        if callback:
            button.clicked.connect(callback)

        return button

    def show_error(self, title: str, message: str):
        """Show an error dialog."""
        QMessageBox.critical(self, title, message)

    def show_warning(self, title: str, message: str):
        """Show a warning dialog."""
        QMessageBox.warning(self, title, message)

    def show_info(self, title: str, message: str):
        """Show an info dialog."""
        QMessageBox.information(self, title, message)

    def ask_yes_no(self, title: str, message: str) -> bool:
        """Show a yes/no question dialog."""
        result = QMessageBox.question(
            self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        return result == QMessageBox.Yes

    def on_activated(self):
        """Called when this tab becomes active. Override as needed."""
        pass

    def on_deactivated(self):
        """Called when this tab is deactivated. Override as needed."""
        pass

    def refresh(self):
        """Refresh the tab content. Override as needed."""
        pass

    def add_card(self, card: EditorCard) -> EditorCard:
        """Register an editor card: its edits mark the artifact modified for the LLM context
        and repaint the header (``content_changed``)."""
        self.cards.append(card)
        card.changed.connect(lambda: self._on_card_changed(card))
        return card

    def _on_card_changed(self, card: EditorCard) -> None:
        tab_context = getattr(self, "tab_context", None)
        if tab_context is not None:
            tab_context.mark_artifact_modified(card.artifact_name)
        self.content_changed.emit()

    def card_for(self, artifact_name: str) -> Optional[EditorCard]:
        """The card editing *artifact_name* ("procedure_text", …) in this view, if any."""
        return next((c for c in self.cards if c.artifact_name == artifact_name), None)

    def sync_editors_to_artifacts(self):
        """Sync editor content to ArtifactManager without saving to disk.

        Called before dirty checks, tab switches, and saves to ensure
        ArtifactManager has the latest editor content.
        """
        if not self.artifact_manager:
            return
        for card in self.cards:
            card.sync()

    def load_content(self):
        """Show the artifacts' current content in the view's editors."""
        if not self.artifact_manager:
            return
        for card in self.cards:
            card.load()

    def _on_format_json(self):
        """Format JSON with proper indentation (the view's procedure.json editor)."""
        card = self.card_for("procedure_json")
        try:
            parsed = json.loads(card.editor.toPlainText())
            card.set_text(json.dumps(parsed, indent=2))
            self.status_message.emit("JSON formatted successfully")
        except json.JSONDecodeError as e:
            self.show_error("Format Failed", f"Invalid JSON: {e}")
        except Exception as e:
            self.show_error("Format Failed", str(e))

    def save_all_artifacts(self):
        """Save all artifacts managed by this tab (sync + save + reset dirty).

        This is the correct entry point for saving from outside the tab
        (e.g. Ctrl+S, Save All): each editor card writes its artifact. True when every card
        saved (a card that failed said why).
        """
        return all([card.save() for card in self.cards])  # a list: every card tries

    # ------------------------------------------------------------------
    # Shared by the LLM views: the chat's task line, a procedure.json proposal
    # ------------------------------------------------------------------

    #: The chat's line for a built-in task the configuration does not name.
    _TASK_DESCRIPTIONS = {
        LLMTask.DERIVE_JSON_FROM_TEXT: "Generate structured JSON from procedure text",
        LLMTask.RENDER_TEXT_FROM_JSON: "Generate human-readable text from JSON",
        LLMTask.REVIEW_TEXT_PROCEDURE: "Review procedure text for quality",
        LLMTask.REVIEW_JSON: "Review JSON structure and content",
        LLMTask.REVIEW_TEXT_VS_JSON: "Check coherence between text and JSON",
        LLMTask.GENERATE_CODE_FROM_JSON: "Generate executable test code from JSON",
        LLMTask.DERIVE_JSON_FROM_CODE: "Extract JSON structure from existing code",
        LLMTask.REVIEW_CODE: "Review test code quality and structure",
        LLMTask.REVIEW_CODE_VS_JSON: "Check coherence between JSON and code",
        LLMTask.AD_HOC_CHAT: "General assistance",
    }

    def _get_task_description(
        self, task: LLMTask, user_message: str = None, custom_task_id: str = None
    ) -> str:
        """The chat's line for a task: a custom task's configured name, the
        operator's own message for ad-hoc chat, else the configured task's name."""
        manager = self.task_config_manager
        if custom_task_id and manager:
            task_config = manager.get_task_config(self.tab_id, custom_task_id)
            if task_config:
                return f"Run: {task_config.name}"

        if task == LLMTask.AD_HOC_CHAT and user_message:
            return user_message

        # Built-in tasks are ALSO configurable (they live in the same tasks list),
        # so source the chat message from the configured task's name — editing the
        # button in the Template Manager then updates this message too, not just
        # custom tasks. Falls through to the static map only when unconfigured.
        if manager:
            tc = manager.get_task_config(self.tab_id, task.value)
            if tc and tc.name:
                return tc.name
        return self._TASK_DESCRIPTIONS.get(task, f"Run {task.name}")

    def propose_edit(
        self,
        card: EditorCard,
        proposed: str,
        *,
        done: Optional[Callable] = None,
        rejected: str = "",
        banner: str = "",
        ask_when_empty: bool = True,
        test: Optional[tuple] = None,
    ) -> None:
        """Offer *proposed* for *card*'s file as a proposal card in this view's chat. Accept
        puts it in the editor (an edit, as if typed) then runs *done* (by default the chat's
        "✓ Applied changes to <file>"); Reject posts *rejected* (by default "✗ Rejected changes
        to <file>"). Without *ask_when_empty* (a ⚡ result) an empty editor takes it at once.
        *test* is the test it is for: an AI reply's, the one open when it was asked; by default
        the open one (a ⚡ result, made now)."""
        chat = self.main_window.dock.chat_panel

        def apply() -> None:
            card.set_text(proposed)
            if done is not None:
                done()
            else:
                chat.add_system_message(f"✓ Applied changes to {card.file_name}", tone="success")

        proposal = Proposal(
            [FileChange(card.file_name, card.editor.toPlainText, proposed)],
            apply,
            lambda: chat.add_system_message(
                rejected or f"✗ Rejected changes to {card.file_name}", tone="danger"
            ),
            banner,
            test=test or current_test(self.artifact_manager),
        )
        if not ask_when_empty and not card.editor.toPlainText().strip():
            proposal.accept()
        else:
            chat.offer_proposal(self.tab_context, proposal)

    def _handle_json_proposal(self, proposal):
        """Offer a procedure.json proposal (``self.json_card``) as a proposal card.

        Accepts "create" alongside "replace" — semantically identical (full
        content for the artifact). "patch" is intentionally NOT included —
        partial-edit semantics need a different handler. Proposed JSON is
        re-dumped with indent=2 and ensure_ascii=False (µ, ±, Ω stay literal);
        content that does not parse is shown as it came.
        """
        if proposal.mode not in ("replace", "create"):
            return
        if isinstance(proposal.content, dict):
            proposed = dict(proposal.content)
        else:
            try:
                proposed = json.loads(str(proposal.content))
            except json.JSONDecodeError:
                proposed = None
        content_str = (
            json.dumps(proposed, indent=2, ensure_ascii=False)
            if proposed is not None
            else str(proposal.content)
        )
        self.propose_edit(self.json_card, content_str, test=self._ensure_run_state().test)

    def _get_task_callback_map(self) -> dict:
        """Return mapping of task_id -> (callback, tooltip).

        Overridden by the views to provide their task callbacks.
        Keys are LLMTask enum values (strings), values are (callable, str) tuples.
        """
        return {}

    def _get_force_callback_map(self) -> dict:
        """Return mapping of task_id -> (force_callback, tooltip) for force-mode buttons.

        Override in views that support force-mode for specific tasks.
        """
        return {}

    def _get_artifact_for_validation(self, name: str) -> Optional[str]:
        """Return the *current* (live editor) content for a validator: the
        view's editor for that artifact when it has one, else the artifact
        manager's copy.
        """
        card = self.card_for(name)
        if card is not None:
            return card.editor.toPlainText() or None
        am = getattr(self.main_window, "artifact_manager", None)
        if am is None:
            return None
        artifact = None
        if name == "procedure_text":
            artifact = getattr(am, "procedure_text", None)
        elif name == "procedure_json":
            artifact = getattr(am, "procedure_json", None)
        elif name == "test_code":
            artifact = getattr(am, "test_code", None)
        return artifact.content if artifact else None

    def _create_action_bar(
        self, file_buttons: Iterable[QWidget] = (), pack_buttons: Iterable[QWidget] = ()
    ) -> ActionBar:
        """The view's action bar (``self.action_bar``): the assistant row built
        from TaskConfigManager, the FILE row with *file_buttons*, a divider,
        *pack_buttons* (⚡) and the validator buttons."""
        self.action_bar = ActionBar(file_buttons, pack_buttons)
        tab_id = getattr(self, "tab_id", None)
        self.validator_buttons = ValidatorButtonRow(
            self.action_bar.file_layout,
            main_window=self.main_window,
            tab_id=tab_id,
            config=self.task_config_manager,
            make_button=self.create_button,
            artifact_for=self._get_artifact_for_validation,
            sync_editors=self.sync_editors_to_artifacts,
            status=self.status_message.emit,
        )
        self.validator_buttons.rebuild()
        self.task_buttons = TaskButtonRow(
            self.action_bar,
            tab_id=tab_id,
            owner_name=type(self).__name__,
            config=self.task_config_manager,
            callbacks=self._get_task_callback_map(),
            force_callbacks=self._get_force_callback_map(),
            run_task=getattr(self, "_run_task_async", None),
        )
        self.task_buttons.rebuild()
        return self.action_bar
