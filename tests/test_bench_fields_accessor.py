"""Studio's editor mode reads the bundle's bench_fields through the host's one
defaults reader (``procedo.core.bundle_registry.bench_fields``) at the path
``bundle_capabilities.defaults_path`` resolves: ``PROCEDO_BUNDLE_DEFAULTS_PATH``
when no project is open.

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

from procedo.core import bundle_registry  # noqa: E402
from procedo.studio.pack_ops import bundle_capabilities  # noqa: E402
from procedo.studio.launch_env import BUNDLE_DEFAULTS_PATH  # noqa: E402


class TestBenchFieldsAccessor(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(BUNDLE_DEFAULTS_PATH)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(BUNDLE_DEFAULTS_PATH, None)
        else:
            os.environ[BUNDLE_DEFAULTS_PATH] = self._saved

    def test_editor_mode_reads_the_env_bundle(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        (Path(d) / "defaults.json").write_text(json.dumps({"pack_dispatch": {"bench_fields": {
            "controller": [{"name": "baud", "type": "enum", "default": 115200, "choices": [9600, 115200]}],
        }}}), encoding="utf-8")
        os.environ[BUNDLE_DEFAULTS_PATH] = str(Path(d) / "defaults.json")
        bf = bundle_registry.bench_fields(bundle_capabilities.defaults_path(None))
        self.assertEqual(bf["controller"][0]["choices"], [9600, 115200])

    def test_empty_when_no_bundle(self):
        os.environ.pop(BUNDLE_DEFAULTS_PATH, None)
        self.assertIsNone(bundle_capabilities.defaults_path(None))
        self.assertEqual(bundle_registry.bench_fields(None), {})


if __name__ == "__main__":
    unittest.main()
