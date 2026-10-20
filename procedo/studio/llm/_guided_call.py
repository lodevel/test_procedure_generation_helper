"""A guided run's ``call`` frame in the RUN engine process (``_execute_op_subprocess.py``).

Imported as a PLAIN SIBLING (``_sib``), never through the ``procedo.studio`` package (PySide6 is
absent from project venvs). The daemon passes its module globals (``d``) so this module uses its
frame writer, its protocol channel and THE stdin reader (``_bench_session_runner``) with no import
cycle.

The function runs in a one-call functions worker (``FunctionWorker(once=True)``) in the run's
folder, where its ``functions`` snapshot is: a child of this process in a kill-on-close job, so a
crash or hang never reaches the held instruments and a killed engine takes it along. While it
waits, each half second: a ``progress`` frame (the host's per-frame deadline never ends us during
a long function), then the frames the reader queued — a posted ``cancel_call`` (or EOF) kills the
worker and answers ``Stopped``; a posted bench frame (``cancel_run``, ``prompt_response``) is
handled as the main loop would; any other is dropped (none comes: the host's lock holds the channel).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


class CallStopped(Exception):
    """The call was stopped: the host's ``cancel_call``, or its pipe closed."""


def _check(d: dict, node_path: str):
    """``check_cancelled`` of the call (see the module docstring)."""
    def check() -> None:
        if d.get("_PROTO_OUT") is not None:
            d["_write_frame"](d["_PROTO_OUT"], {"kind": "progress", "node_path": node_path})
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
    ``{"ok": false, "kind": "Stopped"|"NoCall"|"NoProcedure"|"NoWorkDir", "error"}``."""
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
                                _check(d, node_path))
    except CallStopped as exc:
        return {"ok": False, "kind": "Stopped", "error": str(exc)}
    if reading is None:
        return {"ok": False, "kind": "NoCall", "error": f"step {node_path} has no call to make"}
    return {"ok": True, "reading": reading}
