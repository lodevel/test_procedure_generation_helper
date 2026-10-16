"""core.test_sync — the Tests list's out-of-sync flag and Mark In Sync for a
test that is not open, on the same session file and hashes as an open test."""

import json

from workflow_editor.core import test_sync

TEXT = "# Foo\n\n## Steps\n1. do x\n"
JSON = json.dumps({"id": "Foo", "steps": []}, indent=2)


def _test(tmp_path, session=None):
    test_dir = tmp_path / "Foo"
    test_dir.mkdir(parents=True)
    (test_dir / "procedure_text.md").write_text(TEXT, encoding="utf-8")
    (test_dir / "procedure.json").write_text(JSON, encoding="utf-8")
    if session is not None:
        (test_dir / ".llm_session.json").write_text(session, encoding="utf-8")
    return test_dir


def test_no_session_or_no_baseline_reads_in_sync(tmp_path):
    assert test_sync.is_out_of_sync(_test(tmp_path), []) is False
    assert test_sync.is_out_of_sync(_test(tmp_path / "b", "{}"), []) is False


def test_a_damaged_session_reads_in_sync(tmp_path):
    assert test_sync.is_out_of_sync(_test(tmp_path, "{not json"), []) is False
    assert test_sync.is_out_of_sync(_test(tmp_path / "b", "[1, 2]"), []) is False


def test_the_session_flag_alone_names_every_artifact(tmp_path):
    test_dir = _test(tmp_path, '{"artifacts_in_sync": false}')
    assert test_sync.stale_artifacts(test_dir, []) == ("procedure.json", "test.py", "procedure_text.md")
    assert test_sync.is_out_of_sync(test_dir, []) is True


def test_only_the_changed_artifact_is_stale(tmp_path):
    """The baseline stays the acknowledged one while the session says out of sync, so the
    file that moved away from it is named, flag or not."""
    test_dir = _test(tmp_path)
    test_sync.mark_in_sync(test_dir, [])
    assert test_sync.stale_artifacts(test_dir, []) == ()
    (test_dir / "procedure.json").write_text(JSON.replace("[]", "[1]"), encoding="utf-8")
    assert test_sync.stale_artifacts(test_dir, []) == ("procedure.json",)
    session = test_dir / ".llm_session.json"
    data = json.loads(session.read_text(encoding="utf-8"))
    session.write_text(json.dumps({**data, "artifacts_in_sync": False}), encoding="utf-8")
    assert test_sync.stale_artifacts(test_dir, []) == ("procedure.json",)
    assert test_sync.is_out_of_sync(test_dir, []) is True


def test_mark_then_edit_then_mark(tmp_path):
    test_dir = _test(tmp_path, json.dumps({"intent": "keep me", "artifacts_in_sync": False}))
    test_sync.mark_in_sync(test_dir, [])
    session = json.loads((test_dir / ".llm_session.json").read_text(encoding="utf-8"))
    assert session["artifacts_in_sync"] is True and session["intent"] == "keep me"
    assert set(session["artifact_hashes"]) == {"procedure_text.md", "procedure.json"}
    assert test_sync.is_out_of_sync(test_dir, []) is False
    (test_dir / "procedure_text.md").write_text(TEXT + "2. do y\n", encoding="utf-8")
    assert test_sync.is_out_of_sync(test_dir, []) is True  # edited while closed
    test_sync.mark_in_sync(test_dir, [])
    assert test_sync.is_out_of_sync(test_dir, []) is False


def test_a_test_marked_on_a_damaged_session_gets_a_fresh_one(tmp_path):
    test_dir = _test(tmp_path, "{not json")
    test_sync.mark_in_sync(test_dir, [])
    assert json.loads((test_dir / ".llm_session.json").read_text(encoding="utf-8"))["artifacts_in_sync"] is True


def test_mark_keeps_a_session_the_dataclass_cannot_load(tmp_path):
    """An odd decision shape must not cost the rest of the session file."""
    odd = {"intent": "keep me", "decisions": ["a plain string"], "custom": 1,
           "artifacts_in_sync": False, "artifact_hashes": {"test.py": "old"}}
    test_dir = _test(tmp_path, json.dumps(odd))
    assert test_sync.is_out_of_sync(test_dir, []) is True  # the flag is still read
    test_sync.mark_in_sync(test_dir, [])
    session = json.loads((test_dir / ".llm_session.json").read_text(encoding="utf-8"))
    assert session["decisions"] == ["a plain string"] and session["custom"] == 1
    assert session["artifact_hashes"]["test.py"] == "old"  # merged, not replaced
    assert session["artifacts_in_sync"] is True


def test_null_hashes_read_as_no_baseline(tmp_path):
    test_dir = _test(tmp_path, '{"artifacts_in_sync": true, "artifact_hashes": null}')
    assert test_sync.is_out_of_sync(test_dir, []) is False
    test_sync.mark_in_sync(test_dir, [])
    assert test_sync.is_out_of_sync(test_dir, []) is False
