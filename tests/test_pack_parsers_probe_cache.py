"""The wheel's fixed answers are asked once per interpreter (spawn spy)."""
from __future__ import annotations

import pytest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.studio import _project_local_state  # noqa: E402
from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402


@pytest.fixture
def spawns(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_text("x")
    venv = _project_local_state.ProjectVenv(python, tmp_path / "bundle")
    calls = []

    def fake(_venv, spec, timeout):
        calls.append(spec["op"])
        if spec["op"] == "section_ownership":
            return {"ok": True, "ownership": {"steps": "llm"}}
        if spec["op"] == "supports":
            return {"ok": True, "supported": True}
        return {"ok": True, "available": True, "reason": ""}

    monkeypatch.setattr(pp, "_subprocess_call", fake)
    monkeypatch.setattr(pp, "_resolve_project_python", lambda _root: venv)
    pp._PROBE_CACHE.clear()
    yield calls, python
    pp._PROBE_CACHE.clear()


def test_supports_probes_spawn_once_per_interpreter(spawns, tmp_path, monkeypatch):
    calls, python = spawns
    for _ in range(3):
        assert pp.supports_sync_meta(tmp_path)
        assert pp.supports_build_manual_run(tmp_path)
        assert pp.supports_guided_manual(tmp_path)
    assert len(calls) == 3  # one per distinct question, not per call
    other = python.with_name("other.exe")
    other.write_text("x")
    monkeypatch.setattr(pp, "_resolve_project_python", lambda _r: _project_local_state.ProjectVenv(other, tmp_path))
    assert pp.supports_sync_meta(tmp_path)
    assert len(calls) == 4  # a different interpreter path has its own entry


def test_section_ownership_default_with_missing_sidecar_spawns_once(spawns, tmp_path):
    calls, python = spawns
    for _ in range(5):  # no bundle/rules/section_ownership.json: the keystroke case
        got = pp.get_section_ownership(tmp_path)
        got["steps"] = "mutated"  # a caller's edit must not poison the cache
    assert calls == ["section_ownership"]
    assert pp.get_section_ownership(tmp_path) == {"steps": "llm"}


def test_is_available_shares_the_cache(spawns, tmp_path):
    calls, _ = spawns
    assert pp.is_available(tmp_path) == (True, "")
    assert pp.is_available(tmp_path) == (True, "")
    assert calls == ["is_available"]


def test_a_transport_failure_is_asked_again_but_a_real_answer_is_kept(spawns, tmp_path, monkeypatch):
    calls, _ = spawns
    answers = [
        {"ok": False, "kind": "ParserUnavailable", "error": "Subprocess timed out after 30s"},
        {"ok": True, "supported": False},  # a real "unsupported"
    ]

    def flaky(_venv, spec, timeout):
        calls.append(spec["op"])
        return answers.pop(0)

    monkeypatch.setattr(pp, "_subprocess_call", flaky)
    assert not pp.supports_sync_meta(tmp_path)  # timed out: not remembered
    assert not pp.supports_sync_meta(tmp_path)  # asked again, real "no"
    assert not pp.supports_sync_meta(tmp_path)  # remembered
    assert len(calls) == 2
