"""Studio's stderr is its log: a failing log handler cannot loop, and the stream looks like a file."""

import io
import logging
import sys
import threading

import pytest

from procedo.studio import logging_config
from procedo.studio.core import studio_settings


@pytest.fixture
def stderr_log(monkeypatch, tmp_path):
    """setup_logging on a log under tmp_path: (Studio's stderr, the log path), the process-wide hooks
    restored afterwards. pytest's capture swaps sys.stderr back between phases: a test installs the stream."""
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(logging_config.atexit, "register", lambda fn: None)
    logger = logging.getLogger("procedo.studio")
    before = list(logger.handlers)
    logging_config.setup_logging(log_file=tmp_path / "studio.log")
    stream = sys.stderr
    yield stream, tmp_path / "studio.log"
    for handler in logger.handlers:
        handler.close()
    logger.handlers[:] = before


def test_the_log_is_in_studios_own_settings_folder():
    assert studio_settings.get_log_path() == studio_settings.get_settings_path().with_name("studio.log")


def test_a_log_handler_that_fails_does_not_recurse(stderr_log, monkeypatch):
    stream, _ = stderr_log
    monkeypatch.setattr(sys, "stderr", stream)
    handler = next(h for h in logging.getLogger("procedo.studio").handlers
                   if isinstance(h, logging.FileHandler))

    def emit(record):
        try:
            raise OSError("disk full")
        except OSError:
            handler.handleError(record)  # reports to sys.stderr, which is the log

    monkeypatch.setattr(handler, "emit", emit)
    print("a line", file=sys.stderr)  # RecursionError before the guard


def test_the_stream_looks_like_a_closed_off_text_file(stderr_log):
    stream, log = stderr_log
    assert stream.encoding == "utf-8" and stream.errors == "replace"
    assert stream.isatty() is False
    with pytest.raises(io.UnsupportedOperation):
        stream.fileno()
    stream.close()
    stream.write("still works\n")
    assert "stderr: still works" in log.read_text(encoding="utf-8")


def test_a_last_line_without_newline_is_logged_at_exit(stderr_log):
    stream, log = stderr_log
    stream.write("no newline")
    assert not log.exists() or "no newline" not in log.read_text(encoding="utf-8")
    stream.flush_pending()
    assert "stderr: no newline" in log.read_text(encoding="utf-8")


def test_threads_writing_at_once_lose_no_line(stderr_log):
    stream, log = stderr_log

    def spam():
        for i in range(50):
            stream.write(f"line {i}\n")

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert log.read_text(encoding="utf-8").count("stderr: line ") == 200
