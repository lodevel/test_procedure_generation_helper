"""Studio's side of the authoring API (``procedo.core.authoring_api``): the one object a
launchable wizard's entry receives instead of Studio's internals, so a wizard copy shipped in a
project bundle survives Studio's renames and refactors.

A Qt adapter (it makes the skill-chat widget). ``skill_menu`` imports this module, so this one
imports ``skill_menu`` only inside a function.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from procedo.core.authoring_api import ProjectChanged
from test_procedure_gui import theme as bench_theme

from ..core import odb_inspect
from ..core.artifact_manager import ArtifactManager, ArtifactType
from ..dialogs.settings_dialog import load_settings, save_settings
from ..dock.skill_chat_widget import SkillChatWidget
from ..pack_ops import pack_parsers
from . import load_wizards
from .wizard_flows import WizardFlow, required_api

log = logging.getLogger(__name__)


def wizard_host(flow: WizardFlow, main_window) -> Any:
    """What the flow's launch entry receives: the authoring API bound to ``main_window``.
    A head declaring no ``authoring-api`` is a bundle copy built before the API existed
    and still gets the main window; slice 8 (``workflow_editor`` → ``procedo.studio``)
    deletes that branch — those copies import Studio's old package and break anyway."""
    if required_api(flow) is None:
        return main_window
    return StudioAuthoringApi(main_window)


class StudioAuthoringApi:
    """Studio's :class:`procedo.core.authoring_api.AuthoringApi`, bound to the project
    open in the main window when it is made. Every member is part of a versioned contract
    with wizards shipped in project bundles: change one only together with ``API_VERSION``."""

    def __init__(self, main_window) -> None:
        self._mw = main_window
        self._root = _open_project_root(main_window)

    def _project_manager(self):
        """Studio's project manager, still on the launch project; :class:`ProjectChanged`
        when the operator has opened another one since (a modeless wizard outlives it)."""
        now = _open_project_root(self._mw)
        if now != self._root:
            raise ProjectChanged(
                f"The open project changed since this wizard started (it was {self._root}, "
                f"now {now}). Reopen {self._root}, or close the wizard and start it again.")
        return getattr(self._mw, "project_manager", None)

    @property
    def project_root(self) -> Optional[Path]:
        return self._root

    def get_tests_dir(self) -> Optional[Path]:
        pm = self._project_manager()
        return pm.get_tests_dir() if pm is not None else None

    def create_test_folder(self, name: str) -> Optional[Path]:
        pm = self._project_manager()
        return pm.create_test_folder(name) if pm is not None else None

    def write_procedure_text(self, folder: Path, text: str) -> None:
        self._project_manager()
        am = _artifacts(folder)
        am.set_content(ArtifactType.PROCEDURE_TEXT, text)
        am.save_artifact(ArtifactType.PROCEDURE_TEXT)

    def write_procedure_json(self, folder: Path, procedure: dict) -> None:
        self._project_manager()
        am = _artifacts(folder)
        am.set_json_from_dict(procedure)
        am.save_artifact(ArtifactType.PROCEDURE_JSON)

    def sync_meta_text(self, text: str) -> Optional[str]:
        if not pack_parsers.supports_sync_meta(self.project_root):
            return None
        synced, _warnings = pack_parsers.sync_meta_text(text, project_root=self.project_root)
        return synced

    def parse_procedure_text(self, text: str) -> dict:
        procedure, _warnings = pack_parsers.parse_text(text, self.project_root)
        return procedure

    def load_board(self) -> dict:
        return odb_inspect.load_board(self.project_root)

    def wizard_skills(self) -> dict:
        return {w.skill_id: w for w in load_wizards(self.project_root)}

    def new_skill_chat(self, skill, *, tab_id: str, with_context: bool = False,
                       parent=None, dispatch_gate: Optional[Callable[..., None]] = None):
        sources, documents_dir = self._context_sources() if with_context else (None, None)
        return SkillChatWidget(
            [skill] if skill else [],
            getattr(self._mw, "backend_factory", None),
            sources=sources,
            documents_dir=documents_dir,
            backend_tab_id=tab_id,
            show_skill_selector=False,
            show_run_button=False,
            dispatch_gate=dispatch_gate,
            parent=parent,
        )

    def _context_sources(self):
        """The context picker's sources + documents dir; none when they cannot be built
        (no project open)."""
        from .skill_menu import build_sources  # skill_menu imports this module

        try:
            return build_sources(self._mw, self.project_root)
        except Exception:  # noqa: BLE001
            log.exception("wizard: could not build the chat's context sources")
            return [], None

    def wizard_setting(self, key: str, default: Any) -> Any:
        return load_settings().get("wizard", {}).get(key, default)

    def set_wizard_setting(self, key: str, value: Any) -> None:
        settings = load_settings()
        settings.setdefault("wizard", {})[key] = value
        save_settings(settings)

    def muted_color(self) -> str:
        return bench_theme.muted_color()

    def success_color(self) -> str:
        return bench_theme.success_color()


def _open_project_root(main_window) -> Optional[Path]:
    return getattr(getattr(main_window, "project_manager", None), "project_root", None)


def _artifacts(folder: Path) -> ArtifactManager:
    """An artifact manager on one test folder: its saves apply Studio's rules (folder id, media)."""
    am = ArtifactManager()
    am.set_test_dir(folder)
    return am


__all__ = ["StudioAuthoringApi", "wizard_host"]
