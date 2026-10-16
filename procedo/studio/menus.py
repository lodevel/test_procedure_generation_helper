"""Procedo Studio's menu bar, in the Menus board's order: File, Edit, View, Project, Skills,
Wizards, Packages, Help.

Names, mnemonics and the Project menu follow Bench's (``procedo/bench/main_menus.py``) where
the board agrees; the two menu bars stay separate builders because their Packages and Help menus
hold different entries.
"""
from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu

from .authoring.skill_menu import install_skills_menu


def setup_menu_bar(ws) -> None:
    """Fill ``ws.menu_bar`` (the hosting window shows it)."""
    bar = ws.menu_bar
    ws._open_recent_menu = QMenu("Open &Recent", ws)
    ws._open_recent_menu.aboutToShow.connect(ws._rebuild_open_recent_menu)
    ws.export_menu = _fill(QMenu("&Export", ws), [  # the header's Export too
        _action(ws, "Procedure to &Markdown…", ws._on_export_markdown,
                tip="Export the current procedure as a Markdown (.md) document."),
        _action(ws, "Procedure to &Word…", ws._on_export_word,
                tip="Export the current procedure as a Word (.docx) document."),
    ])
    ws.export_menu.setToolTip("Export the current procedure to Markdown or Word.")
    # Relabelled for the current view ("&Save Text", ...) by the document controller.
    ws.save_action = _action(ws, "&Save", ws._on_save, QKeySequence.Save)
    ws.mark_sync_action = _action(ws, "Mark Artifacts In &Sync", ws._on_sync_indicator_clicked,
                                  tip="Acknowledge that procedure.json and test.py are coherent")
    # Checkable column toggles; shell.build_frame wires them to the columns.
    ws.toggle_workspace_action = _action(ws, "Show &Workspace", None, "Ctrl+Shift+E", checked=True)
    ws.toggle_dock_action = _action(ws, "Show &Assistant", None, "Ctrl+Shift+A", checked=True)

    _fill(bar.addMenu("&File"), [
        _action(ws, "&New Project…", ws._on_new_project, "Ctrl+N"),
        _action(ws, "&Open Project…", ws._on_open_project, QKeySequence.Open),
        ws._open_recent_menu,
        None,
        ws.save_action,
        _action(ws, "Save &All", ws._on_save_all, "Ctrl+Shift+S"),
        ws.export_menu,
        None,
        _action(ws, "Se&ttings…", ws._on_settings, "Ctrl+,"),
        None,
        _action(ws, "E&xit", lambda: ws.window().close()),
    ])
    _fill(bar.addMenu("&Edit"), [
        _action(ws, "&Find…", ws._on_find, QKeySequence.Find, tip=(
            "Open the find bar on the active tab's text editor "
            "(or the leftmost editor if focus is elsewhere).")),
        _action(ws, "&Replace…", ws._on_replace, "Ctrl+H",
                tip="Open the find/replace bar on the active tab's text editor."),
        None,
        ws.mark_sync_action,
    ])
    _fill(bar.addMenu("&View"), [ws.toggle_workspace_action, ws.toggle_dock_action])
    _fill(bar.addMenu("&Project"), [
        _action(ws, "Project &Configuration…", ws._on_project_configuration, tip=(
            "Edit this project's config.json: bundle, equipment profiles/"
            "patterns, templates, imaging, workflows.")),
        _action(ws, "&Template Manager…", ws._on_template_manager, tip=(
            "Manage saved customer-configuration templates (reusable bundle + "
            "equipment + workflow presets).")),
        _action(ws, "&Scenarios…", ws._on_scenarios, tip=(
            "Create / load / delete named test scenarios (subsets of this "
            "project's tests). Scenarios are run from the main app.")),
    ])
    install_skills_menu(ws)  # Skills, Wizards
    _fill(bar.addMenu("&Packages"), [
        _action(ws, "Bundle &Library…", ws._on_bundle_library, tip=(
            "Manage installed bundles; \"Import into Project\" targets the "
            "open project.")),
        _action(ws, "&Install Package…", ws._on_install_package),
    ])
    _fill(bar.addMenu("&Help"), [
        _action(ws, "&DSL Syntax Reference", ws._on_syntax_reference, "F1"),
        _action(ws, "Word Export &Keywords…", ws._on_export_keywords),
        None,
        _action(ws, "&About", ws._on_about),
    ])


def _action(ws, text, slot, shortcut=None, *, tip="", checked=None) -> QAction:
    action = QAction(text, ws)
    if shortcut is not None:
        action.setShortcut(shortcut)
    if tip:
        action.setToolTip(tip)
    if checked is not None:
        action.setCheckable(True)
        action.setChecked(checked)
    if slot is not None:
        action.triggered.connect(slot)
    return action


def _fill(menu: QMenu, items) -> QMenu:
    """Add *items* to *menu*: a QAction, a QMenu (a submenu) or None (a separator)."""
    for item in items:
        if item is None:
            menu.addSeparator()
        elif isinstance(item, QMenu):
            menu.addMenu(item)
        else:
            menu.addAction(item)
    return menu
