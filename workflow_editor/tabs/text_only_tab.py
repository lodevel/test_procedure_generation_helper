"""
Text-only Tab - Single-editor view for procedure_text.md (board S1).

A trimmed-down sibling of :class:`TextJsonTab` that only edits the text
artifact and only allows the LLM to propose updates to ``procedure_text``.
Loads its own subset of rules from ``config/tab_contexts.json`` under the
``text_only`` key, keeping the LLM context (and token cost) smaller than
the paired Text-JSON tab. Beside the editor card: the board-netlist card.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QVBoxLayout

from .base_tab import BaseTab
from .llm_tab_mixin import LLMTabMixin
from .text_actions import TextActionsMixin
from ..core import ArtifactType
from ..llm import TabContext, LLMTask
from ..widgets.editor_card import EditorCard
from ..widgets.find_replace_bar import FindReplaceBar, install_find_shortcuts
from ..widgets.netlist_panel import NetlistPanel
from ..widgets.text_highlighter import ProcedureTextHighlighter

log = logging.getLogger(__name__)

#: The netlist card's width on board S1.
_NETLIST_WIDTH = 330


class TextOnlyTab(TextActionsMixin, LLMTabMixin, BaseTab):
    """
    Text-only tab — single editor on procedure_text.md.

    The artifact is shared with :class:`TextJsonTab`; both tabs read and
    write the same ``procedure_text.md`` via the ArtifactManager. The
    smaller LLM context comes from the rule selection under the
    ``text_only`` key in ``config/tab_contexts.json`` and from the
    output contract that forbids JSON / code proposals on this tab.
    """

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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.text_card = self.add_card(EditorCard(
            self, ArtifactType.PROCEDURE_TEXT, "procedure_text.md",
            placeholder=(
                "Write your test procedure here in natural language...\n\n"
                "This tab loads a smaller rule context (text DSL + grammars only) "
                "to keep token usage low."
            ),
        ))
        self.text_editor = self.text_card.editor
        self.text_highlighter = ProcedureTextHighlighter(self.text_editor.document())
        self.parse_and_generate_btn = self._pack_button(
            "⚡ Parse + Generate", self._on_parse_and_generate,
            "One click: Text → JSON → test.py (strict; aborts on any warning)",
        )
        self.text_card.changed.connect(self._on_text_edited)

        self.find_bar = FindReplaceBar(self)
        layout.addWidget(self.find_bar)
        install_find_shortcuts(self, [self.text_editor], self.find_bar)

        # Editor on the left, a read-only board-netlist reference on the right
        # (browse components/pins/nets and view their board image).
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(10)
        split.addWidget(self.text_card)
        self._netlist_panel = NetlistPanel()
        split.addWidget(self._netlist_panel)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([600, _NETLIST_WIDTH])
        layout.addWidget(split, stretch=1)
        self._netlist_loaded_root = object()   # sentinel — first show triggers a load

        layout.addWidget(self._create_action_bar(
            self._text_file_buttons(), [self.parse_and_generate_btn]))

    def showEvent(self, event):  # noqa: N802 — Qt override
        super().showEvent(event)
        self._maybe_load_netlist()

    def _maybe_load_netlist(self) -> None:
        """Load the board netlist for the current project (once per project)."""
        pm = getattr(self.main_window, "project_manager", None)
        root = getattr(pm, "project_root", None) if pm is not None else None
        if root != self._netlist_loaded_root:
            self._netlist_loaded_root = root
            self._netlist_panel.load(root)

    def reload_netlist(self) -> None:
        """Force a netlist reload (e.g. after the project changes)."""
        self._netlist_loaded_root = object()
        self._maybe_load_netlist()

    def _get_task_callback_map(self) -> dict:
        return {
            LLMTask.REVIEW_TEXT_PROCEDURE.value: (
                self._on_review_text,
                "Review procedure text for quality and completeness",
            ),
        }

    def _pack_gates(self):
        return [(self.parse_and_generate_btn, "Parse + generate code", ("parse", "codegen"))]

    def _get_artifact_for_validation(self, name: str) -> str | None:
        """The Text tab intentionally exposes only ``procedure_text`` —
        validators that need JSON/code receive ``None`` so they can
        ``skip`` cleanly (see ``ValidatorContext`` contract).
        """
        if name == "procedure_text":
            return super()._get_artifact_for_validation(name)
        return None  # never expose JSON/code from this tab

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

    def on_activated(self):
        self.load_content()

    def refresh(self):
        self.load_content()
