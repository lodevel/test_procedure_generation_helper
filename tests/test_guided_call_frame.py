"""The RUN engine makes a guided run's Call (S6): the ``call`` frame answers the row's reading from a
one-call functions worker in the run's folder; a posted ``cancel_call`` ends a long call within a
second and the engine answers the next frame; progress frames keep a long call inside the per-frame
deadline; TOOLS never takes a call."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests import _env
from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.engine_client import _engine_ops as engine_ops  # noqa: E402
from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402
from procedo.engine_client.bench_client import BenchSessionClient  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON),
    pytest.mark.usefixtures("engine_in_process"),
]

_DAEMON = (
    Path(__file__).resolve().parent.parent
    / "procedo"
    / "engine_client"
    / "_execute_op_subprocess.py"
)
_HEAD = (
    "# T\nDesc.\n\n## Meta\nformat_version: 2.0.1\nboard: B\n"
    "rules_pack: rules_packager_base@2.15.1\n\n## Equipment\nDMM_GENERIC : dmm\n\n## Steps\n"
)
FUNCS = """\
import os
import time


def pair(a, b):
    return a + b, a - b


def wait(seconds):
    with open("pid.txt", "w") as fh:
        fh.write(str(os.getpid()))
    time.sleep(seconds)
    return seconds
"""


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "functions" / "analysis.py").write_text(FUNCS, encoding="utf-8")
    return tmp_path


@pytest.fixture
def session():
    """A RUN channel over the REAL daemon script, on this interpreter (no project venv)."""
    sess = BenchSessionClient(Path("/nonexistent"))

    def _start():
        sess._bundle_dir = None
        sess._proc = subprocess.Popen(
            [sys.executable, str(_DAEMON), "--role", "run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        sess._dead = False

    sess._start = _start
    yield sess
    sess.shutdown()


def _document(steps: str, expected: str) -> dict:
    doc, _ = pp.parse_text(_HEAD + steps + "\n## Expected\n" + expected)
    return doc


def _plan(doc: dict, measurements: dict) -> dict:
    return engine_ops.run(
        {"op": "build_manual_run", "procedure_json": doc, "measurements": measurements}
    )


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    ).stdout
    return str(pid) in out


def test_the_call_frame_answers_the_rows_reading(session, run_dir):
    doc = _document(
        "1. Operator: read voltage on TP X with DMM_GENERIC as {1}.\n"
        "2. Call analysis.pair({1}, {1}) as {2} V, {3}.\n",
        "{2} < 5 V\n",
    )
    first = _plan(doc, {})["steps"][0]["node_path"]
    measurements = {first: 1.5}
    row = _plan(doc, measurements)["steps"][1]
    assert row["kind"] == "call" and row["call"]["args"] == [1.5, 1.5]
    answer = session.call_function(
        row["node_path"],
        {"measurements": measurements},
        doc,
        work_dir=run_dir,
        stop=threading.Event(),
    )
    assert answer["ok"] and answer["reading"]["outcome"]["value"] == [3.0, 0.0], answer
    measurements[row["node_path"]] = answer["reading"]
    row = _plan(doc, measurements)["steps"][1]
    assert row["call"] is None and [(o["value_text"], o["verdict"]) for o in row["outputs"]] == [
        ("3 V", "PASS"),
        ("0", ""),
    ]
    again = session.call_function(
        row["node_path"],
        {"measurements": measurements},
        doc,
        work_dir=run_dir,
        stop=threading.Event(),
    )
    assert again["kind"] == "NoCall"  # made with these arguments: none to make
    assert sorted(p.name for p in run_dir.iterdir()) == ["functions"]


def test_a_posted_cancel_ends_a_long_call_within_a_second_and_the_engine_answers_next(
    session, run_dir
):
    doc = _document("1. Call analysis.wait(30) as {1}.\n", "{1} = 1\n")
    node_path = _plan(doc, {})["steps"][0]["node_path"]
    session.cancel_call()  # none running: no answer, nothing stale
    assert session.ping()["pong"]
    marker, box, stop = run_dir / "pid.txt", {}, threading.Event()
    worker = threading.Thread(
        target=lambda: box.update(
            answer=session.call_function(
                node_path, {"measurements": {}}, doc, work_dir=run_dir, stop=stop
            )
        )
    )
    worker.start()
    end = time.monotonic() + 30
    while not (marker.exists() and marker.read_text()) and time.monotonic() < end:
        time.sleep(0.05)
    pid = int(marker.read_text())
    stop.set()  # the run's Stop, then its cancel
    session.cancel_call()
    sent = time.monotonic()
    worker.join(10)
    assert box["answer"]["kind"] == "Stopped" and time.monotonic() - sent < 1.5, box
    assert not _alive(pid)
    assert (
        session.ping()["pong"] and session.engine_op("build_manual_run", procedure_json=doc)["ok"]
    )


def test_progress_frames_keep_a_long_call_inside_the_frame_deadline(session, run_dir):
    doc = _document("1. Call analysis.wait(2) as {1}.\n", "{1} = 2\n")
    node_path = _plan(doc, {})["steps"][0]["node_path"]
    # The daemon is up first, as a run's session is before its first Call: the 1 s per-frame
    # deadline bounds the call's silences, not the daemon's start (up to ~1.1 s under load).
    assert session.ping()["pong"]
    answer = session.call_function(
        node_path, {"measurements": {}}, doc, work_dir=run_dir, timeout=1, stop=threading.Event()
    )
    assert answer["ok"] and answer["reading"]["outcome"]["value"] == 2, answer


def test_tools_never_takes_a_call():
    assert engine_ops.refusal("tools", {"cmd": "call"})["kind"] == "Protocol"
    assert engine_ops.refusal("run", {"cmd": "call"}) is None


def test_a_stop_before_the_call_frame_never_runs_the_function(session, run_dir):
    """The Stop came first (its cancel even reached the engine before the call would have): the call
    frame never goes, the function never runs, and the engine answers next."""
    doc = _document("1. Call analysis.wait(30) as {1}.\n", "{1} = 1\n")
    node_path = _plan(doc, {})["steps"][0]["node_path"]
    assert session.ping()["pong"]
    stop = threading.Event()
    stop.set()
    session.cancel_call()  # reaches the engine before any call
    began = time.monotonic()
    answer = session.call_function(
        node_path, {"measurements": {}}, doc, work_dir=run_dir, stop=stop
    )
    assert answer["kind"] == "Stopped" and time.monotonic() - began < 1, answer
    assert not (run_dir / "pid.txt").exists() and session.ping()["pong"]
