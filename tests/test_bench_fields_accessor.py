"""Unit tests for the bundle bench_fields accessor in pack_parsers.

The pack declares its config variables (rich bench_fields) in rules_index.json;
generate_all emits them into defaults.json:pack_dispatch.bench_fields. The GUI
reads them here to build the equipment-config editor and to identify which
generated constants are operator-editable bench config (replacing the project
regex).

Runs without PySide6:
    PYTHONPATH=. python3 -m pytest tests/test_bench_fields_accessor.py --noconftest -q
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from workflow_editor.llm import pack_parsers as pp  # noqa: E402


class TestBenchFieldsAccessor(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("TPG_BUNDLE_DEFAULTS_PATH")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TPG_BUNDLE_DEFAULTS_PATH", None)
        else:
            os.environ["TPG_BUNDLE_DEFAULTS_PATH"] = self._saved

    def _bundle(self, bench: dict) -> None:
        d = tempfile.mkdtemp()
        (Path(d) / "defaults.json").write_text(
            json.dumps({"pack_dispatch": {"bench_fields": bench}}), encoding="utf-8"
        )
        os.environ["TPG_BUNDLE_DEFAULTS_PATH"] = str(Path(d) / "defaults.json")

    def test_reads_bench_fields(self):
        self._bundle({"controller": [
            {"name": "baud", "type": "enum", "default": 115200, "choices": [9600, 115200]},
        ]})
        bf = pp.bench_fields()
        self.assertEqual(bf["controller"][0]["type"], "enum")
        self.assertEqual(bf["controller"][0]["choices"], [9600, 115200])

    def test_constant_names_match_codegen_naming(self):
        self._bundle({
            "psu": [{"name": "visa"}, {"name": "remote"}, {"name": "channel"}],
            "controller": [{"name": "port"}, {"name": "baud"}],
        })
        self.assertEqual(
            pp.bench_constant_names("psu", "PSU1"),
            ["PSU1_VISA", "PSU1_REMOTE", "PSU1_CHANNEL"],
        )
        self.assertEqual(
            pp.bench_constant_names("controller", "FNCORE_DSC"),
            ["FNCORE_DSC_PORT", "FNCORE_DSC_BAUD"],
        )

    def test_empty_when_no_bundle(self):
        os.environ.pop("TPG_BUNDLE_DEFAULTS_PATH", None)
        self.assertEqual(pp.bench_fields(), {})
        self.assertEqual(pp.bench_constant_names("psu", "PSU1"), [])

    def test_empty_when_pre_benchfields_bundle(self):
        d = tempfile.mkdtemp()
        (Path(d) / "defaults.json").write_text(
            json.dumps({"pack_dispatch": {"capabilities": {}}}), encoding="utf-8"
        )
        os.environ["TPG_BUNDLE_DEFAULTS_PATH"] = str(Path(d) / "defaults.json")
        self.assertEqual(pp.bench_fields(), {})


if __name__ == "__main__":
    unittest.main()
