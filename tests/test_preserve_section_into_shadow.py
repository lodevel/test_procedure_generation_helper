"""Tests for ``project_services.config_manager.preserve_section_into_shadow``.

This is the helper that backs ``ProjectConfigDialog._commit_shadow``'s
single-writer protection for the ``workflows`` section (Codex Q7,
Phase 1 of the workflows-to-project-config refactor): if a separate
writer touched ``config.json:workflows`` while the dialog was open, the
shadow commit must NOT clobber it. The helper runs just before the
copytree to overlay the live value onto the shadow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# Add the shared src/ to sys.path so we can import the foundation package
# without depending on the project being pip-installed. (config_manager now
# lives in the shared `project_services` package below both apps.)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The walk-up assumes the submodule layout; a STANDALONE editor checkout has no
# host src/ above it, so config_manager is truthfully absent -> skip.
pytest.importorskip("project_services")

from project_services.config_manager import preserve_section_into_shadow  # noqa: E402


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_live_workflows_preserved_into_shadow(tmp_path):
    real = tmp_path / "real" / "config"
    shadow = tmp_path / "shadow" / "config"

    _write(real / "config.json", {
        "manifest": {"name": "real"},
        "workflows": {"text_json": {"tasks": [{"id": "live"}]}},
    })
    _write(shadow / "config.json", {
        "manifest": {"name": "shadow edit"},
        "workflows": {"text_json": {"tasks": [{"id": "STALE"}]}},
    })

    preserve_section_into_shadow(real, shadow, "workflows")

    result = json.loads((shadow / "config.json").read_text(encoding="utf-8"))
    # Shadow's other sections are untouched (so dialog edits to other tabs commit).
    assert result["manifest"] == {"name": "shadow edit"}
    # Workflows section taken from the live file, not the stale shadow copy.
    assert result["workflows"] == {"text_json": {"tasks": [{"id": "live"}]}}


def test_live_missing_workflows_drops_section_from_shadow(tmp_path):
    """If the live file has no ``workflows`` key, the shadow's value
    must be dropped too. Otherwise a stale shadow workflows section
    would resurrect a deletion done elsewhere."""
    real = tmp_path / "real" / "config"
    shadow = tmp_path / "shadow" / "config"

    _write(real / "config.json", {"manifest": {"name": "no workflows live"}})
    _write(shadow / "config.json", {
        "manifest": {"name": "shadow"},
        "workflows": {"text_json": {"tasks": [{"id": "stale"}]}},
    })

    preserve_section_into_shadow(real, shadow, "workflows")

    result = json.loads((shadow / "config.json").read_text(encoding="utf-8"))
    assert "workflows" not in result
    assert result["manifest"] == {"name": "shadow"}


def test_silent_noop_when_files_missing(tmp_path):
    real = tmp_path / "real" / "config"  # no config.json
    shadow = tmp_path / "shadow" / "config"
    _write(shadow / "config.json", {"workflows": {"x": 1}})

    preserve_section_into_shadow(real, shadow, "workflows")

    # Shadow file untouched.
    result = json.loads((shadow / "config.json").read_text(encoding="utf-8"))
    assert result == {"workflows": {"x": 1}}


def test_silent_noop_on_malformed_live(tmp_path):
    """A corrupt live config.json must not raise — the dialog commit
    proceeds with whatever the shadow holds."""
    real = tmp_path / "real" / "config"
    shadow = tmp_path / "shadow" / "config"
    real.mkdir(parents=True)
    shadow.mkdir(parents=True)
    (real / "config.json").write_text("{ not json", encoding="utf-8")
    _write(shadow / "config.json", {"workflows": {"x": 1}})

    preserve_section_into_shadow(real, shadow, "workflows")

    result = json.loads((shadow / "config.json").read_text(encoding="utf-8"))
    assert result == {"workflows": {"x": 1}}
