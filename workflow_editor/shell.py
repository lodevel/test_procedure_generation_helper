"""Procedo Studio's frame, as on the approved board S1: the header band (the open test, its
Text / JSON / Code and Sync pills, the OpenCode pill and its menu, Export, Skill chat, Save all),
the segmented switch between the four views, the Workspace and Assistant columns, and the status
line (project, rules, bundle, validator).

Built from ``project_services.ui_kit``, so Studio's frame is Bench's. ``StudioWorkspace`` owns
the state and the actions; this module builds the widgets onto it and paints its state on them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from project_services import bundle_registry, ui_kit
from project_services.segmented import segment, segmented

from .authoring.skill_menu import launch_skill_chat
from .core import ArtifactType
from .core.procedure_steps import numbered_steps
from .llm import validator_loop_settings

if TYPE_CHECKING:
    from .main_window import StudioWorkspace

_ICONS = Path(__file__).parent / "icons"
#: The header's mark and the window's icon.
MARK_SVG = _ICONS / "procedo_steps_studio_light.svg"
WINDOW_ICON_SVG = _ICONS / "procedo_steps_studio_tile.svg"

#: The four views: the switch's label and the files the view works on.
VIEWS = (
    ("Text", "procedure_text.md"),
    ("Text–JSON", "procedure_text.md ↔ procedure.json"),
    ("JSON–Code", "procedure.json ↔ test.py"),
    ("Traceability", "procedure.json → test.py"),
)
#: The header's artifact pills: label, artifact, file.
_ARTIFACTS = (
    ("Text", ArtifactType.PROCEDURE_TEXT, "procedure_text.md"),
    ("JSON", ArtifactType.PROCEDURE_JSON, "procedure.json"),
    ("Code", ArtifactType.TEST_CODE, "test.py"),
)
#: The side columns' widths on the board (the views take the rest).
_WORKSPACE_WIDTH = 246
_ASSISTANT_WIDTH = 342


# ---- Building ------------------------------------------------------------------------------

def build_view_column(ws: StudioWorkspace, views: list[QWidget]) -> QWidget:
    """The segmented switch over the stacked views (``ws.view_stack``), the current view's
    files right of the switch; switching calls ``ws._on_tab_changed``."""
    ws.view_stack = QStackedWidget()
    ws.view_buttons = QButtonGroup(ws)
    ws.view_files = ui_kit.monospace(ui_kit.note(wrap=False))
    ws.view_stack.currentChanged.connect(lambda index: _show_view(ws, index))
    ws.view_stack.currentChanged.connect(ws._on_tab_changed)
    buttons = []
    for index, (label, _files) in enumerate(VIEWS):
        button = segment(label)
        ws.view_buttons.addButton(button, index)
        buttons.append(button)
    ws.view_buttons.idClicked.connect(ws.view_stack.setCurrentIndex)
    switch = segmented(*buttons, large=True)
    switch.layout().addWidget(ws.view_files)
    for view in views:  # the first one fires currentChanged(0): the switch and Save follow
        ws.view_stack.addWidget(view)
    ws.view_column = QWidget()
    column = QVBoxLayout(ws.view_column)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(10)
    column.addWidget(switch)
    column.addWidget(ws.view_stack, 1)
    return ws.view_column


def build_frame(ws: StudioWorkspace) -> None:
    """Lay the workspace out: the header, the three columns, the status line."""
    ws.setObjectName("mainCentral")  # the page colour behind the cards
    ws.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    ws.workspace_column = _card(ws.workspace_widget, _WORKSPACE_WIDTH)
    ws.assistant_column = _card(ws.dock, _ASSISTANT_WIDTH)
    columns = QSplitter(Qt.Orientation.Horizontal)
    columns.setChildrenCollapsible(False)
    for column in (ws.workspace_column, ws.view_column, ws.assistant_column):
        columns.addWidget(column)
    columns.setStretchFactor(1, 1)  # the side columns keep their width, the views take the rest
    body = QWidget()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(16, 12, 16, 12)
    body_layout.addWidget(columns)
    layout = QVBoxLayout(ws)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(_header(ws))
    layout.addWidget(body, 1)
    layout.addWidget(_footer(ws))
    for action, column in ((ws.toggle_workspace_action, ws.workspace_column),
                           (ws.toggle_dock_action, ws.assistant_column)):
        action.toggled.connect(column.setVisible)
    refresh_header(ws)
    refresh_footer(ws)
    ws._refresh_server_indicator()


def _card(content: QWidget, width: int) -> QFrame:
    """*content* in a white card, *width* wide at the least (a side column of the board)."""
    card = ui_kit.card_frame()
    card.setMinimumWidth(width)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(1, 1, 1, 1)  # inside the card's border
    layout.addWidget(content)
    return card


def _header(ws: StudioWorkspace) -> ui_kit.HeaderBand:
    mark = QLabel()
    mark.setPixmap(QIcon(str(MARK_SVG)).pixmap(QSize(30, 30), ws.devicePixelRatioF()))
    ws.artifact_pills = {label: QLabel() for label, _kind, _file in _ARTIFACTS}
    ws.sync_pill = ui_kit.ClickableLabel()
    ws.sync_pill.setObjectName("actionPill")
    ws.sync_pill.clicked.connect(ws._on_sync_indicator_clicked)
    pills = QWidget()
    pills.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # centred, not stretched
    pills.setLayout(ui_kit.hbox(*ws.artifact_pills.values(), ui_kit.vertical_rule(), ws.sync_pill))
    pills.layout().setSpacing(6)

    ws.server_pill = QToolButton()
    ws.server_pill.setObjectName("serverPill")
    ws.server_pill.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    server_menu = QMenu(ws.server_pill)
    ws.restart_server_action = server_menu.addAction("Restart server")
    ws.restart_server_action.setToolTip(
        "Restart the backend server (recover a hung / unresponsive backend without relaunching)")
    ws.restart_server_action.triggered.connect(ws._on_restart_backend)
    server_menu.addAction("LLM settings…").triggered.connect(ws._on_settings)
    ws.server_pill.setMenu(server_menu)

    export = QPushButton("Export ▾")
    export.setToolTip("Procedure to Markdown… / Procedure to Word…")
    export.setMenu(ws.export_menu)
    skill_chat = QPushButton("✦ Skill chat…")
    skill_chat.setToolTip("Run an authoring skill")
    skill_chat.clicked.connect(lambda: launch_skill_chat(ws))
    tools = QWidget()
    tools.setLayout(ui_kit.hbox(export, skill_chat))
    tools.layout().setSpacing(6)
    ws.save_all_btn = QPushButton("Save all")
    ws.save_all_btn.clicked.connect(ws._on_save_all)

    ws.header = ui_kit.HeaderBand(
        "No test open", "Procedo Studio",
        [ws.server_pill, tools, ui_kit.vertical_rule(), ws.save_all_btn], mark=mark, status=[pills])
    ws.header.title.setMinimumWidth(210)
    return ws.header


def _footer(ws: StudioWorkspace) -> ui_kit.StatusLine:
    ws.project_link = ui_kit.link("Project: none", ws._on_project_indicator_clicked)
    ws.rules_link = ui_kit.link("Rules: none", ws._on_rules_indicator_clicked)
    ws.bundle_label = QLabel("Bundle: none")
    ws.validator_label = QLabel()
    ws.footer = ui_kit.StatusLine([ws.project_link, ws.rules_link, ws.bundle_label],
                                  [ws.validator_label])
    return ws.footer


# ---- Painting the state ----------------------------------------------------------------------

def _show_view(ws: StudioWorkspace, index: int) -> None:
    ws.view_buttons.button(index).setChecked(True)
    ws.view_files.setText(VIEWS[index][1])


def refresh_header(ws: StudioWorkspace) -> None:
    """The open test and its step count, the Text / JSON / Code pills (saved, modified or
    missing), the Sync pill, and Save all (primary, with the count, while something is
    unsaved)."""
    am = ws.artifact_manager
    test_dir = am.test_dir if am is not None else None
    ws.header.title.setText(test_dir.name if test_dir else "No test open")
    root = ws.project_manager.project_root
    steps = _step_count(am)
    parts = ["Procedo Studio", Path(root).name if root else "",
             f"{steps} step{'' if steps == 1 else 's'}" if steps is not None else ""]
    ws.header.subtitle.setText(" · ".join(part for part in parts if part))
    unsaved = 0
    for label, kind, file in _ARTIFACTS:
        pill = ws.artifact_pills[label]
        if am is not None and am.is_dirty(kind):
            unsaved += 1
            ui_kit.set_pill(pill, "warning", f"{label} ● modified")
            pill.setToolTip(f"{file} has unsaved changes.")
        elif am is not None and am.get_content(kind).strip():
            ui_kit.set_pill(pill, "success", f"{label} ✓")
            pill.setToolTip(f"{file} is saved.")
        else:
            ui_kit.set_pill(pill, "neutral", f"{label} ○")
            pill.setToolTip(f"No {file}." if am is not None else "No test loaded.")
    ws._document_controller.update_sync_indicator()
    ws.save_all_btn.setText(f"Save all · {unsaved}" if unsaved else "Save all")
    ws.save_all_btn.setEnabled(bool(unsaved))
    ui_kit.set_primary(ws.save_all_btn, bool(unsaved))


def _step_count(am) -> int | None:
    """The procedure's numbered steps, as the generated code counts them (None: no JSON)."""
    data = am.get_json_parsed() if am is not None else None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return None
    return len(numbered_steps(data["steps"]))


def refresh_footer(ws: StudioWorkspace) -> None:
    """Project, rules (folder · documents), bundle and validator by name; full paths in the
    tooltips."""
    pm = ws.project_manager
    root = Path(pm.project_root) if pm.project_root else None
    ws.project_link.setText(f"Project: {root.name}" if root else "Project: none")
    ws.project_link.setToolTip(
        (f"{root}\n" if root else "") + "Click to open a project.")
    rules = Path(pm.rules_root) if pm.rules_root else None
    if rules is None:
        ws.rules_link.setText("Rules: none")
    else:
        docs = len(pm.get_rules_files())
        ws.rules_link.setText(f"Rules: {_short(rules, root)} · {docs} doc{'' if docs == 1 else 's'}")
    ws.rules_link.setToolTip((f"{rules}\n" if rules else "") + "Click to choose the rules folder.")
    bundle = bundle_registry.read_active_bundle(root) if root else None
    ws.bundle_label.setText(f"Bundle: {bundle.id} {bundle.version}" if bundle else "Bundle: none")
    ws.validator_label.setText(
        f"Validator: {'on' if validator_loop_settings.is_enabled(root) else 'off'}"
        f" · {validator_loop_settings.max_attempts(root)} attempts")


def _short(folder: Path, root: Path | None) -> str:
    """*folder* relative to the project when inside it, else its name."""
    if root is not None and folder.is_relative_to(root):
        return str(folder.relative_to(root))
    return folder.name


def show_server_state(ws: StudioWorkspace, tone: str, text: str, tip: str) -> None:
    """The server pill: *text* in *tone* (``▾``: it opens its menu), *tip* as its tooltip."""
    ui_kit.set_pill(ws.server_pill, tone, f"{text} ▾")
    ws.server_pill.setToolTip(tip)
