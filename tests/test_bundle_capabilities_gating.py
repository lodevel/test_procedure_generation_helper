"""can_for_types: a bundle without a capabilities block provides no pack (no pre-capability fallback)."""
from __future__ import annotations

import json

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.core.studio_launch_env import BUNDLE_DEFAULTS_PATH  # noqa: E402
from procedo.studio.pack_ops import bundle_capabilities  # noqa: E402


def _bundle(tmp_path, monkeypatch, payload):
    path = tmp_path / "defaults.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(path))


def test_a_bundle_without_capabilities_provides_no_pack(tmp_path, monkeypatch):
    _bundle(tmp_path, monkeypatch, {"equipment_profiles": []})
    ok, missing = bundle_capabilities.can_for_types("parse", ["psu", "dmm"])
    assert not ok
    assert missing == [("psu", "no pack")]  # dmm is base-native


def test_declared_capabilities_still_gate(tmp_path, monkeypatch):
    caps = {"psu": {"parse": True, "emit": False, "pack": "labscpi"}}
    _bundle(tmp_path, monkeypatch, {"pack_dispatch": {"capabilities": caps}})
    assert bundle_capabilities.can_for_types("parse", ["psu"]) == (True, [])
    assert bundle_capabilities.can_for_types("emit", ["psu"]) == (False, [("psu", "labscpi")])
