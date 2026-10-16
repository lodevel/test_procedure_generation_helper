"""Structured validator-error dialog.

One renderer for both surfaces that produce ``ValidationOutcome`` objects:

  - **Quick Parse / Quick Code** — the deterministic-only buttons
    (``text_json_tab._on_quick_parse``, ``json_code_tab._on_quick_code``)
    raise a ``ParseError`` or return an outcome with ``ok=False``. Pre-fix
    the GUI surfaced just ``str(e)`` in a generic dialog, throwing away
    the structured ``code`` / ``fix_hint``.

  - **LLM-with-feedback loop** — when the auto-retry FSM exhausts its
    attempt budget, the residual issues are shown here too. Same dialog,
    same structured payload, so operators learn one shape of error.

The dialog is intentionally *informational only*: no dispatch back to
LLM, no auto-fix button. Operators close it, then either fix the input
manually or hit the LLM-loop "retry once more" button (phase 2). The
single-renderer constraint keeps the rendering logic in exactly one
place; adding a new error surface is one ``ValidatorErrorDialog.show_for``
call.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QScrollArea, QVBoxLayout, QWidget

from procedo.core import ui_kit

from ..llm.validator_dispatch import ValidationOutcome
from ..widgets import issue_cards


class ValidatorErrorDialog(QDialog):
    """Modal listing the structured issues from a :class:`ValidationOutcome`."""

    def __init__(
        self,
        outcome: ValidationOutcome,
        *,
        title: str = "Validator findings",
        intro: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(520, 380)
        self.resize(620, 560)
        self._build_ui(outcome, intro)

    # --------------------------------------------------------------------- #
    # Convenience entry-point used by callers                               #
    # --------------------------------------------------------------------- #

    @classmethod
    def show_for(
        cls,
        outcome: ValidationOutcome,
        *,
        title: str = "Validator findings",
        intro: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        """Show the dialog modally. No-op on ``ok=True`` outcomes — callers
        can call this unconditionally and the dialog only appears on real
        rejections."""
        if outcome.ok and not outcome.issues:
            return
        cls(outcome, title=title, intro=intro, parent=parent).exec()

    @classmethod
    def show_from_exception(
        cls,
        exc: BaseException,
        *,
        title: str = "Validator findings",
        intro: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        """Show the dialog populated from an arbitrary exception (e.g. a
        ``ParseError`` from a Quick-Parse / Quick-Code button). Wraps
        :func:`outcome_from_exception` from validator_dispatch so the
        Quick-button error paths and the LLM-loop residual-failure path
        share one rendering call site (no duplicated five-line block at
        every catch)."""
        # Local import: keeps validator_dispatch out of the dialog's
        # import-time chain when no Quick button has fired yet.
        from ..llm.validator_dispatch import outcome_from_exception
        cls.show_for(
            outcome_from_exception(exc),
            title=title,
            intro=intro,
            parent=parent,
        )

    # --------------------------------------------------------------------- #
    # UI construction                                                       #
    # --------------------------------------------------------------------- #

    def _build_ui(self, outcome: ValidationOutcome, intro: str) -> None:
        """The counts as pills, one card per issue (its code in its severity's tone, its
        location, the message, the fix hint) — board S10; Copy all, Close."""
        issues = [issue.to_dock_dict() for issue in outcome.issues]
        copy_btn = QPushButton("Copy all")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(issue_cards.issues_text(issues)))
        close_btn = ui_kit.primary(QPushButton("Close"))
        close_btn.clicked.connect(self.accept)
        body = ui_kit.framed(self, ui_kit.HeaderBand(self.windowTitle(), intro, stacked=True),
                             ui_kit.FooterBand([copy_btn, close_btn]))

        self.counts = ui_kit.hbox()
        self.counts.setSpacing(6)
        issue_cards.fill_counts(self.counts, issues)
        body.addLayout(self.counts)

        # The cards scroll, so 20+ issues don't blow out the dialog height.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        cards = QWidget()
        column = QVBoxLayout(cards)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        for issue in issues:
            column.addWidget(issue_cards.issue_card(issue, tag=issue["code"], fix_label="Fix hint"))
        column.addStretch()
        scroll.setWidget(cards)
        body.addWidget(scroll, 1)
