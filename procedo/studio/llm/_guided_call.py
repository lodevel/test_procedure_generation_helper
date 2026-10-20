"""A guided run's ``call`` frame in the RUN engine process (``_execute_op_subprocess.py``).

Imported as a PLAIN SIBLING (``_sib``), never through the ``procedo.studio`` package (PySide6 is
absent from project venvs). The daemon passes its module globals (``d``) so this module uses its
frame writer, its protocol channel and THE stdin reader (``_bench_session_runner``) with no import
cycle.

The function runs in a one-call functions worker (``FunctionWorker(once=True)``) in the run's
folder, where its ``functions`` snapshot is: a child of this process in a kill-on-close job, so a
crash or hang never reaches the held instruments and a killed engine takes it along.

From the frame's first line to its answer a heartbeat writes a ``progress`` frame each half second
(the host's per-frame deadline never ends us while the worker starts or the function runs). While
the function runs, each half second, the frames the reader queued are read — a posted
``cancel_call`` (or EOF) kills the worker and answers ``Stopped``; a posted bench frame
(``cancel_run``, ``prompt_response``) is handled as the main loop would; any other is dropped
(none comes: the host's lock holds the channel).
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path

_BEAT_S = 0.5   # a progress frame each half second while a call frame is served


class CallStopped(Exception):
    """The call was stopped: the host's ``cancel_call``, or its pipe closed."""


# The only frame writer while a call runs (_write_frame takes no lock); a second writer needs a shared lock.
@contextlib.contextmanager
def _heartbeat(d: dict, node_path: str):
    """A ``progress`` frame now and each half second until the block ends (the frame's answer is
    written after it, never between beats)."""
    out = d.get("_PROTO_OUT")
    if out is None:
        yield
        return
    done = threading.Event()

    def beat() -> None:
        while True:
            d["_write_frame"](out, {"kind": "progress", "node_path": node_path})
            if done.wait(_BEAT_S):
                return

    thread = threading.Thread(target=beat, daemon=True, name="call-heartbeat")
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()


def _check(d: dict):
    """``check_cancelled`` of the call (see the module docstring)."""
    def check() -> None:
        bench = d["_bench"]()
        reader = bench._READER
        while reader is not None and (item := reader.poll()) is not None:
            if item is bench._EOF:                  # re-queued: the main loop still tears down
                raise CallStopped("the host closed the channel")
            try:
                req = json.loads(item)
            except ValueError:
                continue
            cmd = req.get("cmd") if isinstance(req, dict) else None
            if cmd == "cancel_call":
                raise CallStopped("the call was stopped")
            if cmd in ("cancel_run", "prompt_response"):
                bench.handle(d, d["_SESSION"], req)
            elif cmd:
                print(f"frame {cmd!r} dropped: a call is running", file=sys.stderr, flush=True)
    return check


def cmd_call(d: dict, req: dict) -> dict:
    """The ``call`` frame: ``{"ok": true, "reading"}`` (the row's reading, manual_run.make_call) or
    ``{"ok": false, "kind": "Stopped"|"NoCall"|"NoProcedure"|"NoWorkDir", "error"}``; a progress
    frame each half second until then."""
    with _heartbeat(d, req.get("node_path", "")):
        return _call(d, req)


def _call(d: dict, req: dict) -> dict:
    from rules_packager_base.function_calls import FunctionWorker
    from rules_packager_base.rules.v2_0_2.parser.manual_run import make_call

    document, node_path, work_dir = req.get("procedure_json"), req.get("node_path", ""), req.get("work_dir")
    if not isinstance(document, dict):
        return {"ok": False, "kind": "NoProcedure", "error": "the call frame carries no procedure document"}
    if not work_dir or not Path(work_dir).is_dir():
        return {"ok": False, "kind": "NoWorkDir", "error": f"no run folder to call in: {work_dir!r}"}
    try:
        with FunctionWorker(work_dir, once=True) as worker:
            reading = make_call(document, d["_sib"]("_engine_ops").run_inputs(req), node_path, worker,
                                _check(d))
    except CallStopped as exc:
        return {"ok": False, "kind": "Stopped", "error": str(exc)}
    if reading is None:
        return {"ok": False, "kind": "NoCall", "error": f"step {node_path} has no call to make"}
    return {"ok": True, "reading": reading}
