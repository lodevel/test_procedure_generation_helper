"""A view's action bar, as on the approved boards S1–S3: the tinted ✦ ASSISTANT row with the LLM
task buttons over the FILE row — the file actions, a divider, then the ⚡ pack actions and the
validator buttons. Both rows wrap when the view is narrow."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QMenu, QToolButton, QVBoxLayout, QWidget

from project_services import ui_kit
from project_services.flow_layout import FlowLayout


def task_button(label: str, callback: Callable, *, force_callback: Callable | None = None,
                force_tooltip: str = "") -> QToolButton:
    """An assistant (LLM task) button; with *force_callback* a split button whose ▾ menu offers
    the task and its force variant (validation bypassed)."""
    button = QToolButton()
    button.setProperty("ai", True)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    button.clicked.connect(callback)
    if force_callback is not None:
        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        menu.addAction("").triggered.connect(callback)
        force = menu.addAction("")
        force.setToolTip(force_tooltip)
        force.triggered.connect(force_callback)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    set_task_label(button, label)
    return button


def set_task_label(button: QToolButton, label: str) -> None:
    """Show *label* on an assistant button (and its menu's two variants)."""
    button.setText(f"✦ {label}")
    menu = button.menu()
    if menu is not None:
        strict, force = menu.actions()
        strict.setText(label)
        force.setText(f"{label} — force\tbypass checks")


class _FileRow(QWidget):
    """The FILE row: its divider shows only while a pack action or a validator follows it."""

    def __init__(self) -> None:
        super().__init__()
        self.layout_ = FlowLayout(self, spacing=8)
        self.divider = ui_kit.vertical_rule()
        self.divider.setFixedHeight(22)

    def event(self, event) -> bool:  # noqa: D401 (Qt)
        if event.type() == QEvent.Type.LayoutRequest:  # a button was shown, hidden, added
            items = [self.layout_.itemAt(i).widget() for i in range(self.layout_.count())]
            after = items[items.index(self.divider) + 1:] if self.divider in items else []
            self.divider.setVisible(any(not w.isHidden() for w in after if w is not None))
        return super().event(event)


class ActionBar(QFrame):
    """The two rows in a white card; ``file_layout`` takes the validator buttons after the pack
    actions, ``set_assistant_buttons`` refills the assistant row."""

    def __init__(self, file_buttons: Iterable[QWidget], pack_buttons: Iterable[QWidget]) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setProperty("actionBar", True)  # its small row labels (app QSS)
        column = QVBoxLayout(self)
        column.setContentsMargins(1, 1, 1, 1)  # inside the card's border
        column.setSpacing(0)
        assistant = QWidget()
        assistant.setObjectName("assistantRow")  # the tinted row (app QSS)
        assistant.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.assistant_layout = FlowLayout(assistant, spacing=6)
        self.assistant_layout.setContentsMargins(12, 8, 12, 8)
        files = _FileRow()
        self.file_layout = files.layout_
        self.file_layout.setContentsMargins(12, 8, 12, 8)
        self.file_layout.addWidget(ui_kit.subheading("File"))
        for button in file_buttons:
            self.file_layout.addWidget(button)
        self.file_layout.addWidget(files.divider)
        for button in pack_buttons:
            self.file_layout.addWidget(button)
        column.addWidget(assistant)
        column.addWidget(files)
        self.set_assistant_buttons([])

    def set_assistant_buttons(self, widgets: Iterable[QWidget]) -> None:
        ui_kit.clear_layout(self.assistant_layout)
        self.assistant_layout.addWidget(ui_kit.subheading("✦ Assistant"))
        for widget in widgets:
            self.assistant_layout.addWidget(widget)
