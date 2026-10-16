"""Validation issues drawn as cards (boards S5 Findings and S10 Validator errors).

An issue is the dock's dict (``ValidationIssueView.to_dock_dict``: severity, code, message,
location, suggested_fix). Shared by the Assistant column's Findings tab and the validator
dialog: the counts as pills, one card per issue, the issues as text for the clipboard.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from project_services import ui_kit

#: An issue's severity -> its pill tone.
_TONES = {"error": "danger", "warning": "warning"}


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 or word == 'info' else 's'}"


def issue_card(issue: dict, *, tag: str = "", fix_label: str = "Fix") -> QFrame:
    """One issue as a card (#findingCard): a pill in its severity's tone (*tag*, the severity
    word beside it; or the severity itself), its location pill on the right, the message, the
    fix (``fix_label: …``)."""
    card = QFrame()
    card.setObjectName("findingCard")
    column = QVBoxLayout(card)
    column.setContentsMargins(10, 8, 10, 8)
    column.setSpacing(4)
    severity = str(issue.get("severity", "info"))
    head = ui_kit.hbox(ui_kit.set_pill(QLabel(), _TONES.get(severity, "info"), tag or severity.upper()))
    if tag:  # a tone alone does not say error or warning
        head.addWidget(ui_kit.note(severity, wrap=False))
    head.addStretch(1)
    if issue.get("location"):
        head.addWidget(ui_kit.set_pill(QLabel(), "neutral", str(issue["location"])))
    column.addLayout(head)
    message = QLabel(str(issue.get("message", issue)))
    message.setWordWrap(True)
    column.addWidget(message)
    if issue.get("suggested_fix"):
        fix = QLabel(f"{fix_label}: {issue['suggested_fix']}")
        fix.setObjectName("findingFix")
        fix.setWordWrap(True)
        column.addWidget(fix)
    if issue.get("code") and not tag:
        card.setToolTip(str(issue["code"]))
    return card


def fill_counts(row: QHBoxLayout, issues: list) -> None:
    """Refill *row* with the counts' pills: errors, warnings, the rest as info ("No issues")."""
    ui_kit.clear_layout(row)
    severities = [i.get("severity", "info") if isinstance(i, dict) else "info" for i in issues]
    errors, warnings = severities.count("error"), severities.count("warning")
    pills = [("danger", _count(errors, "error")) if errors else None,
             ("warning", _count(warnings, "warning")) if warnings else None,
             ("info", _count(len(issues) - errors - warnings, "info"))
             if len(issues) > errors + warnings else None]
    if not issues:
        pills = [("success", "No issues")]
    for tone, text in filter(None, pills):
        row.addWidget(ui_kit.set_pill(QLabel(), tone, text))
    row.addStretch(1)


def issues_text(issues: list) -> str:
    """The issues as plain-text markdown bullets (what Copy puts on the clipboard)."""
    lines = []
    for issue in issues:
        if not isinstance(issue, dict):
            lines.append(f"- {issue}")
            continue
        severity = issue.get("severity", "info")
        code = issue.get("code", "")
        message = issue.get("message", "")
        location = issue.get("location", "")
        fix = issue.get("suggested_fix", "")

        head = f"- [{severity}]"
        if code:
            head += f" {code}:"
        if message:
            head += f" {message}"
        if location:
            head += f" ({location})"
        lines.append(head)
        if fix:
            lines.append(f"  Fix: {fix}")
    return "\n".join(lines)

