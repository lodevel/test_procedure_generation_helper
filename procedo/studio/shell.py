"""Procedo Studio's frame, as on the approved board S1: the header band (the open test, its
Text | Code version switch and the other version's "inactive, not updated" pill (boards
StudioCode, StudioCompare), its Text / JSON / Code and Sync pills, the OpenCode pill and its menu,
Export, Skill chat, Save all), the segmented switch between the four views — or, for a code test
or a file of shared phases, the code view —, the Workspace column, the right panel (board
StudioPanel: Assistant | Phases, folding to a rail like Bench's run window side pane, remembered)
and the status line (project, rules, bundle, validator).

Built from ``procedo.ui.ui_kit``, so Studio's frame is Bench's. ``StudioWorkspace`` owns
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
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from procedo.core import app_settings, project_bundle, test_format
from procedo.core.paths import exists_quiet
from procedo.ui import ui_kit
from procedo.ui.segmented import segment, segmented

from .authoring.skill_menu import launch_skill_chat
from .core import ArtifactType
from .core.procedure_steps import numbered_steps
from .llm import validator_loop_settings
from .widgets.shared_phases_panel import used_by_text

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
#: A test's two versions: the switch's label and the file each one is.
_VERSIONS = {
    test_format.TEXT: ("Text", "procedure_text.md"),
    test_format.CODE: ("Code", test_format.CODE_SCRIPT),
}
#: The side columns' widths on the board (the views take the rest).
_WORKSPACE_WIDTH = 246
_ASSISTANT_WIDTH = 342
#: The right panel's tabs.
ASSISTANT_TAB, PHASES_TAB = 0, 1


# ---- Building ------------------------------------------------------------------------------


def build_view_column(ws: StudioWorkspace, views: list[QWidget]) -> QWidget:
    """The segmented switch over the stacked views (``ws.view_stack``), the current view's
    files right of the switch; switching calls the document controller's ``on_tab_changed``."""
    ws.view_stack = QStackedWidget()
    ws.view_buttons = QButtonGroup(ws)
    ws.view_files = ui_kit.monospace(ui_kit.note(wrap=False))
    ws.view_stack.currentChanged.connect(lambda index: _show_view(ws, index))
    ws.view_stack.currentChanged.connect(ws._document_controller.on_tab_changed)
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
    ws.assistant_column = _side_panel(ws)
    ws.middle = QStackedWidget()  # the text views, or the open code test's view
    ws.middle.addWidget(ws.view_column)
    ws.middle.addWidget(ws.code_view)
    columns = QSplitter(Qt.Orientation.Horizontal)
    columns.setChildrenCollapsible(False)
    for column in (ws.workspace_column, ws.middle, ws.assistant_column):
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
    for action, column in (
        (ws.toggle_workspace_action, ws.workspace_column),
        (ws.toggle_dock_action, ws.assistant_column),
    ):
        action.toggled.connect(column.setVisible)
    ws.workspace_widget.shared_phases.loaded.connect(ws.code_view.show_phases)
    show_code_view(ws, False)
    refresh_header(ws)
    refresh_footer(ws)
    ws.llm_server.refresh_indicator()


def _card(content: QWidget, width: int) -> QFrame:
    """*content* in a white card, *width* wide at the least (a side column of the board)."""
    card = ui_kit.card_frame()
    card.setMinimumWidth(width)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(1, 1, 1, 1)  # inside the card's border
    layout.addWidget(content)
    return card


def _side_panel(ws: StudioWorkspace) -> QWidget:
    """The right panel: Assistant | Phases (the code view's phase list, with its count) in
    ``ui_kit.CollapsibleSidePane``, its fold remembered."""
    ws.assistant = QStackedWidget()  # the Assistant, or what it says for a code test
    ws.assistant.addWidget(ws.dock)
    ws.assistant.addWidget(_code_assistant())
    ws.side_tabs = QTabWidget()
    ws.side_tabs.setMinimumWidth(_ASSISTANT_WIDTH)
    ws.side_tabs.addTab(ws.assistant, "Assistant")
    ws.side_tabs.addTab(ws.code_view.phases, "Phases")
    ws.phase_count = QLabel()
    ws.side_tabs.tabBar().setTabButton(PHASES_TAB, QTabBar.ButtonPosition.RightSide, ws.phase_count)
    key = app_settings.STUDIO_SIDE_PANE
    ws.side_pane = ui_kit.CollapsibleSidePane(
        ws.side_tabs, collapsed=app_settings.get_side_pane_collapsed(key)
    )
    ws.side_pane.toggled.connect(lambda folded: app_settings.set_side_pane_collapsed(key, folded))
    return ws.side_pane


#: The Assistant tab while a code test or a file of shared phases is open (board StudioCode).
CODE_ASSISTANT_NOTE = "Writing code tests with the AI comes later."


def _code_assistant() -> QWidget:
    page = QWidget()
    column = QVBoxLayout(page)
    column.setContentsMargins(14, 12, 14, 12)
    column.addWidget(ui_kit.bold("Assistant"))
    column.addWidget(ui_kit.note(CODE_ASSISTANT_NOTE))
    column.addStretch(1)
    return page


def mark_label(size: int, ratio: float) -> QLabel:
    """Procedo Studio's mark, *size* pixels square on a screen of pixel *ratio* (the header's,
    the About box's)."""
    label = QLabel()
    label.setPixmap(QIcon(str(MARK_SVG)).pixmap(QSize(size, size), ratio))
    return label


def _header(ws: StudioWorkspace) -> ui_kit.HeaderBand:
    mark = mark_label(30, ws.devicePixelRatioF())
    ws.artifact_pills = {label: QLabel() for label, _kind, _file in _ARTIFACTS}
    ws.sync_pill = ui_kit.ClickableLabel()
    ws.sync_pill.setObjectName("actionPill")
    ws.sync_pill.clicked.connect(ws._document_controller.on_sync_indicator_clicked)
    pills = QWidget()
    pills.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
    )  # centred, not stretched
    pills.setLayout(ui_kit.hbox(*ws.artifact_pills.values(), ui_kit.vertical_rule(), ws.sync_pill))
    pills.layout().setSpacing(6)
    ws.artifact_status = pills  # a text test's: hidden for a code test

    ws.version_segments = {version: segment(label) for version, (label, _file) in _VERSIONS.items()}
    versions = QButtonGroup(ws)
    for version, button in ws.version_segments.items():
        versions.addButton(button)
        button.clicked.connect(
            lambda _checked=False, v=version: ws._code_test_controller.on_version_clicked(v)
        )
    ws.version_switch = segmented(*ws.version_segments.values())
    ws.version_switch.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    ws.inactive_pill = QLabel()
    ws.used_by_pill = QLabel()  # a file of shared phases: how many tests use it
    show_versions(ws, None, "")

    ws.server_pill = QToolButton()
    ws.server_pill.setObjectName("serverPill")
    ws.server_pill.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    server_menu = QMenu(ws.server_pill)
    ws.restart_server_action = server_menu.addAction("Restart server")
    ws.restart_server_action.setToolTip(
        "Restart the backend server (recover a hung / unresponsive backend without relaunching)"
    )
    ws.restart_server_action.triggered.connect(ws._assistant_controller.on_restart_backend)
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
    ws.save_all_btn.clicked.connect(ws._document_controller.on_save_all)

    ws.header = ui_kit.HeaderBand(
        "No test open",
        "Procedo Studio",
        [ws.server_pill, tools, ui_kit.vertical_rule(), ws.save_all_btn],
        mark=mark,
        status=[ws.version_switch, ws.inactive_pill, ws.used_by_pill, pills],
    )
    ws.header.title.setMinimumWidth(210)
    return ws.header


def _footer(ws: StudioWorkspace) -> ui_kit.StatusLine:
    ws.project_link = ui_kit.link("Project: none", ws._project_controller.on_open_project)
    ws.rules_link = ui_kit.link("Rules: none", ws._on_rules_indicator_clicked)
    ws.bundle_label = QLabel("Bundle: none")
    ws.validator_label = QLabel()
    ws.loading = ui_kit.LoadingCue()  # background work no card shows (a save's media refresh, …)
    ws.footer = ui_kit.StatusLine(
        [ws.project_link, ws.rules_link, ws.bundle_label], [ws.loading, ws.validator_label]
    )
    return ws.footer


# ---- Painting the state ----------------------------------------------------------------------


def show_code_view(ws: StudioWorkspace, on: bool) -> None:
    """The code view (*on*: a code test or a file of shared phases), else the text views: the
    middle column, the Assistant tab (its note for code), the Phases tab (code only), File ›
    Save's label."""
    ws.middle.setCurrentWidget(ws.code_view if on else ws.view_column)
    ws.assistant.setCurrentIndex(1 if on else 0)
    if not on:
        ws.side_tabs.setCurrentIndex(ASSISTANT_TAB)
    ws.side_pane.set_tab_visible(PHASES_TAB, on)
    view = ws.code_view.name if on else VIEWS[ws.view_stack.currentIndex()][0]
    ws.save_action.setText(f"&Save {view}")


def show_versions(ws: StudioWorkspace, test_dir: Path | None, active: str) -> None:
    """The open test's Text | Code switch (hidden when none is open): its *active* version
    checked, the other offered when the test has it, then its "inactive, not updated" pill."""
    ws.version_switch.setVisible(test_dir is not None)
    ws.inactive_pill.hide()
    if test_dir is None:
        return
    has = {version: exists_quiet(test_dir / file) for version, (_label, file) in _VERSIONS.items()}
    for version, button in ws.version_segments.items():
        name, file = _VERSIONS[version][0].lower(), _VERSIONS[version][1]
        button.setChecked(version == active)
        button.setEnabled(version == active or has[version])
        if version == active:
            button.setToolTip(f"Bench runs this version ({file}).")
        elif has[version]:
            button.setToolTip(f"Run the {name} version ({file}) from now on.")
        else:
            button.setToolTip(f"This test has no {name} version ({file}).")
    other = next(version for version in _VERSIONS if version != active)
    if has[other]:
        label, file = _VERSIONS[other]
        ui_kit.set_pill(ws.inactive_pill, "warning", f"{label} version: inactive, not updated")
        ws.inactive_pill.setToolTip(f"{file} is kept as it is: nothing updates it.")
        ws.inactive_pill.show()


def _show_view(ws: StudioWorkspace, index: int) -> None:
    ws.view_buttons.button(index).setChecked(True)
    ws.view_files.setText(VIEWS[index][1])


def refresh_header(ws: StudioWorkspace) -> None:
    """The open test and its step count — or the open file of shared phases and how many tests
    use it —, the Phases tab's count, the Text / JSON / Code pills (saved, modified or missing),
    the Sync pill, and Save all (primary, with the count, while something is unsaved)."""
    am, code = ws.artifact_manager, ws.code_view
    test_dir = am.test_dir if am is not None else code.test_dir
    root = ws.project_manager.project_root
    phases = len(code.described["phases"]) if code.described else None
    ui_kit.set_pill(ws.phase_count, "neutral", str(phases) if phases is not None else "")
    ws.phase_count.setVisible(phases is not None)
    ws.phase_count.adjustSize()
    ws.side_tabs.setTabText(PHASES_TAB, "Phases")  # the tab bar makes room for its new size
    if code.shared_file:
        ws.header.title.setText(code.name)
        users = {
            test
            for p in ws.workspace_widget.shared_phases.phases
            if p["file"] == code.name
            for test in p["used_by"]
        }
        ui_kit.set_pill(ws.used_by_pill, "neutral", used_by_text(len(users)))
        what = "shared phases"
    elif code.phase_file is not None:
        ws.header.title.setText(code.name)
        what = ""
    else:
        ws.header.title.setText(test_dir.name if test_dir else "No test open")
        steps = phases if code.test_dir is not None else _step_count(am)
        what = f"{steps} step{'' if steps == 1 else 's'}" if steps is not None else ""
    ws.used_by_pill.setVisible(code.shared_file)
    parts = ["Procedo Studio", Path(root).name if root else "", what]
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
    ws.artifact_status.setVisible(not code.is_open)
    if code.is_open:
        unsaved = int(code.dirty)
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
    ws.project_link.setToolTip((f"{root}\n" if root else "") + "Click to open a project.")
    rules = Path(pm.rules_root) if pm.rules_root else None
    docs = pm.get_rule_docs()
    own = sum(doc.origin == "project" for doc in docs)
    if rules is None:
        text = "Rules: none"
    else:
        n = len(docs) - own
        text = f"Rules: {_short(rules, root)} · {n} doc{'' if n == 1 else 's'}"
    ws.rules_link.setText(text + (f" + {own} project" if own else ""))
    ws.rules_link.setToolTip((f"{rules}\n" if rules else "") + "Click to choose the rules folder.")
    ws.dock.refresh_rules_picker()  # the Assistant's picker counts the same rules
    ws.bundle_label.setText(f"Bundle: {bundle_name(root) or 'none'}")
    ws.validator_label.setText(
        f"Validator: {'on' if validator_loop_settings.is_enabled(root) else 'off'}"
        f" · {validator_loop_settings.max_attempts(root)} attempts"
    )


def bundle_name(project_root) -> str:
    """The project's active bundle as Studio names it (``baseline 2.8.1``), "" when none."""
    bundle = project_bundle.read_active_bundle(Path(project_root)) if project_root else None
    return f"{bundle.id} {bundle.version}" if bundle else ""


def _short(folder: Path, root: Path | None) -> str:
    """*folder* relative to the project when inside it, else its name."""
    if root is not None and folder.is_relative_to(root):
        return str(folder.relative_to(root))
    return folder.name


def show_server_state(ws: StudioWorkspace, tone: str, text: str, tip: str) -> None:
    """The server pill: *text* in *tone* (``▾``: it opens its menu), *tip* as its tooltip."""
    ui_kit.set_pill(ws.server_pill, tone, f"{text} ▾")
    ws.server_pill.setToolTip(tip)
