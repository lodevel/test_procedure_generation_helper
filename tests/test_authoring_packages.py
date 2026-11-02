"""Skills, wizards and tools come from the switched-on packages, the project's bundle and the
project itself: every copy is listed with its provenance, a reference by id takes the project's
copy, then the bundle's, then the package's, and the tools of any switched-on package run.

Packages are built under ``tmp_path`` (the real ``packages/`` folder is never read)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from procedo.core import package_store
from procedo.studio.authoring import (
    SkillSource,
    load_skills,
    load_wizards,
    locations,
    rs_core,
    skills_by_id,
    skill_menu,
    wizard_flows,
)
from procedo.studio.authoring.tool_folders import build_skill_tools_universe
from procedo.studio.llm import launch_config, launch_files

from .test_main_window_characterization import qapp as qapp


@pytest.fixture
def packages(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "packages"
    root.mkdir()
    monkeypatch.setattr(package_store, "packages_root", lambda: root)
    monkeypatch.setattr(package_store, "local_root", lambda: root / "local")
    monkeypatch.setattr(package_store, "installed_root", lambda: root / "installed")
    return root


def _package(packages: Path, folder: str, name: str) -> Path:
    root = packages / "builtin" / folder
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"id": folder, "name": name}), encoding="utf-8")
    return root


def _skill(parent: Path, skill_id: str, body: str, **meta) -> Path:
    folder = parent / skill_id
    folder.mkdir(parents=True)
    front = "".join(f"{k}: {v}\n" for k, v in {"name": skill_id, **meta}.items())
    (folder / "SKILL.md").write_text(f"---\n{front}---\n{body}\n", encoding="utf-8")
    return folder


def _tool(parent: Path, folder_name: str, server: str) -> Path:
    folder = parent / folder_name
    folder.mkdir(parents=True)
    (folder / "tools.json").write_text(json.dumps({"server": server, "tools": ["go"]}))
    (folder / "tools.py").write_text(f'SERVER_NAME = "{server}"\nTOOLS = []\n', encoding="utf-8")
    return folder


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _provenances(copies) -> list[str]:
    return [c.provenance for c in copies]


# --------------------------------------------------------------------------- #
# the same skill in the project, its bundle and a package                      #
# --------------------------------------------------------------------------- #


def test_every_copy_is_listed_and_a_reference_takes_project_then_bundle_then_package(
    packages, project
):
    package = _package(packages, "acme", "Acme lab")
    _skill(package / "skills", "dcdc", "package body")
    bundled = _skill(project / "bundle" / "authoring_skills", "dcdc", "bundle body")
    own = _skill(project / "authoring_skills", "dcdc", "project body")

    copies = [s for s in load_skills(project) if s.skill_id == "dcdc"]
    assert _provenances(copies) == ["Project", "Bundle", "Acme lab"]
    assert [s.listing for s in copies][2] == "dcdc — Acme lab"
    assert skills_by_id(copies)["dcdc"].system_prompt == "project body"

    shutil.rmtree(own)
    copies = [s for s in load_skills(project) if s.skill_id == "dcdc"]
    assert _provenances(copies) == ["Bundle", "Acme lab"]
    assert skills_by_id(copies)["dcdc"].system_prompt == "bundle body"

    shutil.rmtree(bundled)
    copies = [s for s in load_skills(project) if s.skill_id == "dcdc"]
    assert skills_by_id(copies)["dcdc"].system_prompt == "package body"
    assert skills_by_id(copies)["dcdc"].source is SkillSource.PACKAGE


def test_local_is_a_package_like_any_other(packages, project):
    _skill(package_store.local_root() / "skills", "mine", "local body")
    (copy,) = [s for s in load_skills(project) if s.skill_id == "mine"]
    assert copy.provenance == "Local"
    assert locations.local_skills_dir() == package_store.local_root() / "skills"


def test_a_versioned_package_skill_keeps_its_id(packages, project):
    package = _package(packages, "acme", "Acme lab")
    _skill(package / "skills" / "dcdc", "1.0.0", "old")
    _skill(package / "skills" / "dcdc", "2.0.0", "new")
    (copy,) = [s for s in load_skills(project) if s.skill_id == "dcdc"]
    assert copy.system_prompt == "new" and copy.path.name == "2.0.0"


# --------------------------------------------------------------------------- #
# switched off                                                                 #
# --------------------------------------------------------------------------- #


def test_a_switched_off_package_offers_nothing(packages, project):
    package = _package(packages, "acme", "Acme lab")
    _skill(package / "skills", "dcdc", "body")
    _skill(package / "wizards", "wiz", "body", flow="f", launch="m:C")
    _tool(package / "tools", "acme_tools", "acme_tools")
    assert [s.skill_id for s in load_skills(project)] == ["dcdc"]
    assert [s.skill_id for s in load_wizards(project)] == ["wiz"]
    assert "acme_tools" in build_skill_tools_universe(project)

    package_store.set_enabled("acme", False)

    assert load_skills(project) == []
    assert load_wizards(project) == []
    assert build_skill_tools_universe(project) == {}


# --------------------------------------------------------------------------- #
# tools                                                                        #
# --------------------------------------------------------------------------- #


def test_the_tools_of_any_switched_on_package_run_local_included(packages, project):
    package = _package(packages, "acme", "Acme lab")
    _tool(package / "tools", "acme_tools", "acme_server")
    _tool(package_store.local_root() / "tools", "mine", "local_server")
    # a skill folder shipping its own tools keeps working
    skill = _tool(package / "skills", "with_tools", "skill_server")
    (skill / "SKILL.md").write_text("---\nname: s\n---\nbody\n", encoding="utf-8")
    # a project-own tool folder never runs
    _tool(project / "authoring_skills", "proj", "project_server")
    # a bundled one does
    _tool(project / "bundle" / "tools", "bundled", "bundle_server")

    assert set(build_skill_tools_universe(project)) == {
        "acme_server",
        "local_server",
        "skill_server",
        "bundle_server",
    }
    blocks = launch_config._build_skill_tools_blocks(project)
    assert set(blocks) == {"acme_server", "local_server", "skill_server", "bundle_server"}
    command = blocks["local_server"]["command"]
    assert str(package_store.local_root() / "tools" / "mine") in command


def test_a_bundled_tool_folder_wins_over_the_package_one_of_the_same_name(packages, project):
    package = _package(packages, "acme", "Acme lab")
    _tool(package / "tools", "shared", "package_server")
    _tool(project / "bundle" / "tools", "shared", "bundle_server")
    assert set(build_skill_tools_universe(project)) == {"bundle_server"}


# --------------------------------------------------------------------------- #
# the menus                                                                    #
# --------------------------------------------------------------------------- #


def _window(project: Path):
    return SimpleNamespace(project_manager=SimpleNamespace(project_root=project))


def test_the_skill_chat_lists_each_copy_with_its_provenance(qapp, packages, project, monkeypatch):
    package = _package(packages, "acme", "Acme lab")
    _skill(package / "skills", "dcdc", "p", name="DCDC bring-up")
    _skill(project / "bundle" / "authoring_skills", "dcdc", "b", name="DCDC bring-up")
    shown = {}

    class Dialog:
        def __init__(self, **kwargs):
            shown.update(kwargs)

        def show(self):
            pass

    import procedo.studio.dock.skill_chat_dialog as dialog_module

    monkeypatch.setattr(dialog_module, "SkillChatDialog", Dialog)
    monkeypatch.setattr(skill_menu, "build_sources", lambda mw, root: ([], None))
    monkeypatch.setattr(skill_menu, "_hold_modeless", lambda *a: None)
    skill_menu.launch_skill_chat(SimpleNamespace(**vars(_window(project)), backend_factory=None))
    assert [s.listing for s in shown["skills"]] == [
        "DCDC bring-up — Bundle",
        "DCDC bring-up — Acme lab",
    ]

    from procedo.studio.dock import skill_chat_widget as scw

    widget = scw.SkillChatWidget(shown["skills"], SimpleNamespace(), show_skill_selector=True)
    try:
        combo = widget._skill_combo
        assert [combo.itemText(i) for i in range(combo.count())] == [
            "DCDC bring-up — Bundle",
            "DCDC bring-up — Acme lab",
        ]
    finally:
        widget._worker = None
        widget.shutdown()
        widget.deleteLater()


def _wizard(parent: Path, wizard_id: str, **meta) -> Path:
    return _skill(parent, wizard_id, "body", **{"flow": "bringup", **meta})


def test_the_wizards_menu_lists_each_copy_with_its_provenance(qapp, packages, project, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMenu

    package = _package(packages, "acme", "Acme lab")
    head = {"name": "DCDC bring-up", "launch": "m:C", "authoring-api": 1}
    _wizard(package / "wizards", "bringup_head", **head)
    _wizard(project / "bundle" / "authoring_wizards", "bringup_head", **head)
    window = _window(project)
    menu = QMenu()
    skill_menu._populate_wizards(window, menu)
    (action,) = menu.actions()
    assert action.isEnabled()

    asked = {}

    def get_item(parent, title, label, items, current, editable):
        asked["items"] = list(items)
        return "", False

    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(get_item))
    skill_menu._choose_and_launch_wizard(window)
    assert asked["items"] == ["DCDC bring-up — Bundle", "DCDC bring-up — Acme lab"]


# --------------------------------------------------------------------------- #
# a wizard's requires                                                          #
# --------------------------------------------------------------------------- #


def test_requires_resolves_a_stage_by_project_then_bundle_then_package(packages, project):
    package = _package(packages, "acme", "Acme lab")
    _wizard(
        project / "bundle" / "authoring_wizards",
        "head",
        **{"launch": "m:C", "requires": "[head, stage]"},
    )
    assert wizard_flows.discover_flows(project) == []  # no stage anywhere yet

    _wizard(package / "wizards", "stage")
    (flow,) = wizard_flows.discover_flows(project)
    assert {s.skill_id: s.provenance for s in flow.skills}["stage"] == "Acme lab"

    _wizard(project / "bundle" / "authoring_wizards", "stage")
    (flow,) = wizard_flows.discover_flows(project)
    assert {s.skill_id: s.provenance for s in flow.skills}["stage"] == "Bundle"

    _wizard(project / "authoring_wizards", "stage")
    (flow,) = wizard_flows.discover_flows(project)
    assert {s.skill_id: s.provenance for s in flow.skills}["stage"] == "Project"
    assert flow.head.provenance == "Bundle"


def test_launchable_code_runs_only_from_a_package_or_the_bundle(packages, project):
    _wizard(project / "authoring_wizards", "head", launch="m:C")
    (flow,) = wizard_flows.discover_flows(project)
    with pytest.raises(wizard_flows.WizardLaunchError, match="package or the bundle"):
        wizard_flows.resolve_launch(flow)


# --------------------------------------------------------------------------- #
# the run_skill server                                                         #
# --------------------------------------------------------------------------- #


def test_run_skill_gets_the_copy_a_reference_by_id_takes(packages, project, tmp_path):
    package = _package(packages, "acme", "Acme lab")
    _skill(package / "skills", "dcdc", "package body")
    bundled = _skill(project / "bundle" / "authoring_skills", "dcdc", "bundle body")
    own = _skill(project / "authoring_skills", "dcdc", "project body")
    launch = tmp_path / "launch"
    launch.mkdir()

    def child_body() -> str:
        block = launch_config._build_run_skill_block(project, launch, "secret")
        command = block["run_skill"]["command"]
        skills_file = command[command.index("--skills-file") + 1]
        assert Path(skills_file) == launch_files.launch_file_path(launch, launch_files.SKILLS_FILE)
        body, _declared = rs_core.resolve_skill("dcdc", rs_core.load_skill_map(skills_file))
        return body

    assert child_body() == "project body"
    shutil.rmtree(own)
    assert child_body() == "bundle body"
    shutil.rmtree(bundled)
    assert child_body() == "package body"
