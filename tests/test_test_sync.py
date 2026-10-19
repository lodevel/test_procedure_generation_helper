"""core.test_sync — the Tests list's out-of-sync flag and Mark In Sync for a
test that is not open, on the same session file and hashes as an open test."""

import json
import os
import re
import time
from pathlib import Path

from procedo.studio.core import test_sync

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


# --- the hash cache: the Tests list re-reads every test after each save -------------------

_LONG_AGO = 10_000_000_000  # ns: well past the racy window


def _age(path, by=_LONG_AGO):
    then = path.stat().st_mtime_ns - by
    os.utime(path, ns=(then, then))


def _counted_hashes(monkeypatch):
    calls = []
    real = test_sync.enforced_sync_hash
    monkeypatch.setattr(test_sync, "enforced_sync_hash",
                        lambda test_dir, name, *a: calls.append(name) or real(test_dir, name, *a))
    return calls


def test_an_unchanged_file_is_hashed_once_and_an_edit_is_seen(tmp_path, monkeypatch):
    test_dir = _test(tmp_path)
    test_sync.mark_in_sync(test_dir, [])
    for name in ("procedure_text.md", "procedure.json"):
        _age(test_dir / name)
    calls = _counted_hashes(monkeypatch)
    assert test_sync.stale_artifacts(test_dir, []) == ()
    assert test_sync.stale_artifacts(test_dir, []) == ()
    assert sorted(calls) == ["procedure.json", "procedure_text.md"]
    (test_dir / "procedure.json").write_text(JSON.replace("[]", "[1]"), encoding="utf-8")
    _age(test_dir / "procedure.json", by=_LONG_AGO // 2)
    assert test_sync.stale_artifacts(test_dir, []) == ("procedure.json",)


def test_a_file_rewritten_under_the_same_stamp_is_read_again(tmp_path):
    """Timestamps are coarse: a same-size rewrite within one clock tick keeps the stamp, so a
    file read that soon after it changed is not kept."""
    test_dir = _test(tmp_path)
    test_sync.mark_in_sync(test_dir, [])
    path = test_dir / "procedure.json"
    stamp = path.stat().st_mtime_ns
    assert test_sync.stale_artifacts(test_dir, []) == ()
    path.write_text(JSON.replace("[]", "{}"), encoding="utf-8")  # same size
    os.utime(path, ns=(stamp, stamp))
    assert test_sync.stale_artifacts(test_dir, []) == ("procedure.json",)


def test_a_file_studio_saved_is_not_read_back(tmp_path, monkeypatch):
    """The first read of a fresh file waits for the virus scanner: what Studio just wrote is
    hashed from what it wrote, and gives the hash the file gives."""
    from procedo.studio.core import ArtifactType
    from procedo.studio.core.artifact_manager import ArtifactManager
    test_dir = _test(tmp_path)
    manager = ArtifactManager()
    manager.set_test_dir(test_dir)
    manager.set_content(ArtifactType.PROCEDURE_TEXT, TEXT + "2. do y\n")
    manager.save_artifact(ArtifactType.PROCEDURE_TEXT)
    reads = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self.name) or real(self, *a, **k))
    remembered = test_sync._disk_hash(test_dir, "procedure_text.md", [])
    assert "procedure_text.md" not in reads
    test_sync._hashes.clear()
    assert remembered == test_sync._disk_hash(test_dir, "procedure_text.md", [])


def test_a_saved_text_holding_a_cr_is_read_back(tmp_path):
    """Written then read back, a CR changes the text: its hash comes from the file."""
    from procedo.studio.core import ArtifactType
    from procedo.studio.core.artifact_manager import ArtifactManager
    test_dir = _test(tmp_path)
    manager = ArtifactManager()
    manager.set_test_dir(test_dir)
    manager.set_content(ArtifactType.PROCEDURE_TEXT, TEXT.replace("\n", "\r\n"))
    manager.save_artifact(ArtifactType.PROCEDURE_TEXT)
    remembered = test_sync._disk_hash(test_dir, "procedure_text.md", [])
    test_sync._hashes.clear()
    assert remembered == test_sync._disk_hash(test_dir, "procedure_text.md", [])


def _kept(test_dir, name, patterns=()):
    """The file's hash, read and kept: its mtime is set long ago, then pinned there."""
    path = test_dir / name
    then = time.time_ns() - _LONG_AGO
    os.utime(path, ns=(then, then))
    test_sync._disk_hash(test_dir, name, list(patterns))
    return then


def test_a_size_change_under_a_kept_mtime_is_read_again(tmp_path):
    test_dir = _test(tmp_path)
    then = _kept(test_dir, "procedure.json")
    (test_dir / "procedure.json").write_text(JSON.replace("[]", "[1]"), encoding="utf-8")
    os.utime(test_dir / "procedure.json", ns=(then, then))
    assert test_sync._disk_hash(test_dir, "procedure.json", []) == _fresh(test_dir, "procedure.json", [])


def test_a_replaced_file_of_the_same_size_and_mtime_is_read_again(tmp_path):
    test_dir = _test(tmp_path)
    then = _kept(test_dir, "procedure.json")
    replacement = test_dir / "new.json"
    replacement.write_text(JSON.replace("[]", "{}"), encoding="utf-8")  # same size, a new file id
    os.utime(replacement, ns=(then, then))
    os.replace(replacement, test_dir / "procedure.json")
    assert test_sync._disk_hash(test_dir, "procedure.json", []) == _fresh(test_dir, "procedure.json", [])


def test_other_patterns_hash_the_file_again(tmp_path):
    test_dir = _test(tmp_path)
    (test_dir / "test.py").write_text("PSU_PORT = 'COM3'\nrun()\n", encoding="utf-8")
    _kept(test_dir, "test.py")
    patterns = [re.compile(r"^PSU_PORT\s*=")]
    assert test_sync._disk_hash(test_dir, "test.py", patterns) == _fresh(test_dir, "test.py", patterns)


def _fresh(test_dir, name, patterns):
    """The hash the file gives, read now."""
    return test_sync.enforced_sync_hash(
        test_dir, name, (test_dir / name).read_text(encoding="utf-8"), patterns)
