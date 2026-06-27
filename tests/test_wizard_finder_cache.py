"""Tests for the DCDC finder IC-list cache (wizard/finder_cache.py)."""
from __future__ import annotations

from workflow_editor.authoring.wizard import finder_cache
from workflow_editor.authoring.wizard.list_parse import IcRow

ROWS = [
    IcRow("U5", "RBBA3000-50", "DC-DC", "+CAP_30V"),
    IcRow("U11.1", "LMZM33604RLXR", "DC-DC", "+AUX0_16V"),
]


def _tgz(tmp_path, content=b"board"):
    p = tmp_path / "board.tgz"
    p.write_bytes(content)
    return p


def test_save_then_load_roundtrip(tmp_path):
    _tgz(tmp_path)
    assert finder_cache.load(tmp_path) is None          # nothing cached yet
    finder_cache.save(tmp_path, ROWS)
    assert finder_cache.load(tmp_path) == ROWS          # IcRow dataclass equality


def test_cache_invalidates_when_board_archive_changes(tmp_path):
    _tgz(tmp_path, content=b"first")
    finder_cache.save(tmp_path, ROWS)
    assert finder_cache.load(tmp_path) == ROWS
    _tgz(tmp_path, content=b"second-and-bigger-archive")  # board replaced -> stale
    assert finder_cache.load(tmp_path) is None


def test_clear_removes_cache(tmp_path):
    _tgz(tmp_path)
    finder_cache.save(tmp_path, ROWS)
    finder_cache.clear(tmp_path)
    assert finder_cache.load(tmp_path) is None
    finder_cache.clear(tmp_path)                         # idempotent, no crash


def test_no_root_or_no_archive_is_safe(tmp_path):
    assert finder_cache.load(None) is None
    finder_cache.save(None, ROWS)                        # no crash
    finder_cache.clear(None)                             # no crash
    # no .tgz -> empty signature, but save+load still round-trips (sig "" == "")
    finder_cache.save(tmp_path, ROWS)
    assert finder_cache.load(tmp_path) == ROWS
