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
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..llm.validator_dispatch import ValidationOutcome, ValidationIssueView
from ..theme import ERROR_COLOR, WARNING_COLOR


_ICON_BY_SEVERITY = {
    "error": "⛔",
    "warning": "⚠️",
}

_COLOR_BY_SEVERITY = {
    "error": ERROR_COLOR,
    "warning": WARNING_COLOR,
}


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
        self.setMinimumWidth(620)
        self.setMinimumHeight(380)
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
        layout = QVBoxLayout(self)

        if intro:
            intro_label = QLabel(intro)
            intro_label.setWordWrap(True)
            layout.addWidget(intro_label)

        summary = QLabel(self._summary_text(outcome))
        summary.setStyleSheet("font-weight: bold;")
        layout.addWidget(summary)

        # Issue list goes into a scroll area so 20+ issues don't blow out
        # the dialog height. Each row is a self-contained panel — adding
        # a new severity class is one entry in the colour map at module
        # scope.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        list_host = QWidget()
        list_layout = QVBoxLayout(list_host)
        list_layout.setContentsMargins(0, 0, 0, 0)
        for issue in outcome.issues:
            list_layout.addWidget(self._issue_panel(issue))
        list_layout.addStretch()
        scroll.setWidget(list_host)
        layout.addWidget(scroll, stretch=1)

        # Single Close button — the dialog is informational.
        button_row = QDialogButtonBox(QDialogButtonBox.Close)
        button_row.rejected.connect(self.reject)
        button_row.accepted.connect(self.accept)
        layout.addWidget(button_row)

    @staticmethod
    def _summary_text(outcome: ValidationOutcome) -> str:
        n_err = sum(1 for i in outcome.issues if i.severity == "error")
        n_warn = sum(1 for i in outcome.issues if i.severity == "warning")
        if n_err and n_warn:
            return f"{n_err} error(s), {n_warn} warning(s)"
        if n_err:
            return f"{n_err} error(s)"
        if n_warn:
            return f"{n_warn} warning(s)"
        return "Validator findings"

    @staticmethod
    def _issue_panel(issue: ValidationIssueView) -> QWidget:
        """Render a single issue as a card-like row.

        Layout:
            [icon] [code]                        location
                   message...
                   fix hint: ...
        """
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(8, 6, 8, 6)
        host_layout.setSpacing(2)

        # Header line: severity icon + code + location
        header = QHBoxLayout()
        header.setSpacing(8)

        icon = QLabel(_ICON_BY_SEVERITY.get(issue.severity, "•"))
        header.addWidget(icon)

        code_label = QLabel(issue.code or "(no code)")
        code_font = QFont()
        code_font.setBold(True)
        code_font.setFamily("monospace")
        code_label.setFont(code_font)
        colour = _COLOR_BY_SEVERITY.get(issue.severity, "#888")
        code_label.setStyleSheet(f"color: {colour};")
        header.addWidget(code_label)

        header.addStretch()

        if issue.location:
            loc_label = QLabel(issue.location)
            loc_label.setStyleSheet("color: #888;")
            loc_font = QFont()
            loc_font.setFamily("monospace")
            loc_label.setFont(loc_font)
            header.addWidget(loc_label)

        host_layout.addLayout(header)

        # Message body
        msg = QLabel(issue.message)
        msg.setWordWrap(True)
        host_layout.addWidget(msg)

        # Fix hint, if any
        if issue.fix_hint:
            fix = QLabel(f"<i>fix hint:</i> {issue.fix_hint}")
            fix.setWordWrap(True)
            fix.setStyleSheet("color: #4a8;")
            host_layout.addWidget(fix)

        host.setStyleSheet(
            "QWidget {"
            f"  border-left: 3px solid {colour};"
            "  background: rgba(128, 128, 128, 0.05);"
            "  border-radius: 3px;"
            "}"
        )
        return host
