"""The chat panel's "Board data" checkbox: lets the Text tab's conversation
read the board (see ``llm.board_chat``)."""

from PySide6.QtWidgets import QCheckBox

from procedo.core import odb_archive


_TIP = (
    "Let the chat load the complete netlist and component identities when needed.\n"
    "Enabling this does not load board data. Disabling stops new reads; "
    "previously read data stays in the conversation."
)


class BoardDataCheckBox(QCheckBox):
    def __init__(self, parent=None):
        super().__init__("Board data", parent)
        self._context = None
        self.toggled.connect(self._store)
        self.set_context(None)

    def _store(self, enabled):
        if self._context is not None:
            self._context.board_data_enabled = enabled
            self._context.board_data_project = self._context.project_manager.project_root

    def set_context(self, context):
        self._context = context
        is_text = context is not None and context.tab_id == "text_only"
        root = context.project_manager.project_root if is_text else None
        available = root is not None and odb_archive.find(root) is not None
        selected = bool(
            available and getattr(context, "board_data_enabled", False)
            and getattr(context, "board_data_project", None) == root
        )
        self.blockSignals(True)
        self.setVisible(is_text)
        self.setEnabled(available)
        self.setChecked(selected)
        self.blockSignals(False)
        if is_text and not selected:
            context.board_data_enabled = False
        self.setToolTip(_TIP if available else "No board available in this project.\n\n" + _TIP)
