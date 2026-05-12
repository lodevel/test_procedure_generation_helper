"""Tests for the Phase-3 pack workflow-defaults discovery.

Covers the manifest-path resolution chain in ``TaskConfigManager``:

  drivers_registry.json
    -> pack entry's ``rules.source.path`` + ``rules.rules_index``
    -> rules_index.json's ``workflow_defaults`` field
    -> the pack's ``pack_workflow_defaults.json``

Also covers ``_merge_workflows`` and end-to-end aggregation via
``_load_pack_workflow_defaults``.

The submodule-vs-parent walk-up that ``_find_drivers_registry`` performs
is exercised in one "live" test against the real ``external/rules_packager``
layout; everything else uses a synthetic registry built under ``tmp_path``
and monkeypatched into the manager so the tests don't depend on the live
submodule state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow_editor.core.task_config import (
    TaskConfigManager,
    _merge_workflows,
)


# ---------------------------------------------------------------------------
# Live walk-up (uses the real submodule layout)
# ---------------------------------------------------------------------------


def test_find_drivers_registry_walks_up(tmp_path):
    """The walk-up locates the real ``external/rules_packager/drivers_registry.json``.

    This is a smoke test for the install-layout assumption; if it breaks
    in CI the submodule layout has changed and ``_find_drivers_registry``
    needs revisiting.
    """
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    registry = mgr._find_drivers_registry()
    assert registry is not None, "real drivers_registry.json should resolve"
    assert registry.name == "drivers_registry.json"
    assert registry.parent.name == "rules_packager"


def test_find_pack_workflow_defaults_for_base(tmp_path):
    """Real ``base`` pack resolves to its shipped ``pack_workflow_defaults.json``."""
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    defaults = mgr._find_pack_workflow_defaults("base")
    assert defaults is not None, "rules_packager_base should expose defaults"
    assert defaults.name == "pack_workflow_defaults.json"
    payload = json.loads(defaults.read_text(encoding="utf-8"))
    assert "workflows" in payload, "shipped defaults must declare a workflows block"


def test_find_pack_workflow_defaults_unknown_pack(tmp_path):
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._find_pack_workflow_defaults("does_not_exist_xyz") is None


# ---------------------------------------------------------------------------
# Synthetic-registry helpers
# ---------------------------------------------------------------------------


def _build_pack(
    pack_root: Path,
    rules_index_rel: str,
    workflow_defaults_rel: str | None,
    defaults_payload: dict | None,
) -> None:
    """Materialise a fake pack under ``pack_root``:

    * Writes a ``rules_index.json`` at ``pack_root/rules_index_rel``.
    * If ``workflow_defaults_rel`` is provided, embeds it as the
      ``workflow_defaults`` field of the index and writes the JSON at
      ``rules_index.parent / workflow_defaults_rel``.
    """
    rules_index_path = pack_root / rules_index_rel
    rules_index_path.parent.mkdir(parents=True, exist_ok=True)

    idx: dict = {"rules_version": "0.0.0", "files": []}
    if workflow_defaults_rel is not None:
        idx["workflow_defaults"] = workflow_defaults_rel
        if defaults_payload is not None:
            defaults_path = (rules_index_path.parent / workflow_defaults_rel).resolve()
            defaults_path.parent.mkdir(parents=True, exist_ok=True)
            defaults_path.write_text(json.dumps(defaults_payload), encoding="utf-8")
    rules_index_path.write_text(json.dumps(idx), encoding="utf-8")


def _write_registry(registry_path: Path, packs: list[dict]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({"packs": packs}), encoding="utf-8")


def _seed_project(project_root: Path, selected_packs: list[str]) -> None:
    cfg = project_root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text(
        json.dumps({"packs": {"selected_packs": selected_packs}}),
        encoding="utf-8",
    )


def _stub_registry_path(monkeypatch, mgr: TaskConfigManager, path: Path) -> None:
    """Force ``_find_drivers_registry`` to return ``path``."""
    monkeypatch.setattr(mgr, "_find_drivers_registry", lambda: path)


# ---------------------------------------------------------------------------
# Synthetic registry: end-to-end manifest chain
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_registry(tmp_path):
    """Build a synthetic two-pack registry under ``tmp_path``.

    Pack ``alpha``: defaults declare text_json with one validator.
    Pack ``beta``: defaults declare text_json (overlapping) AND json_code.

    Returns ``(registry_path, project_root)``.
    """
    registry_path = tmp_path / "registry" / "drivers_registry.json"

    alpha_root = tmp_path / "packs" / "alpha"
    _build_pack(
        alpha_root,
        rules_index_rel="rules/manifest.json",
        workflow_defaults_rel="../pack_workflow_defaults.json",
        defaults_payload={
            "workflows": {
                "text_json": {
                    "validators": [
                        {"id": "alpha.validate_one", "enabled": True}
                    ]
                }
            }
        },
    )

    beta_root = tmp_path / "packs" / "beta"
    _build_pack(
        beta_root,
        rules_index_rel="rules/v1/manifest.json",
        workflow_defaults_rel="../../pack_workflow_defaults.json",
        defaults_payload={
            "workflows": {
                "text_json": {
                    "validators": [
                        {"id": "beta.validate_two", "enabled": True}
                    ]
                },
                "json_code": {
                    "validators": [
                        {"id": "beta.validate_three", "enabled": True}
                    ]
                },
            }
        },
    )

    _write_registry(
        registry_path,
        packs=[
            {
                "id": "alpha",
                "rules": {
                    "source": {"type": "path", "path": "../packs/alpha"},
                    "rules_index": "rules/manifest.json",
                },
            },
            {
                "id": "beta",
                "rules": {
                    "source": {"type": "path", "path": "../packs/beta"},
                    "rules_index": "rules/v1/manifest.json",
                },
            },
        ],
    )

    project_root = tmp_path / "project"
    return registry_path, project_root


def test_resolve_via_synthetic_registry(monkeypatch, tmp_path, fake_registry):
    registry_path, project_root = fake_registry
    _seed_project(project_root, ["alpha"])

    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)

    resolved = mgr._find_pack_workflow_defaults("alpha")
    assert resolved is not None
    assert resolved.name == "pack_workflow_defaults.json"
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    assert payload["workflows"]["text_json"]["validators"][0]["id"] == "alpha.validate_one"


def test_pack_without_manifest_entry_returns_none(monkeypatch, tmp_path, fake_registry):
    registry_path, project_root = fake_registry
    _seed_project(project_root, ["never_added"])
    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)

    assert mgr._find_pack_workflow_defaults("never_added") is None


def test_pack_without_workflow_defaults_field_returns_none(monkeypatch, tmp_path):
    """Pack ships a rules_index.json with NO ``workflow_defaults`` key — skip."""
    registry_path = tmp_path / "registry" / "drivers_registry.json"
    pack_root = tmp_path / "packs" / "labscpi_like"
    _build_pack(
        pack_root,
        rules_index_rel="rules/idx.json",
        workflow_defaults_rel=None,  # no field at all
        defaults_payload=None,
    )
    _write_registry(
        registry_path,
        packs=[{
            "id": "labscpi_like",
            "rules": {
                "source": {"type": "path", "path": "../packs/labscpi_like"},
                "rules_index": "rules/idx.json",
            },
        }],
    )

    project_root = tmp_path / "project"
    _seed_project(project_root, ["labscpi_like"])
    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)
    assert mgr._find_pack_workflow_defaults("labscpi_like") is None


def test_workflow_defaults_pointer_missing_file_returns_none(monkeypatch, tmp_path):
    """rules_index.json points at a defaults file that doesn't exist on disk."""
    registry_path = tmp_path / "registry" / "drivers_registry.json"
    pack_root = tmp_path / "packs" / "bogus"
    _build_pack(
        pack_root,
        rules_index_rel="rules/idx.json",
        workflow_defaults_rel="../no_such_file.json",
        defaults_payload=None,  # don't write the file
    )
    _write_registry(
        registry_path,
        packs=[{
            "id": "bogus",
            "rules": {
                "source": {"type": "path", "path": "../packs/bogus"},
                "rules_index": "rules/idx.json",
            },
        }],
    )

    project_root = tmp_path / "project"
    _seed_project(project_root, ["bogus"])
    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)
    assert mgr._find_pack_workflow_defaults("bogus") is None


def test_drivers_registry_search_cached(tmp_path):
    """``_find_drivers_registry`` caches its result; the second call does
    not re-walk. We assert the cache field is populated after the first
    call."""
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._drivers_registry_cached is None
    _ = mgr._find_drivers_registry()
    assert mgr._drivers_registry_cached is not None
    assert mgr._drivers_registry_cached is not False  # found
    # Second call returns the cached value.
    cached = mgr._drivers_registry_cached
    assert mgr._find_drivers_registry() is cached


def test_drivers_registry_not_found_caches_false_sentinel(tmp_path, monkeypatch):
    """When walk-up fails, the False sentinel is cached so we don't re-walk."""
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    # Force a cwd-walk that never finds the registry by patching __file__
    # walk anchor onto a tmp_path subtree.
    import workflow_editor.core.task_config as mod

    orig_path = Path(mod.__file__)
    fake_module_file = tmp_path / "nowhere" / "task_config.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    fake_module_file.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(mod, "__file__", str(fake_module_file))

    mgr2 = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    result = mgr2._find_drivers_registry()
    assert result is None
    assert mgr2._drivers_registry_cached is False, "False sentinel cached"

    # Sanity: original module path is still resolvable (no global mutation
    # leaked beyond this test's monkeypatch).
    assert orig_path.name == "task_config.py"


# ---------------------------------------------------------------------------
# Aggregation: ``_load_pack_workflow_defaults``
# ---------------------------------------------------------------------------


def test_aggregate_single_pack(monkeypatch, tmp_path, fake_registry):
    registry_path, project_root = fake_registry
    _seed_project(project_root, ["alpha"])

    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)
    aggregate = mgr._load_pack_workflow_defaults()
    assert aggregate == {
        "text_json": {
            "validators": [{"id": "alpha.validate_one", "enabled": True}],
        },
    }


def test_aggregate_alphabetical_first_wins(monkeypatch, tmp_path, fake_registry):
    """When two packs declare the same tab key, alphabetical order wins
    per-key. ``alpha`` < ``beta``, so ``text_json.validators`` keeps
    alpha's list, while ``json_code`` (only in beta) comes from beta."""
    registry_path, project_root = fake_registry
    _seed_project(project_root, ["beta", "alpha"])  # order irrelevant

    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)
    aggregate = mgr._load_pack_workflow_defaults()

    assert aggregate["text_json"]["validators"] == [
        {"id": "alpha.validate_one", "enabled": True}
    ], "alpha's validators should win the text_json conflict"
    assert aggregate["json_code"]["validators"] == [
        {"id": "beta.validate_three", "enabled": True}
    ]


def test_aggregate_skips_pack_without_defaults(monkeypatch, tmp_path):
    """A selected pack with no ``workflow_defaults`` field contributes nothing."""
    registry_path = tmp_path / "registry" / "drivers_registry.json"
    pack_root = tmp_path / "packs" / "silent"
    _build_pack(
        pack_root,
        rules_index_rel="rules/idx.json",
        workflow_defaults_rel=None,
        defaults_payload=None,
    )
    _write_registry(registry_path, packs=[{
        "id": "silent",
        "rules": {
            "source": {"type": "path", "path": "../packs/silent"},
            "rules_index": "rules/idx.json",
        },
    }])

    project_root = tmp_path / "project"
    _seed_project(project_root, ["silent"])
    mgr = TaskConfigManager(
        fallback_path=tmp_path / "no_fallback.json",
        project_root=project_root,
    )
    _stub_registry_path(monkeypatch, mgr, registry_path)
    assert mgr._load_pack_workflow_defaults() == {}


# ---------------------------------------------------------------------------
# _merge_workflows
# ---------------------------------------------------------------------------


def test_merge_workflows_overlay_overrides_per_key():
    base = {"text_json": {"a": 1, "b": 2}}
    overlay = {"text_json": {"a": 99}}
    merged = _merge_workflows(base, overlay)
    assert merged == {"text_json": {"a": 99, "b": 2}}


def test_merge_workflows_new_tab_from_overlay():
    base = {"text_json": {"a": 1}}
    overlay = {"json_code": {"x": 7}}
    merged = _merge_workflows(base, overlay)
    assert merged == {"text_json": {"a": 1}, "json_code": {"x": 7}}


def test_merge_workflows_empty_base():
    assert _merge_workflows({}, {"text_json": {"a": 1}}) == {"text_json": {"a": 1}}


def test_merge_workflows_empty_overlay():
    assert _merge_workflows({"text_json": {"a": 1}}, {}) == {"text_json": {"a": 1}}


def test_merge_workflows_non_dict_tab_value_tolerated():
    """Defensive: a malformed tab payload (non-dict) shouldn't crash the
    merge — it's treated as an empty dict so the other side wins."""
    base = {"text_json": "not-a-dict"}
    overlay = {"text_json": {"a": 1}}
    assert _merge_workflows(base, overlay) == {"text_json": {"a": 1}}


# ---------------------------------------------------------------------------
# Phase 4a: bundle-defaults hand-off via TPG_BUNDLE_DEFAULTS_PATH
# ---------------------------------------------------------------------------


def test_bundle_env_var_short_circuits_manifest_walk(monkeypatch, tmp_path):
    """When the parent app sets TPG_BUNDLE_DEFAULTS_PATH, the editor's
    pack-defaults loader reads from that file and skips the legacy
    manifest walk. Verifies the Phase 4a hand-off contract."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({
        "workflows": {
            "text_json": {"validators": [{"id": "bundle.v"}]},
        },
        # parsers/extractors are also in the bundle's defaults but
        # the editor only reads the workflows block.
        "extractors": {"equipment": {"module": "m"}},
    }), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(defaults_path))

    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    result = mgr._load_pack_workflow_defaults()
    assert result == {"text_json": {"validators": [{"id": "bundle.v"}]}}


def test_bundle_env_var_unset_falls_through_to_manifest_walk(monkeypatch,
                                                              tmp_path):
    """No env var → behaviour unchanged from before Phase 4a (manifest
    walk path). The legacy projects with no bundle: ref still work."""
    monkeypatch.delenv("TPG_BUNDLE_DEFAULTS_PATH", raising=False)
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    # No registered packs, no env var → empty dict (not a crash).
    assert mgr._load_pack_workflow_defaults() == {}


def test_bundle_env_var_pointing_at_missing_file_falls_through(monkeypatch,
                                                                 tmp_path):
    """Defensive: a stale env var pointing at a deleted file must not
    crash the editor — fall through to the manifest walk."""
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(tmp_path / "gone.json"))
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}


def test_bundle_env_var_pointing_at_malformed_file_falls_through(monkeypatch,
                                                                   tmp_path):
    """A defaults.json with wrong shape (no workflows block, or whole
    thing isn't an object) falls through gracefully."""
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(bad))
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}


def test_bundle_env_var_with_no_workflows_block_returns_empty(monkeypatch,
                                                                tmp_path):
    """defaults.json present but workflows: missing → return empty
    dict (not None and not fall through). The bundle's contract is
    that it ships every section; an absent workflows block is a real
    'no workflow defaults' state, not 'use the legacy walk'."""
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps({"extractors": {}}), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(defaults_path))
    mgr = TaskConfigManager(fallback_path=tmp_path / "no_fallback.json")
    assert mgr._load_pack_workflow_defaults() == {}
