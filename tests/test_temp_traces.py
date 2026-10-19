"""The editor's temp users leave nothing in %TEMP%: the guided run's screenshot dir goes
when its session shuts down (after the host harvested it), and a code check makes no
file at all."""
from __future__ import annotations

import subprocess
import sys
import tempfile

import pytest

from procedo.core import temp_root
from procedo.studio.core.validators import CodeValidator
from procedo.studio.pack_ops import pack_parsers

# A daemon that answers one frame, then exits: what ``shutdown`` talks to.
_ONE_FRAME = "import sys; sys.stdin.readline(); print('{\"ok\": true, \"log\": []}', flush=True)"


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    """``tmp_path/TEMP`` is %TEMP% for this test, and this process has no root yet."""
    folder = tmp_path / "TEMP"
    folder.mkdir()
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(folder))
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    monkeypatch.setattr(temp_root, "_root", None)
    yield folder
    temp_root.remove()


def _session(tmp_path, daemon: str | None = None) -> pack_parsers.RemoteSession:
    sess = pack_parsers.RemoteSession(tmp_path)
    assert sess.screenshot_dir.parent == temp_root.root()
    if daemon is not None:  # stands in for _start: a live daemon, cwd in the shots dir
        sess._proc = subprocess.Popen(
            [sys.executable, "-c", daemon], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(sess.screenshot_dir))
    return sess


def test_shutdown_removes_the_screenshot_dir(temp_dir, tmp_path):
    sess = _session(tmp_path, _ONE_FRAME)
    (sess.screenshot_dir / "scope.png").write_bytes(b"png")
    assert sess.shutdown()["ok"] is True
    assert not sess.screenshot_dir.exists()


def test_shutdown_of_a_session_that_never_started_removes_it_too(temp_dir, tmp_path):
    sess = _session(tmp_path)
    sess.shutdown()
    assert not sess.screenshot_dir.exists()


def test_a_killed_session_keeps_its_screenshots_for_finish_and_save(temp_dir, tmp_path):
    """A session lost mid-run is killed before the operator's Finish & Save harvests
    its screenshots; the temp root removes them at exit."""
    sess = _session(tmp_path, "import time; time.sleep(60)")
    (sess.screenshot_dir / "scope.png").write_bytes(b"png")
    sess.kill()
    assert (sess.screenshot_dir / "scope.png").is_file()


def test_safe_off_remote_leaves_no_screenshot_dir(temp_dir, tmp_path, monkeypatch):
    def no_venv(_root):
        raise pack_parsers.ParserUnavailable("no venv")

    monkeypatch.setattr(pack_parsers, "_resolve_project_python", no_venv)
    res = pack_parsers.safe_off_remote([], {}, tmp_path)
    assert res["kind"] == "ParserUnavailable"
    assert list(temp_root.root().iterdir()) == []


def test_code_check_writes_nothing_and_words_errors_as_py_compile(temp_dir):
    assert CodeValidator().validate("x = 1\n").is_valid
    bad = CodeValidator().validate("def broken(:\n")
    assert not bad.is_valid
    message = bad.issues[0].message
    assert message.startswith('Python syntax error:   File "test.py", line 1')
    assert "SyntaxError" in message
    assert list(temp_dir.iterdir()) == []  # no temp .py, no __pycache__ .pyc


def test_code_check_reads_a_leading_bom_as_py_compile_did(temp_dir):
    assert CodeValidator().validate("\ufeffx = 1\n").is_valid
