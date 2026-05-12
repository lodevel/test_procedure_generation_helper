"""Phase 2/3 confirmation: pack defaults + project validator overrides
on a project seeded from ``customer_configs/FN_hardware``.

Extends ``test_phase1_confirmation_fn_hardware.py`` which covered the
prompt/button-label half of the lift. This module covers the half that
Phase 2/3 introduced:

1. A project carrying FN_hardware's packs (``base`` is the only one
   shipping ``pack_workflow_defaults.json`` today) and NO ``workflows``
   section in its ``config.json`` inherits the base pack's validator
   defaults via the manifest-path chain.

2. Adding a ``workflows.text_json.validators`` block to the project's
   ``config.json`` fully replaces the pack-provided list for that tab
   only — other tabs keep the pack defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_editor.core.task_config import TaskConfigManager


# Repo-relative anchors (same as the Phase 1 confirmation module).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FALLBACK_PATH = (
    _REPO_ROOT / "external" / "test_procedure_generation_helper"
    / "config" / "tab_contexts.json"
)
_FN_HARDWARE = _REPO_ROOT / "customer_configs" / "FN_hardware"
_BASE_DEFAULTS = (
    _REPO_ROOT / "external" / "rules_packager" / "src" / "rules_packager_base"
    / "pack_workflow_defaults.json"
)


@pytest.fixture
def fn_hardware_project(tmp_path, monkeypatch):
    """Seed a project carrying FN_hardware's manifest + a pinned pack
    set, exercising the legacy manifest-walk path.

    Phase 4c migrated the on-disk FN_hardware template from
    ``packs.selected_packs`` to ``bundle:``. These Phase 2/3
    confirmation tests pin the **manifest-walk** behavior — they
    pre-date bundles. Rather than chase FN_hardware's current shape,
    pin the legacy pack list explicitly so this regression suite
    stays meaningful regardless of the template's migration state.

    Also clears ``TPG_BUNDLE_DEFAULTS_PATH`` in case the parent app
    set it in the environment — the test exercises the fallback path.
    """
    monkeypatch.delenv("TPG_BUNDLE_DEFAULTS_PATH", raising=False)
    root = tmp_path / "fn_proj"
    cfg = root / "config"
    cfg.mkdir(parents=True)

    fn_config = json.loads((_FN_HARDWARE / "config.json").read_text(encoding="utf-8"))
    project_config = {
        "manifest": fn_config.get("manifest", {}),
        # Hard-pin the legacy pack list. FN_hardware shipped with
        # base + fncore-mockup-driver + labscpi pre-bundle; only base
        # has pack_workflow_defaults.json today.
        "packs": {
            "selected_packs": [
                "base", "fncore-mockup-driver", "labscpi",
            ],
        },
    }
    (cfg / "config.json").write_text(
        json.dumps(project_config, indent=2), encoding="utf-8"
    )
    return root


def _base_pack_validators_for_tab(tab_id: str) -> list[dict]:
    payload = json.loads(_BASE_DEFAULTS.read_text(encoding="utf-8"))
    return payload.get("workflows", {}).get(tab_id, {}).get("validators", [])


def test_pack_defaults_surface_through_get_validator_specs(fn_hardware_project):
    """With no ``workflows`` key in the project, ``get_validator_specs_for_tab``
    surfaces the validators list that rules_packager_base ships in its
    ``pack_workflow_defaults.json``."""
    mgr = TaskConfigManager(_FALLBACK_PATH, project_root=fn_hardware_project)

    for tab_id in ("text_only", "text_json", "json_code"):
        expected = _base_pack_validators_for_tab(tab_id)
        if not expected:
            continue  # tab not declared in the pack defaults
        got = mgr.get_validator_specs_for_tab(tab_id)
        assert got == expected, (
            f"tab {tab_id}: expected pack defaults {expected}, got {got}"
        )


def test_project_validators_merge_by_id_with_pack(fn_hardware_project):
    """Phase 4.2 deepens the merge to per-id within tasks/validators.
    Project entries override matching ids; pack entries with ids not
    in the project's list are still surfaced. Lets users override one
    validator's enabled flag without dropping the pack's other validators.
    """
    cfg_path = fn_hardware_project / "config" / "config.json"
    full = json.loads(cfg_path.read_text(encoding="utf-8"))
    project_validators = [
        {"id": "rules_packager_base.validate_json_schema", "enabled": True},
        {"id": "core.check_python_syntax",                 "enabled": False},
    ]
    full["workflows"] = {"text_json": {"validators": project_validators}}
    cfg_path.write_text(json.dumps(full, indent=2), encoding="utf-8")

    mgr = TaskConfigManager(_FALLBACK_PATH, project_root=fn_hardware_project)

    # Overridden tab: project list FIRST, then pack ids not in project.
    pack_for_text_json = _base_pack_validators_for_tab("text_json")
    project_ids = {v["id"] for v in project_validators}
    pack_tail = [v for v in pack_for_text_json if v["id"] not in project_ids]
    expected = project_validators + pack_tail
    assert mgr.get_validator_specs_for_tab("text_json") == expected

    # Non-overridden tabs: pack defaults preserved.
    for tab_id in ("text_only", "json_code"):
        expected_full = _base_pack_validators_for_tab(tab_id)
        if expected_full:
            assert mgr.get_validator_specs_for_tab(tab_id) == expected_full
