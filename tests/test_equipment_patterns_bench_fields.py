"""Sync patterns are derived from pack-declared bench_fields (single source).

``ProjectManager.load_equipment_patterns`` now derives one constant-matching
pattern per declared bench field (``<EID>_<FIELD.upper()> = ...``) from the
bundle's ``bench_fields``, so identifying operator-editable bench constants no
longer depends on hand-written project-config regex. The config.json
patterns/override_suffix remain an optional override.

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_equipment_patterns_bench_fields.py --noconftest -q
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.core.project_manager import ProjectManager  # noqa: E402


class TestEquipmentPatternsFromBenchFields(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("TPG_BUNDLE_DEFAULTS_PATH", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["TPG_BUNDLE_DEFAULTS_PATH"] = self._saved

    def _pm_with_bench(self, bench: dict) -> ProjectManager:
        d = Path(tempfile.mkdtemp())
        (d / "bundle").mkdir()
        (d / "bundle" / "defaults.json").write_text(
            json.dumps({"pack_dispatch": {"bench_fields": bench}}), encoding="utf-8"
        )
        pm = ProjectManager()
        pm.project_root = d
        return pm

    def test_patterns_derived_from_bench_fields(self):
        pm = self._pm_with_bench({
            "psu": [{"name": "visa"}, {"name": "channel"}],
            "controller": [{"name": "manual_override"}],
        })
        pats = pm.load_equipment_patterns()
        matches = lambda s: any(p.search(s) for p in pats)
        self.assertTrue(matches("PSU1_VISA = 'TCPIP0::x::INSTR'\n"))
        self.assertTrue(matches("PSU1_CHANNEL = 2\n"))
        self.assertTrue(matches("FNCORE_DSC_MANUAL_OVERRIDE = True\n"))
        # a non-bench constant (a per-op arg / driver call) must NOT be matched
        self.assertFalse(matches("psu1.set_voltage(channel=1, volts=5.0)\n"))

    def test_no_bench_fields_no_config_returns_empty(self):
        d = Path(tempfile.mkdtemp())
        pm = ProjectManager()
        pm.project_root = d
        self.assertEqual(pm.load_equipment_patterns(), [])


if __name__ == "__main__":
    unittest.main()
