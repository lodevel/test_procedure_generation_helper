"""Tests for the bundle ``equipment_profiles`` bridge in
``workflow_editor.llm.pack_parsers`` (Phase B, D4 hard cut).

Covers ``_load_bundle_equipment_profiles`` (reads the bundle's
defaults.json via ``TPG_BUNDLE_DEFAULTS_PATH``) and the
``sync_equipment_from_steps`` fallback that filters the records to
``equipment_type == "controller"`` and shims them to the wheel shape
(``pattern`` <- ``id_pattern``).

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_pack_parsers_equipment_profiles.py --noconftest -q
"""
from __future__ import annotations

import json

import pytest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers as pp  # noqa: E402


CONTROLLER_PROFILE = {
    "equipment_type": "controller",
    "subtype": "fncore-mockup",
    "display_name": "FNCORE Mockup",
    "category": "Controllers",
    "id_pattern": "^fncore([-_0-9].*)?$",
    "aliases": ["fncore"],
    "session_policy_default": "per_step",
    "remote": {"transport": "serial", "enable_field": "manual_override",
               "inverted": True},
    "override_suffix": "_MANUAL_OVERRIDE",
    "defaults": {},
}

PSU_PROFILE = {
    "equipment_type": "psu",
    "subtype": "",
    "display_name": "Power Supply",
    "category": "Power Supplies",
    "id_pattern": "^psu([-_0-9].*)?$",
    "aliases": [],
    "session_policy_default": "per_session",
    "remote": {"transport": "visa", "enable_field": "remote",
               "inverted": False},
    "override_suffix": None,
    "defaults": {},
}


def _write_defaults(tmp_path, payload, monkeypatch):
    defaults_path = tmp_path / "defaults.json"
    defaults_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(defaults_path))
    return defaults_path


# ---------------------------------------------------------------------------
# _load_bundle_equipment_profiles
# ---------------------------------------------------------------------------


def test_new_key_loads_all_records(monkeypatch, tmp_path):
    """``equipment_profiles`` is read unfiltered — the loader returns every
    record (controller filtering is the sync consumer's job)."""
    _write_defaults(
        tmp_path,
        {"equipment_profiles": [CONTROLLER_PROFILE, PSU_PROFILE]},
        monkeypatch,
    )
    assert pp._load_bundle_equipment_profiles() == [
        CONTROLLER_PROFILE, PSU_PROFILE,
    ]


def test_non_dict_entries_are_dropped(monkeypatch, tmp_path):
    _write_defaults(
        tmp_path,
        {"equipment_profiles": ["junk", 7, CONTROLLER_PROFILE, None]},
        monkeypatch,
    )
    assert pp._load_bundle_equipment_profiles() == [CONTROLLER_PROFILE]


def test_env_unset_returns_empty(monkeypatch):
    """No bundle context (env unset) is not an error — degrade to []."""
    monkeypatch.delenv("TPG_BUNDLE_DEFAULTS_PATH", raising=False)
    assert pp._load_bundle_equipment_profiles() == []


def test_missing_file_returns_empty(monkeypatch, tmp_path):
    """A stale env var pointing at a deleted file degrades to []."""
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(tmp_path / "gone.json"))
    assert pp._load_bundle_equipment_profiles() == []


def test_malformed_json_returns_empty(monkeypatch, tmp_path):
    bad = tmp_path / "defaults.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(bad))
    assert pp._load_bundle_equipment_profiles() == []


def test_old_key_only_bundle_raises_parser_unavailable(monkeypatch, tmp_path):
    """D4 hard cut: a readable defaults.json carrying only the legacy
    ``controller_profiles`` key (bundle/1) must raise loudly — never a
    silent [] that quietly kills controller inference."""
    _write_defaults(
        tmp_path,
        {"controller_profiles": [CONTROLLER_PROFILE]},
        monkeypatch,
    )
    with pytest.raises(pp.ParserUnavailable, match="equipment_profiles"):
        pp._load_bundle_equipment_profiles()


def test_key_missing_entirely_raises_parser_unavailable(monkeypatch, tmp_path):
    _write_defaults(tmp_path, {"workflows": {}}, monkeypatch)
    with pytest.raises(pp.ParserUnavailable):
        pp._load_bundle_equipment_profiles()


def test_non_list_value_raises_parser_unavailable(monkeypatch, tmp_path):
    _write_defaults(
        tmp_path, {"equipment_profiles": {"oops": "dict"}}, monkeypatch,
    )
    with pytest.raises(pp.ParserUnavailable):
        pp._load_bundle_equipment_profiles()


# ---------------------------------------------------------------------------
# sync_equipment_from_steps fallback: controller filter + pattern shim
# ---------------------------------------------------------------------------


def _capture_inproc_sync(monkeypatch):
    captured = {}

    def fake_sync(text, controller_profiles):
        captured["profiles"] = controller_profiles
        return text, []

    monkeypatch.setattr(pp, "_inproc_sync_equipment", fake_sync)
    return captured


def test_sync_filters_controllers_and_shims_pattern(monkeypatch, tmp_path):
    """Fallback load keeps only controller records and stamps the wheel's
    ``pattern`` field from ``id_pattern`` — without the shim the wheel's
    ``p["pattern"]`` match never fires and subtype inference dies."""
    _write_defaults(
        tmp_path,
        {"equipment_profiles": [PSU_PROFILE, CONTROLLER_PROFILE]},
        monkeypatch,
    )
    captured = _capture_inproc_sync(monkeypatch)

    pp.sync_equipment_from_steps("## Steps\n", project_root=None)

    profiles = captured["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["subtype"] == "fncore-mockup"
    assert profiles[0]["pattern"] == CONTROLLER_PROFILE["id_pattern"]
    # Original record fields ride along untouched.
    assert profiles[0]["id_pattern"] == CONTROLLER_PROFILE["id_pattern"]
    assert profiles[0]["remote"]["inverted"] is True


def test_sync_env_unset_passes_empty_profiles(monkeypatch):
    monkeypatch.delenv("TPG_BUNDLE_DEFAULTS_PATH", raising=False)
    captured = _capture_inproc_sync(monkeypatch)

    pp.sync_equipment_from_steps("## Steps\n", project_root=None)

    assert captured["profiles"] == []


def test_sync_old_key_only_bundle_raises(monkeypatch, tmp_path):
    """D4 surfaces through the public entry point."""
    _write_defaults(
        tmp_path,
        {"controller_profiles": [CONTROLLER_PROFILE]},
        monkeypatch,
    )
    _capture_inproc_sync(monkeypatch)

    with pytest.raises(pp.ParserUnavailable, match="rebuild the bundle"):
        pp.sync_equipment_from_steps("## Steps\n", project_root=None)


def test_sync_explicit_profiles_bypass_bundle_load(monkeypatch, tmp_path):
    """An explicit ``controller_profiles`` argument wins: the bundle is
    never consulted, so even an old-format bundle cannot raise."""
    _write_defaults(
        tmp_path,
        {"controller_profiles": [CONTROLLER_PROFILE]},  # old-format bundle
        monkeypatch,
    )
    captured = _capture_inproc_sync(monkeypatch)
    explicit = [{"subtype": "x", "pattern": "^x$"}]

    pp.sync_equipment_from_steps(
        "## Steps\n", project_root=None, controller_profiles=explicit,
    )

    assert captured["profiles"] == explicit
