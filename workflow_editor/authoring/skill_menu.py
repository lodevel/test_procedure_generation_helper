"""The editor's **Skills** menu — discovers chat skills and launches the
skill-chat dialog.

Kept out of ``main_window`` so that the (mixed-EOL) main window only needs a
one-line call: ``install_skills_menu(self)``. Everything host-specific is reached
through the passed-in ``MainWindow`` (project root, backend factory, the text tab
for the insert, the context sources).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QMessageBox

from . import load_skills, load_wizards, locations
from .context_sources import (
    ArtifactProvider,
    ArtifactsSource,
    DocumentsSource,
    RulesSource,
)
from .netlist_text import (
    format_component_ids,
    format_other_component_ids,
    format_netlist,
)

log = logging.getLogger(__name__)

_DOCS_SUBDIR = "documents"
_AUTHORING_GUIDE = "authoring-a-skill.md"  # under the editor's docs/ folder


def install_skills_menu(main_window) -> None:
    """Add a **Skills** menu to ``main_window``, repopulated each time it opens
    (so freshly-dropped skills appear without a restart)."""
    mb = main_window.menuBar()
    menu = mb.addMenu("S&kills")
    # Keep Help rightmost (standard convention): move the Skills menu just before it.
    help_act = next((a for a in mb.actions()
                     if a.menu() is not None and a.text().replace("&", "") == "Help"), None)
    if help_act is not None:
        mb.removeAction(menu.menuAction())
        mb.insertMenu(help_act, menu)
    menu.aboutToShow.connect(lambda: _populate(main_window, menu))
    _populate(main_window, menu)


# --------------------------------------------------------------------------- #
# menu population                                                             #
# --------------------------------------------------------------------------- #

def _project_root(main_window) -> Optional[Path]:
    pm = getattr(main_window, "project_manager", None)
    root = getattr(pm, "project_root", None) if pm else None
    return Path(root) if root else None


def _populate(main_window, menu) -> None:
    menu.clear()
    try:
        count = len(load_skills(project_root=_project_root(main_window)))
    except Exception:  # noqa: BLE001 — discovery must never break the menu
        log.exception("skill discovery failed")
        count = 0

    # ONE entry — the skill is chosen inside the dialog (a flat menu of every
    # skill doesn't scale).
    chat_act = QAction(f"Skill chat…  ({count})" if count else "Skill chat…", menu)
    chat_act.setEnabled(count > 0)
    chat_act.setToolTip("Run an authoring skill (choose which one in the window).")
    chat_act.triggered.connect(lambda: _launch_chat(main_window))
    menu.addAction(chat_act)
    if not count:
        hint = QAction("(no skills found — add one via 'Open skills folder…')", menu)
        hint.setEnabled(False)
        menu.addAction(hint)

    # The DCDC test wizard (Phase 1: single IC). Driven by the WIZARD-scoped
    # skills (dcdc_classifier + dcdc_authoring), discovered separately from the chat
    # skills above so they never appear in the Skill-chat list.
    try:
        wiz_count = len(load_wizards(project_root=_project_root(main_window)))
    except Exception:  # noqa: BLE001 — discovery must never break the menu
        log.exception("wizard discovery failed")
        wiz_count = 0
    wiz_act = QAction("DCDC test wizard…", menu)
    wiz_act.setEnabled(wiz_count > 0)
    wiz_act.setToolTip("Find a power IC, build its scope test, validate it against "
                       "the netlist, and add it to the project.")
    wiz_act.triggered.connect(lambda: _launch_dcdc_wizard(main_window))
    menu.addAction(wiz_act)

    menu.addSeparator()
    open_act = QAction("Open skills folder…", menu)
    open_act.triggered.connect(lambda: _open_skills_folder(main_window))
    menu.addAction(open_act)

    help_act = QAction("How to write a skill…", menu)
    help_act.triggered.connect(lambda: _open_authoring_guide(main_window))
    menu.addAction(help_act)


def _open_authoring_guide(main_window) -> None:
    # docs/authoring-a-skill.md sits at the editor (submodule) root: this file is
    # workflow_editor/authoring/skill_menu.py → up 3 → <editor>/docs/.
    doc = Path(__file__).resolve().parents[2] / "docs" / _AUTHORING_GUIDE
    if not doc.is_file():
        QMessageBox.information(
            main_window, "Skills", f"Authoring guide not found at:\n{doc}"
        )
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(doc)))


def _open_skills_folder(main_window) -> None:
    folder = locations.local_skills_dir()
    if folder is None:
        QMessageBox.information(
            main_window, "Skills", "No local skills folder is available."
        )
        return
    folder.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


# --------------------------------------------------------------------------- #
# launching a skill                                                           #
# --------------------------------------------------------------------------- #

def _build_sources(main_window, root: Optional[Path]):
    """Build the picker's (Rules, Documents, Artifacts) sources + the documents
    dir, all bound to the live project. Returns ``(sources, documents_dir)``."""
    pm = main_window.project_manager
    am = getattr(main_window, "artifact_manager", None)

    # Rules — the same docs the text tab sends; strip LLM-useless frontmatter.
    try:
        from ..llm.tab_context import _strip_llm_useless_frontmatter as _strip
    except Exception:  # noqa: BLE001
        def _strip(s):
            return s
    sources = [RulesSource(pm.get_rules_files, transform=_strip)]

    documents_dir = (root / _DOCS_SUBDIR) if root else None
    if documents_dir is not None:
        sources.append(DocumentsSource(documents_dir))

    providers = []
    if am is not None:
        from ..core.artifact_manager import ArtifactType
        providers += [
            ArtifactProvider("procedure_text", "Procedure text",
                             lambda: am.get_content(ArtifactType.PROCEDURE_TEXT)),
            ArtifactProvider("procedure_json", "Procedure JSON",
                             lambda: am.get_content(ArtifactType.PROCEDURE_JSON)),
            ArtifactProvider("test_code", "Test code",
                             lambda: am.get_content(ArtifactType.TEST_CODE)),
        ]
    from ..core import odb_inspect
    # Cache the loaded board for the dialog's lifetime: load_board spawns an ODB
    # CLI subprocess (~seconds), and the picker re-materializes on every checkbox
    # toggle for the token readout — without this each toggle would re-run it.
    # Both providers below share the single load.
    _board_cache: dict = {}

    def _board() -> dict:
        if "board" not in _board_cache:
            _board_cache["board"] = odb_inspect.load_board(root)
        return _board_cache["board"]

    # Connectivity and part numbers are SEPARATE toggles: connectivity is small
    # and broadly useful; the IC part-number block is larger and only some skills
    # need it.
    providers.append(ArtifactProvider(
        "netlist", "Netlist (connectivity)", lambda: format_netlist(_board())))
    providers.append(ArtifactProvider(
        "component_ids", "Component part numbers (ICs: U, IC)",
        lambda: format_component_ids(_board())))
    providers.append(ArtifactProvider(
        "other_component_ids", "Component part numbers (other: non-IC)",
        lambda: format_other_component_ids(_board())))
    sources.append(ArtifactsSource(providers))
    return sources, documents_dir


def _make_insert_callback(main_window):
    """Raw-append the draft to the OPEN TEST's procedure text editor (the live
    source of truth) and surface the Text tab; the editor's own ``textChanged``
    keeps the artifact in sync.

    No test open → no procedure to insert into: warn and do nothing rather than
    dump the draft into an unbound editor. ``artifact_manager`` is None until a
    test is opened from the workspace/tests widget."""
    def insert(draft: str) -> None:
        if getattr(main_window, "artifact_manager", None) is None:
            QMessageBox.information(
                main_window, "No test open",
                "Open or create a test first — the draft is inserted into the "
                "open test's procedure.",
            )
            return
        tab = getattr(main_window, "text_only_tab", None)
        editor = getattr(tab, "text_editor", None)
        if editor is None:
            return
        existing = editor.toPlainText()
        editor.setPlainText(f"{existing}\n\n{draft}" if existing.strip() else draft)
        tabs = getattr(main_window, "tab_widget", None)
        if tabs is not None and tab is not None:
            tabs.setCurrentWidget(tab)

    return insert


def _launch_dcdc_wizard(main_window) -> None:
    """Open the DCDC test wizard (modeless). Held on ``main_window`` so the GC
    does not collect the window while it is open."""
    from ..dock.dcdc_wizard_dialog import DcdcWizardDialog

    dlg = DcdcWizardDialog(main_window, parent=main_window)
    main_window._dcdc_wizard_dialog = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


def _launch_chat(main_window) -> None:
    from ..dock.skill_chat_dialog import SkillChatDialog

    root = _project_root(main_window)
    skills = load_skills(project_root=root)
    if not skills:
        QMessageBox.information(
            main_window, "Skills",
            "No skills found. Use 'Open skills folder…' to add one, or ship one "
            "in the built-in library.",
        )
        return
    sources, documents_dir = _build_sources(main_window, root)
    dialog = SkillChatDialog(
        skills=skills,
        sources=sources,
        backend_factory=main_window.backend_factory,
        documents_dir=documents_dir,
        insert_callback=_make_insert_callback(main_window),
        parent=main_window,
    )
    # Hold a reference so the modeless dialog isn't garbage-collected.
    refs = getattr(main_window, "_skill_chat_dialogs", None)
    if refs is None:
        refs = main_window._skill_chat_dialogs = []
    refs.append(dialog)
    dialog.show()
