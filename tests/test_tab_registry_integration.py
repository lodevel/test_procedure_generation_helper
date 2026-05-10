"""Tab → registry → button → outcome integration test (Phase 2/3).

Verifies the end-to-end wiring that the Phase 2/3 refactor introduced:

* ``TaskConfigManager.get_validator_specs_for_tab`` returns the
  ``workflows.<tab>.validators`` block.
* ``BaseTab._populate_validator_buttons`` consumes that list and adds
  one ``QPushButton`` per registered, enabled validator.
* ``rebuild_validator_buttons`` swaps the row when the active project
  (and therefore the validator config) changes.
* Clicking a button dispatches through ``validators_registry.get`` and
  routes the outcome to ``main_window.dock.show_validation_result_from_list``.

The tests use a real ``TaskConfigManager`` against ``tmp_path`` projects
and a ``MagicMock`` main_window. The registry is reset before each test
so built-in registrations don't bleed across cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton

from workflow_editor.core.task_config import TaskConfigManager
from workflow_editor.core.validators_registry import (
    ValidatorContext,
    ensure_builtins_registered,
    register,
    unregister_all,
)
from workflow_editor.llm.validator_dispatch import ValidationOutcome
from workflow_editor.tabs.base_tab import BaseTab


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry. Built-ins are re-registered
    on demand by ``ensure_builtins_registered``."""
    unregister_all()
    yield
    unregister_all()


def _seed_project(root: Path, validators: list[dict]) -> None:
    """Write a project config carrying ``workflows.text_json.validators``."""
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text(
        json.dumps({
            "manifest": {"name": "integration test"},
            # No selected_packs → no pack defaults aggregated → the
            # project's validators list is the only contribution.
            "workflows": {"text_json": {"validators": validators}},
        }, indent=2),
        encoding="utf-8",
    )


def _mock_main_window(tmp_path: Path, manager: TaskConfigManager, project_root: Path):
    """Build a MagicMock main_window with the attributes BaseTab touches."""
    mw = MagicMock()
    mw.task_config_manager = manager
    mw.project_manager = SimpleNamespace(project_root=project_root)
    # ArtifactManager stub: each artifact has a ``.content`` field. We use
    # plain SimpleNamespaces so the BaseTab default ``_get_artifact_for_validation``
    # picks them up.
    mw.artifact_manager = SimpleNamespace(
        procedure_text=SimpleNamespace(content="step 1: do thing\n"),
        procedure_json=SimpleNamespace(content='{"name":"t","steps":[{"text":"a"}]}'),
        test_code=SimpleNamespace(content="def test():\n    pass\n"),
    )
    mw.dock = MagicMock()
    return mw


def _make_tab(qapp, main_window, tab_id: str = "text_json") -> BaseTab:
    """Construct a BaseTab and tag it with ``tab_id`` (sub-classes set this
    in __init__; we set it directly to keep the test focused on the
    base-class plumbing)."""
    tab = BaseTab(main_window)
    tab.tab_id = tab_id
    return tab


# ---------------------------------------------------------------------------
# Build / rebuild
# ---------------------------------------------------------------------------


def test_buttons_built_from_project_validators(tmp_path, qapp):
    """A project carrying a validators list produces one button per
    enabled, registered validator."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
        {"id": "core.check_python_syntax",                 "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    mw = _mock_main_window(tmp_path, manager, project_root)
    tab = _make_tab(qapp, mw)

    layout = QHBoxLayout()
    added = tab._build_validator_buttons(layout)
    assert added == 2

    labels = [
        layout.itemAt(i).widget().text()
        for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), QPushButton)
    ]
    assert "Validate Json Schema" in labels
    assert "Check Python Syntax" in labels


def test_disabled_spec_skipped(tmp_path, qapp):
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
        {"id": "core.check_python_syntax",                 "enabled": False},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    tab = _make_tab(qapp, _mock_main_window(tmp_path, manager, project_root))

    layout = QHBoxLayout()
    added = tab._build_validator_buttons(layout)
    assert added == 1


def test_unregistered_id_skipped_with_warning(tmp_path, qapp, caplog):
    """A spec referencing an unknown validator id is silently skipped from
    the button row but emits a warning so the operator can diagnose the
    config error."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
        {"id": "made.up.never_registered",                  "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    tab = _make_tab(qapp, _mock_main_window(tmp_path, manager, project_root))

    layout = QHBoxLayout()
    with caplog.at_level("WARNING"):
        added = tab._build_validator_buttons(layout)
    assert added == 1
    assert any("made.up.never_registered" in rec.message for rec in caplog.records)


def test_rebuild_validator_buttons_swaps_set(tmp_path, qapp):
    """After ``rebuild_validator_buttons``, the row reflects the new
    validator list — even if the project's config is mutated and the
    manager is reloaded."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    tab = _make_tab(qapp, _mock_main_window(tmp_path, manager, project_root))

    layout = QHBoxLayout()
    tab._build_validator_buttons(layout)
    assert _count_validator_buttons(layout) == 1

    # Rewrite the project config and reload — the new validators list
    # should win after rebuild.
    _seed_project(project_root, validators=[
        {"id": "core.check_python_syntax", "enabled": True},
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
    ])
    manager.reload(project_root)
    tab.rebuild_validator_buttons()
    assert _count_validator_buttons(layout) == 2


def test_rebuild_preserves_non_validator_widgets(tmp_path, qapp):
    """``rebuild_validator_buttons`` only removes widgets tagged with the
    ``validator_id`` property — sibling widgets (e.g. a Format JSON button
    a tab put in the same row) survive."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    tab = _make_tab(qapp, _mock_main_window(tmp_path, manager, project_root))

    layout = QHBoxLayout()
    tab._build_validator_buttons(layout)
    sibling = QPushButton("Format JSON")  # no validator_id property
    layout.addWidget(sibling)

    tab.rebuild_validator_buttons()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert sibling in widgets, "non-validator sibling should survive rebuild"


# ---------------------------------------------------------------------------
# Click → dispatch → dock
# ---------------------------------------------------------------------------


def test_click_dispatches_through_registry_to_dock(tmp_path, qapp):
    """Clicking a validator button calls the registered validator with a
    ``ValidatorContext`` populated from the artifact_manager, and the
    outcome's issues land on the dock panel."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "pack.under_test.recording_validator", "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    mw = _mock_main_window(tmp_path, manager, project_root)
    tab = _make_tab(qapp, mw)

    captured: list[ValidatorContext] = []

    def _recording(ctx: ValidatorContext) -> ValidationOutcome:
        captured.append(ctx)
        return ValidationOutcome(ok=False, skipped=False, issues=[])

    register("pack.under_test.recording_validator", _recording)

    layout = QHBoxLayout()
    tab._build_validator_buttons(layout)
    button = next(
        layout.itemAt(i).widget() for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), QPushButton)
        and layout.itemAt(i).widget().property("validator_id")
        == "pack.under_test.recording_validator"
    )
    button.click()

    # Validator received a context with all three artifacts surfaced.
    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.tab_id == "text_json"
    assert ctx.artifact_text == "step 1: do thing\n"
    assert ctx.artifact_json == '{"name":"t","steps":[{"text":"a"}]}'
    assert ctx.artifact_code == "def test():\n    pass\n"

    # Outcome routed to the dock (empty issues -> empty list passed).
    mw.dock.show_validation_result_from_list.assert_called_once_with([])


def test_click_skipped_outcome_clears_dock_findings(tmp_path, qapp):
    """A ``skipped=True`` outcome clears stale findings (empty list to
    the dock) and emits a status hint. No modal popup (plan 2026-05-10)."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "pack.skip.always", "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    mw = _mock_main_window(tmp_path, manager, project_root)
    tab = _make_tab(qapp, mw)

    register("pack.skip.always",
             lambda _ctx: ValidationOutcome(ok=True, skipped=True, reason="not applicable", issues=[]))

    status_msgs: list[str] = []
    tab.status_message.connect(status_msgs.append)

    layout = QHBoxLayout()
    tab._build_validator_buttons(layout)
    button = next(
        layout.itemAt(i).widget() for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), QPushButton)
    )
    button.click()

    mw.dock.show_validation_result_from_list.assert_called_once_with([])
    assert status_msgs == ["not applicable"]


def test_validator_crash_is_reported_not_propagated(tmp_path, qapp, caplog):
    """A raising validator must not blow up the click handler. The status
    bar receives a hint and the exception is logged with traceback."""
    project_root = tmp_path / "proj"
    _seed_project(project_root, validators=[
        {"id": "pack.broken.exploder", "enabled": True},
    ])
    manager = TaskConfigManager(tmp_path / "no_fallback.json", project_root=project_root)
    mw = _mock_main_window(tmp_path, manager, project_root)
    tab = _make_tab(qapp, mw)

    def _explode(_ctx):  # pragma: no cover — body raises
        raise RuntimeError("boom")

    register("pack.broken.exploder", _explode)

    status_msgs: list[str] = []
    tab.status_message.connect(status_msgs.append)

    layout = QHBoxLayout()
    tab._build_validator_buttons(layout)
    button = next(
        layout.itemAt(i).widget() for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), QPushButton)
    )
    with caplog.at_level("ERROR"):
        button.click()  # must not raise

    assert any("crashed" in m for m in status_msgs)
    assert any("pack.broken.exploder" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_validator_buttons(layout: QHBoxLayout) -> int:
    return sum(
        1 for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), QPushButton)
        and layout.itemAt(i).widget().property("validator_id")
    )
