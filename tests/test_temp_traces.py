"""The editor's temp users leave nothing in %TEMP%: an engine process makes no folder of its
own (it works in the app's temp root; the guided run owns the folder its screenshots land in),
and a code check makes no file at all."""
from __future__ import annotations

import tempfile

import pytest

from procedo.core import temp_root
from procedo.studio.core.validators import CodeValidator
from procedo.studio.pack_ops import engine, engine_session


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


def test_an_engine_process_makes_no_folder_of_its_own(temp_dir, tmp_path):
    """Its working folder is the temp root itself (test_project_venv_bridge pins the
    cwd), so even a killed instance leaves nothing behind."""
    engine_session.RemoteSession(tmp_path).kill()
    assert list(temp_root.root().iterdir()) == []


def test_safe_off_remote_leaves_nothing_in_the_temp_root(temp_dir, tmp_path, monkeypatch):
    def no_venv(_root):
        raise engine_session.ParserUnavailable("no venv")

    monkeypatch.setattr(engine_session, "_resolve_project_python", no_venv)
    res = engine.safe_off_remote([], {}, tmp_path)
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
