"""procedure.json media follows the step text wherever the JSON came from.

The parser computes media for its own output. A JSON the operator wrote or
accepted from the LLM has none, or stale ones; saving it runs the same
catalogue, so what reaches disk (and the editor buffer) is what the parser
would have produced. On parser output the save changes nothing.
"""
from __future__ import annotations

import json

from rules_packager_base.rules.v2_0_2 import parse
from procedo.studio.core import ArtifactType
from procedo.studio.core.artifact_manager import ArtifactManager
from procedo.studio.pack_ops import pack_parsers

_HAND = {
    "format_version": "2.0.1",
    "id": "GPO",
    "steps": [
        {"n": 1, "op": "connect", "text": "the load + to P4 pin 1"},
        {"n": 2, "op": "wait", "seconds": 1,
         "media": [{"type": "image", "ref": {"component": "STALE", "pin": None}, "caption": "STALE"}]},
    ],
    "expected": [],
}

_TEXT = """# GPO

## Description
d

## Meta
format_version: 2.0.1
board: B
rules_pack: rules_packager_base@2.0.5

## Steps
1. Connect the load + to P4 pin 1, - to J2 pins 1-2.
2. Wait 1 s.
"""


def _manager(tmp_path):
    test_dir = tmp_path / "GPO"          # no project layout above: the editor's own pack, in-process
    test_dir.mkdir()
    manager = ArtifactManager()
    manager.set_test_dir(test_dir)
    return manager


def test_a_hand_written_json_gets_its_media_on_save(tmp_path):
    manager = _manager(tmp_path)
    manager.set_content(ArtifactType.PROCEDURE_JSON, json.dumps(_HAND, indent=2))

    manager.save_artifact(ArtifactType.PROCEDURE_JSON)

    saved = json.loads((tmp_path / "GPO" / "procedure.json").read_text(encoding="utf-8"))
    assert [m["caption"] for m in saved["steps"][0]["media"]] == ["P4 pin 1"]
    assert "media" not in saved["steps"][1]                    # stale media on a non-text op is gone
    assert manager.get_content(ArtifactType.PROCEDURE_JSON) == json.dumps(saved, indent=2, ensure_ascii=False)


def test_parser_output_is_saved_unchanged(tmp_path):
    result = parse(_TEXT)
    assert result.success, [f"{f.code}: {f.message}" for f in result.findings]
    content = json.dumps(result.json, indent=2, ensure_ascii=False)
    manager = _manager(tmp_path)
    manager.set_content(ArtifactType.PROCEDURE_JSON, content)

    manager.save_artifact(ArtifactType.PROCEDURE_JSON)

    assert (tmp_path / "GPO" / "procedure.json").read_text(encoding="utf-8") == content


def test_content_that_is_not_a_procedure_is_saved_as_written(tmp_path):
    manager = _manager(tmp_path)
    for content in ("not json at all", "[1, 2]"):
        manager.set_content(ArtifactType.PROCEDURE_JSON, content)
        manager.save_artifact(ArtifactType.PROCEDURE_JSON)
        assert (tmp_path / "GPO" / "procedure.json").read_text(encoding="utf-8") == content


def test_refresh_media_in_process_is_the_catalogue(tmp_path):
    doc = json.loads(json.dumps(_HAND))
    out = pack_parsers.refresh_media(doc)
    assert out is doc
    assert [m["caption"] for m in doc["steps"][0]["media"]] == ["P4 pin 1"]


def _saved_over_a_baseline(tmp_path, *, acknowledged):
    from procedo.studio.core import test_sync
    manager = _manager(tmp_path)
    test_dir = tmp_path / "GPO"
    text = json.dumps(_HAND, indent=2)
    (test_dir / "procedure.json").write_text(text, encoding="utf-8")
    if acknowledged:
        test_sync.mark_in_sync(test_dir, [])
    else:
        (test_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        test_sync.mark_in_sync(test_dir, [])
        (test_dir / "test.py").write_text("x = 2\n", encoding="utf-8")   # edited since: out of sync
    manager.set_content(ArtifactType.PROCEDURE_JSON, text)
    manager.save_artifact(ArtifactType.PROCEDURE_JSON)                 # the refresh rewrites the media
    assert "P4 pin 1" in (test_dir / "procedure.json").read_text(encoding="utf-8")
    return test_sync.is_out_of_sync(test_dir, [])


def test_the_refresh_write_keeps_a_coherent_test_in_sync(tmp_path):
    assert _saved_over_a_baseline(tmp_path, acknowledged=True) is False


def test_the_refresh_write_does_not_acknowledge_an_out_of_sync_test(tmp_path):
    assert _saved_over_a_baseline(tmp_path, acknowledged=False) is True


def test_the_refresh_write_gives_a_never_acknowledged_test_no_baseline(tmp_path):
    manager = _manager(tmp_path)
    manager.set_content(ArtifactType.PROCEDURE_JSON, json.dumps(_HAND, indent=2))
    manager.save_artifact(ArtifactType.PROCEDURE_JSON)               # the refresh rewrites the media
    assert "P4 pin 1" in (tmp_path / "GPO" / "procedure.json").read_text(encoding="utf-8")
    assert not (tmp_path / "GPO" / ".llm_session.json").exists()
