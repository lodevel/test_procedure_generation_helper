"""THE daemon's stdin channel: the one reader thread + queue, and the frames a running test
exchanges over it (output lines, operator prompts, in-run cancels) — the bench-session runner's
in-run waits (contract: :mod:`_bench_session_runner`'s docstring).

A PLAIN SIBLING module like the runner: loaded by bare name, never through the ``procedo``
package (absent from project venvs).
"""

from __future__ import annotations

import json
import queue
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _bench_session_runner import BenchState, _Run

_EOF = object()  # queue sentinel: parent closed the pipe


# ---------------------------------------------------------------------------
# THE one reader thread + queue.
# ---------------------------------------------------------------------------


class Reader:
    """Daemon-side stdin reader: one thread pumping lines into one queue.

    Owns EOF detection (implementer note 2): EOF is signalled THROUGH the
    queue and RE-QUEUED on every consumption, so nested consumers (prompt
    waits, cancel drains) each observe it and the main loop still reaches
    the final ``_teardown`` backstop.
    """

    def __init__(self, stream) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._pump, args=(stream,), daemon=True, name="bench-stdin-reader"
        )
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._q.put(line)
        except Exception:  # noqa: BLE001 — a dying pipe is EOF, not a crash
            pass
        self._q.put(_EOF)

    def get(self):
        """Blocking: next raw line, or the ``_EOF`` sentinel (re-queued)."""
        item = self._q.get()
        if item is _EOF:
            self._q.put(_EOF)
        return item

    def poll(self):
        """Non-blocking: next raw line, ``_EOF`` (re-queued), or None."""
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            return None
        if item is _EOF:
            self._q.put(_EOF)
        return item

    def next_line(self):
        """Main-loop accessor: blocking; ``None`` on EOF (mirrors readline)."""
        item = self.get()
        return None if item is _EOF else item


_READER: Reader | None = None


def start_reader(stream) -> Reader:
    """Create THE reader (called once from the daemon's ``main()``)."""
    global _READER
    _READER = Reader(stream)
    return _READER


# ---------------------------------------------------------------------------
# Frame plumbing shared by the in-run waits.
# ---------------------------------------------------------------------------


def _emit(d: dict, obj: dict) -> None:
    """Write one daemon->host frame on the protocol channel (no-op when the
    channel is not wired, i.e. handler-level unit tests without frames)."""
    out = d.get("_PROTO_OUT")
    if out is not None:
        d["_write_frame"](out, obj)


def _parse_frame(d: dict, line: str):
    line = line.strip()
    if not line:
        return None
    try:
        req = json.loads(line)
    except Exception as exc:  # noqa: BLE001
        _emit(d, {"kind": "reject", "ok": False, "error": f"bad frame: {exc}"})
        return None
    return req if isinstance(req, dict) else None


def _handle_inrun_cancel(d: dict, st: BenchState, req: dict) -> None:
    rid = req.get("run_id")
    target = st.runs.get(rid)
    if target is None:
        _emit(
            d,
            {
                "kind": "reject",
                "ok": False,
                "cmd": "cancel_run",
                "error": f"unknown run_id {rid!r}",
            },
        )
        return
    target.cancelled = True
    _emit(d, {"kind": "ack", "cmd": "cancel_run", "ok": True, "run_id": rid})


_IN_RUN_REJECT = (
    "frame rejected: a test run is in progress (only prompt_response/cancel_run are accepted)"
)


def _cancelled_poll(d: dict, st: BenchState, run: _Run) -> bool:
    """``ctx.cancelled``: drain frames the reader queued (cancel_run sets the
    flag; anything else is rejected), then report the flag.  Runs at every
    step boundary and inside every ``_sleep`` slice."""
    rd = _READER
    while rd is not None:
        item = rd.poll()
        if item is None:
            break
        if item is _EOF:
            run.cancelled = True  # host gone: stop the test; the main loop
            break  # still sees the re-queued EOF -> _teardown
        req = _parse_frame(d, item)
        if req is None:
            continue
        cmd = req.get("cmd")
        if cmd == "cancel_run":
            _handle_inrun_cancel(d, st, req)
        elif cmd == "prompt_response":
            _emit(d, {"kind": "reject", "ok": False, "cmd": cmd, "error": "no prompt outstanding"})
        else:
            _emit(d, {"kind": "reject", "ok": False, "cmd": cmd, "error": _IN_RUN_REJECT})
    return run.cancelled


def _prompt_via_frames(d: dict, st: BenchState, run: _Run, text) -> str:
    """Holder-side ``ctx.prompt``: a correlated ``{"kind":"prompt"}`` frame, then BLOCK
    for its prompt_response.  A stop aborts the test (OperatorStop) but never its cleanup
    (``stop_ignored``): there a prompt after a stop only shows its text (already output)."""
    from rules_packager_base import OperatorStop, stop_ignored

    if run.cancelled and stop_ignored():
        return ""
    pid = next(st.prompt_seq)
    _emit(d, {"kind": "prompt", "id": pid, "text": str(text)})
    rd = _READER
    if rd is None:
        raise RuntimeError("no stdin reader; operator prompt unavailable")
    while True:
        if run.cancelled and not stop_ignored():
            raise OperatorStop("operator stop requested during a prompt")
        item = rd.get()
        if item is _EOF:
            run.cancelled = True
            raise OperatorStop("host closed the pipe during a prompt")
        req = _parse_frame(d, item)
        if req is None:
            continue
        cmd = req.get("cmd")
        if cmd == "prompt_response":
            if req.get("id") == pid:
                text_val = req.get("text")
                return "" if text_val is None else str(text_val)
            _emit(
                d,
                {
                    "kind": "reject",
                    "ok": False,
                    "cmd": cmd,
                    "error": f"stale prompt_response (id {req.get('id')!r}, expected {pid})",
                },
            )
            continue
        if cmd == "cancel_run":
            _handle_inrun_cancel(d, st, req)
            continue  # loop re-checks run.cancelled -> OperatorStop
        _emit(d, {"kind": "reject", "ok": False, "cmd": cmd, "error": _IN_RUN_REJECT})


class _FrameStdout:
    """File-like sys.stdout stand-in for the run duration: each complete line
    becomes an ``{"kind":"output"}`` frame (precedent: progress frames).
    C-level VISA noise still lands on fd 1 -> stderr (daemon fd isolation)."""

    def __init__(self, d: dict) -> None:
        self._d = d
        self._buf = ""

    def write(self, s) -> int:
        s = str(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            _emit(self._d, {"kind": "output", "text": line})
        return len(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self._buf:
            _emit(self._d, {"kind": "output", "text": self._buf})
            self._buf = ""
