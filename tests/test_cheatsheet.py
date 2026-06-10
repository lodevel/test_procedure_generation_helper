"""Tests for the DSL cheat-sheet extractor (core/cheatsheet.py).

NOTE: this is a TRANSITIONAL, heading-coupled extractor — it lifts the Markdown
tables/fences the bundle rule docs already contain. The long-term fix is a
bundle-shipped ``cheatsheet.md`` (the prebuilt seam, tested below). These tests
lock the extractor's contract so heading drift is caught, not silent.

Loaded standalone (the module is stdlib-only) to avoid the package __init__'s
heavy LLM imports.
"""

import importlib.util
import json
import os
import tempfile
from pathlib import Path

_MOD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "workflow_editor", "core", "cheatsheet.py")
_spec = importlib.util.spec_from_file_location("cheatsheet", _MOD)
cheatsheet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cheatsheet)


def _bundle(tmp: Path) -> Path:
    """A minimal two-doc bundle: a base doc (verbs table) + a scope-like pack
    doc whose verb table has NO 'Verbs and forms' heading (header-row fallback)."""
    (tmp / "001_base.md").write_text(
        "# Base\n\n## Steps\n\n### Canonical verbs\n\n"
        "| Verb | Used by |\n|---|---|\n| Set | psu |\n| Wait | sleep |\n\n"
        "### Directive set\n\n| Directive | Form |\n|---|---|\n| @FOR | @FOR i IN |\n\n"
        "## `## Expected` section — closed criterion grammar\n\n"
        "| Form | Notes |\n|---|---|\n| = | equality |\n",
        encoding="utf-8")
    (tmp / "006_scope.md").write_text(
        "# Scope\n\n## Equipment line\n\n```\nSCOPE1 : scope channels=[1,2]\n```\n\n"
        "## Acquisition ops\n\n| Action | Canonical form |\n|---|---|\n"
        "| Arm | Arm SCOPE1. |\n",
        encoding="utf-8")
    (tmp / "manifest.json").write_text(json.dumps([
        {"index": 1, "filename": "001_base.md", "source": "pack:base"},
        {"index": 2, "filename": "006_scope.md", "source": "pack:labscpi"},
    ]), encoding="utf-8")
    return tmp


def test_extracts_base_and_pack_surfaces():
    with tempfile.TemporaryDirectory() as td:
        root = _bundle(Path(td))
        sheet = cheatsheet.build_cheatsheet(root)
        assert "Canonical verbs" in sheet and "| Set | psu |" in sheet
        assert "Directive set" in sheet and "@FOR i IN" in sheet
        assert "Expected-criterion comparators" in sheet and "| = | equality |" in sheet
        assert "SCOPE1 : scope" in sheet                  # equipment-line fence
        assert "Arm SCOPE1." in sheet                     # Canonical-form fallback


def test_list_docs_full_text_and_titles():
    with tempfile.TemporaryDirectory() as td:
        root = _bundle(Path(td))
        docs = cheatsheet.list_docs(root)
        assert [d["title"] for d in docs] == ["Base", "Scope"]          # first H1
        assert "channels=[1,2]" in docs[1]["text"]                       # verbatim
        assert docs[0]["source"] == "pack:base"


def test_prebuilt_cheatsheet_overrides_extraction():
    with tempfile.TemporaryDirectory() as td:
        root = _bundle(Path(td))
        (root / "cheatsheet.md").write_text("# SHIPPED\nbundle wins\n", encoding="utf-8")
        assert cheatsheet.build_cheatsheet(root) == "# SHIPPED\nbundle wins\n"


def test_manifestless_glob_fallback():
    with tempfile.TemporaryDirectory() as td:
        root = _bundle(Path(td))
        (root / "manifest.json").unlink()
        assert "Canonical verbs" in cheatsheet.build_cheatsheet(root)


def test_graceful_with_no_rules():
    assert cheatsheet.build_cheatsheet(None) == ""
    assert cheatsheet.list_docs(None) == []
    assert cheatsheet.build_cheatsheet(Path("/nonexistent/x")) == ""
    assert cheatsheet.list_docs(Path("/nonexistent/x")) == []


if __name__ == "__main__":
    test_extracts_base_and_pack_surfaces()
    test_list_docs_full_text_and_titles()
    test_prebuilt_cheatsheet_overrides_extraction()
    test_manifestless_glob_fallback()
    test_graceful_with_no_rules()
    print("all cheat-sheet tests passed")
