"""core.fileio.atomic_write_text — the editor's one safe write."""

import os

import pytest

from procedo.core import atomic_write
from procedo.studio.core import fileio
from procedo.studio.core.fileio import atomic_write_text


def test_replaces_an_existing_file_through_os_replace(tmp_path, monkeypatch):
    """Over an existing file the swap is os.replace (atomic on Windows), never a
    copy-then-delete; no temp file is left behind."""
    target = tmp_path / "procedure.json"
    target.write_text("old", encoding="utf-8")
    calls = []
    real = os.replace
    monkeypatch.setattr(atomic_write.os, "replace", lambda a, b: calls.append(b) or real(a, b))
    atomic_write_text(target, "new µ±Ω")
    assert target.read_text(encoding="utf-8") == "new µ±Ω"
    assert calls == [target] and os.listdir(tmp_path) == ["procedure.json"]


def test_creates_the_folder(tmp_path):
    atomic_write_text(tmp_path / "a" / "b" / "x.txt", "x")
    assert (tmp_path / "a" / "b" / "x.txt").read_text(encoding="utf-8") == "x"


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "test.py"
    target.write_text("old", encoding="utf-8")

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_write.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert os.listdir(tmp_path) == ["test.py"]


def test_a_target_briefly_held_open_is_retried(tmp_path, monkeypatch):
    """Another process reading the file for a moment (Windows refuses the
    rename then) must not fail the save."""
    target = tmp_path / "procedure.json"
    target.write_text("old", encoding="utf-8")
    real = os.replace
    failures = iter([PermissionError(5, "Access is denied")] * 3)

    def busy_then_free(a, b):
        err = next(failures, None)
        if err is not None:
            raise err
        real(a, b)

    monkeypatch.setattr(atomic_write.os, "replace", busy_then_free)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_S", 0)
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new" and os.listdir(tmp_path) == [
        "procedure.json"
    ]


def test_a_target_held_open_for_good_fails_and_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "test.py"
    target.write_text("old", encoding="utf-8")

    def always_busy(*_):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(atomic_write.os, "replace", always_busy)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_S", 0)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old" and os.listdir(tmp_path) == ["test.py"]


def test_a_save_that_cannot_clean_up_leaves_only_a_pid_named_temp(tmp_path, monkeypatch):
    """A kill (or a failed unlink) leaves a temp the start-up sweep can take: its name
    carries the writer's pid, never a bare random mkstemp name."""
    target = tmp_path / "settings.json"

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_write.os, "replace", boom)
    monkeypatch.setattr(atomic_write.os, "unlink", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "new")
    monkeypatch.undo()
    (leftover,) = os.listdir(tmp_path)
    assert atomic_write._temp_owner(leftover) == os.getpid()


def test_atomic_create_writes_the_bytes_verbatim(tmp_path):
    """CRLF, LF and lone CR reach the file exactly as given (never text-translated)."""
    data = b"a\r\nb\nc\rd"
    fileio.atomic_create(tmp_path / "sub" / "x.md", data)
    assert (tmp_path / "sub" / "x.md").read_bytes() == data


@pytest.mark.skipif(os.linesep != "\r\n", reason="Windows line endings")
def test_line_endings_are_the_platform_s_as_before(tmp_path):
    """Every old writer used text mode: on Windows a \\n is written as \\r\\n."""
    atomic_write_text(tmp_path / "x.md", "a\nb")
    assert (tmp_path / "x.md").read_bytes() == b"a\r\nb"


def test_task_config_writes_readable_indented_json(tmp_path):
    """The workflows config keeps its format: indented, µ/Ω as characters."""
    import json

    from procedo.studio.core.task_config_model import TaskConfig
    from procedo.studio.core.task_config import TaskConfigManager

    config_path = tmp_path / "tab_contexts.json"
    manager = TaskConfigManager(config_path)
    manager.add_task(
        "text_json",
        TaskConfig(
            id="unit_task", name="Mesure µΩ", button_label="µΩ", prompt_template="p", enabled=True
        ),
    )
    manager.save_config()
    text = config_path.read_text(encoding="utf-8")
    assert "Mesure µΩ" in text  # not \u00b5
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False)
