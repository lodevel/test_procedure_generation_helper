"""Unit tests for the DC-DC wizard's test materializer.

Drives :func:`materialize_test` against a tmp ``tests/`` directory. The parser
(text->json) is pack-dispatched and not available headlessly, so the json path
is exercised deterministically by monkeypatching ``pack_parsers``; the
filesystem behaviour (folder create, sanitize, disambiguate, text-only, no
empty json) is asserted directly.

Runs headless (PySide6 is present in the editor venv):
    PYTHONPATH=. python -m pytest tests/test_test_materializer.py -q
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflow_editor.authoring.test_materializer import (
    MaterializeResult,
    _make_test_id,
    materialize_test,
    sanitize_test_name,
)
from workflow_editor.llm import pack_parsers


# The body the dcdc_authoring skill emits (no title, no ## Meta).
DCDC_BLOCK = """## Equipment
PSU1 : psu channels=[{1, max_voltage=28.0 V, max_current=10.0 A}]

## Steps
1. Set PSU1 CH1 voltage = 28.0 V.
2. Set PSU1 CH1 output = ON.

## Expected
{1} = 5.0 V +/- 3.0 %
"""

PROC_TEXT = "procedure_text.md"
PROC_JSON = "procedure.json"


@pytest.fixture
def tests_dir(tmp_path: Path) -> Path:
    """A ``<project>/tests`` directory inside a tmp project root."""
    d = tmp_path / "proj" / "tests"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def no_parser(monkeypatch: pytest.MonkeyPatch):
    """Force the headless 'parser unavailable' world: no meta synth, parse raises.

    Mirrors the real headless editor venv (no project bundle -> psu unknown),
    so materialize_test takes its clean text-only fallback deterministically
    without spawning a project venv subprocess.
    """
    monkeypatch.setattr(pack_parsers, "supports_sync_meta", lambda *a, **k: False)

    def _raise(*_a, **_k):
        raise pack_parsers.ParserUnavailable("no bundle in headless test")

    monkeypatch.setattr(pack_parsers, "parse_text", _raise)
    return monkeypatch


# ---------------------------------------------------------------------------
# Name / id sanitization (pure)
# ---------------------------------------------------------------------------


def test_sanitize_keeps_plus_dash_underscore_space():
    # '+', '-', '_' and spaces are all legal in a folder name.
    assert sanitize_test_name("PSU - +MAIN_5V0") == "PSU - +MAIN_5V0"


def test_sanitize_scrubs_illegal_chars():
    # ':' '/' '?' are Windows-reserved -> replaced with '_'.
    assert sanitize_test_name('PSU: 3V3/1V8?') == "PSU_ 3V3_1V8_"


def test_sanitize_empty_degrades_to_test():
    assert sanitize_test_name("   ") == "test"
    assert sanitize_test_name("..") == "test"


def test_make_test_id_is_grammar_valid():
    tid = _make_test_id("PSU - +MAIN_5V0")
    assert tid == "PSU_-_MAIN_5V0"
    # the parser's grammar: starts with a letter, [A-Za-z0-9_-]*, <= 64.
    import re

    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", tid)
    assert len(tid) <= 64


def test_make_test_id_leading_nonalpha_and_empty():
    assert _make_test_id("3V3 rail")[0].isalpha()  # leading digit dropped
    assert _make_test_id("///") == "Test"          # nothing valid -> default


# ---------------------------------------------------------------------------
# Filesystem behaviour (text-only world)
# ---------------------------------------------------------------------------


def test_creates_folder_and_writes_text_only(tests_dir: Path, no_parser):
    res = materialize_test(tests_dir, "PSU - +MAIN_5V0", DCDC_BLOCK)

    assert isinstance(res, MaterializeResult)
    assert res.created is True
    assert res.json_written is False
    assert res.path == tests_dir / "PSU - +MAIN_5V0"
    assert res.path.is_dir()

    text_file = res.path / PROC_TEXT
    assert text_file.exists()
    # body preserved verbatim inside the written file
    assert "PSU1 : psu channels=" in text_file.read_text(encoding="utf-8")


def test_no_empty_json_when_parser_unavailable(tests_dir: Path, no_parser):
    res = materialize_test(tests_dir, "+MAIN_5V0 rail", DCDC_BLOCK)
    assert res.json_written is False
    # The critical assertion: NO empty/placeholder procedure.json is left behind
    # (an empty json makes the main GUI render the test 'visible-but-empty').
    assert not (res.path / PROC_JSON).exists()


def test_illegal_chars_in_name_are_sanitized_on_disk(tests_dir: Path, no_parser):
    res = materialize_test(tests_dir, 'PSU: 3V3/1V8?', DCDC_BLOCK)
    assert res.created is True
    assert res.path.name == "PSU_ 3V3_1V8_"
    assert res.path.is_dir()
    # no stray folder created from the raw (slash-bearing) name
    assert not (tests_dir / "PSU: 3V3").exists()


def test_collision_disambiguates(tests_dir: Path, no_parser):
    first = materialize_test(tests_dir, "PSU MAIN", DCDC_BLOCK)
    assert first.path.name == "PSU MAIN"

    second = materialize_test(tests_dir, "PSU MAIN", DCDC_BLOCK)
    assert second.created is True
    assert second.path.name == "PSU MAIN (2)"
    assert second.path != first.path

    third = materialize_test(tests_dir, "PSU MAIN", DCDC_BLOCK)
    assert third.path.name == "PSU MAIN (3)"


def test_missing_tests_dir_returns_failure(no_parser):
    # A ProjectManager-like handle whose tests/ dir does not exist.
    handle = SimpleNamespace(
        get_tests_dir=lambda: None,
        create_test_folder=lambda name: None,
        project_root=None,
    )
    res = materialize_test(handle, "X", DCDC_BLOCK)
    assert res.created is False
    assert res.path is None


# ---------------------------------------------------------------------------
# ProjectManager-handle path (production seam) is honoured
# ---------------------------------------------------------------------------


def test_uses_handle_create_test_folder(tests_dir: Path, no_parser):
    """When given a ProjectManager-like handle, its create_test_folder is used."""
    calls: list[str] = []

    def _create(name: str):
        calls.append(name)
        folder = tests_dir / name
        folder.mkdir()
        return folder

    handle = SimpleNamespace(
        get_tests_dir=lambda: tests_dir,
        create_test_folder=_create,
        project_root=tests_dir.parent,
    )
    res = materialize_test(handle, "RAIL_A", DCDC_BLOCK)
    assert res.created is True
    assert calls == ["RAIL_A"]
    assert (res.path / PROC_TEXT).exists()


# ---------------------------------------------------------------------------
# JSON path (deterministic via monkeypatch) — proves the success branch
# ---------------------------------------------------------------------------


def test_writes_real_json_when_parse_succeeds(
    tests_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(pack_parsers, "supports_sync_meta", lambda *a, **k: False)
    fake_json = {"test_id": "RAIL_5V0", "steps": [{"text": "x"}]}
    monkeypatch.setattr(
        pack_parsers, "parse_text", lambda *_a, **_k: (fake_json, [])
    )

    res = materialize_test(tests_dir, "RAIL_5V0", DCDC_BLOCK)
    assert res.json_written is True
    json_file = res.path / PROC_JSON
    assert json_file.exists()
    loaded = json.loads(json_file.read_text(encoding="utf-8"))
    assert loaded["test_id"] == "RAIL_5V0"


def test_parse_failure_keeps_text_only(
    tests_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(pack_parsers, "supports_sync_meta", lambda *a, **k: False)

    def _fail(*_a, **_k):
        raise pack_parsers.ParseFailure("error-severity findings")

    monkeypatch.setattr(pack_parsers, "parse_text", _fail)
    res = materialize_test(tests_dir, "RAIL_BAD", DCDC_BLOCK)
    assert res.json_written is False
    assert not (res.path / PROC_JSON).exists()
    assert (res.path / PROC_TEXT).exists()


# ---------------------------------------------------------------------------
# Document completion: title + meta synthesis (body verbatim)
# ---------------------------------------------------------------------------


def test_completion_prepends_title_and_meta(
    tests_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """When sync_meta is available, the written text gains a title + ## Meta."""
    monkeypatch.setattr(pack_parsers, "supports_sync_meta", lambda *a, **k: True)

    def _sync_meta(text, project_root=None):
        # Stub the deterministic synthesizer: inject a Meta block after the title.
        lines = text.splitlines()
        out = [lines[0], "", "## Meta", "format_version: 2.0.1", ""] + lines[2:]
        return "\n".join(out), []

    monkeypatch.setattr(pack_parsers, "sync_meta_text", _sync_meta)
    # parse still unavailable -> text-only, but the completed text is written.
    monkeypatch.setattr(
        pack_parsers, "parse_text",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pack_parsers.ParserUnavailable("x")
        ),
    )

    res = materialize_test(tests_dir, "PSU - +MAIN_5V0", DCDC_BLOCK)
    written = (res.path / PROC_TEXT).read_text(encoding="utf-8")
    assert written.startswith("# PSU_-_MAIN_5V0")
    assert "## Meta" in written
    # body still present verbatim
    assert "PSU1 : psu channels=" in written


def test_completion_leaves_existing_meta_untouched(
    tests_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """A document that already has ## Meta is not re-synthesized."""
    called = {"sync": False}

    def _sync_meta(text, project_root=None):
        called["sync"] = True
        return text, []

    monkeypatch.setattr(pack_parsers, "supports_sync_meta", lambda *a, **k: True)
    monkeypatch.setattr(pack_parsers, "sync_meta_text", _sync_meta)
    monkeypatch.setattr(
        pack_parsers, "parse_text",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pack_parsers.ParserUnavailable("x")
        ),
    )

    complete_doc = "# ALREADY\n\n## Meta\nformat_version: 2.0.1\n\n" + DCDC_BLOCK
    res = materialize_test(tests_dir, "ALREADY", complete_doc)
    written = (res.path / PROC_TEXT).read_text(encoding="utf-8")
    assert called["sync"] is False
    assert written == complete_doc  # verbatim, untouched
