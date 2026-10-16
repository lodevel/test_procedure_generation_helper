"""A wedged daemon is reported with the op it was on, within one op's budget."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from procedo.studio.pack_ops import pack_parsers as pp

OPS = [
    {"node_path": "1", "op": {"device": "psu1", "op": "psu.output", "channel": 1, "on": True}},
    {"node_path": "2", "op": {"device": "FNCORE1", "op": "fncore.read_analog"}},
    {"node_path": "3", "op": {"device": "psu1", "op": "psu.output", "channel": 1, "on": False}},
]


class _Proc:
    """A daemon that answers ``progress_lines`` then never answers again."""

    def __init__(self, progress_lines):
        self._lines = list(progress_lines)
        self.stdin = SimpleNamespace(write=lambda _s: None, flush=lambda: None)
        self.stdout = SimpleNamespace(readline=self._readline)
        self.terminated = False

    def _readline(self):
        if self._lines:
            return self._lines.pop(0)
        time.sleep(0.5)          # wedged synchronous read: outlives the deadline
        return ""

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _session(tmp_path, proc):
    sess = pp.RemoteSession(tmp_path / "procedure.json", tmp_path)
    sess._proc = proc
    return sess


def test_the_op_in_flight_is_named_after_the_last_progress_frame(tmp_path):
    done_first = json.dumps({"kind": "progress", "node_path": "1", "ok": True, "i": 0, "total": 3}) + "\n"
    proc = _Proc([done_first])
    res = _session(tmp_path, proc)._request({"cmd": "exec_ops", "ops": OPS}, timeout=0.05)
    assert res["kind"] == "SessionTimeout"
    assert res["error"] == "daemon unresponsive after 0.05s while running FNCORE1 fncore.read_analog (op 2 of 3)"
    assert proc.terminated     # the wedged daemon is gone before any recovery daemon opens the port


def test_a_wedge_before_any_progress_names_the_first_op(tmp_path):
    res = _session(tmp_path, _Proc([]))._request({"cmd": "exec_ops", "ops": OPS}, timeout=0.05)
    assert res["error"].endswith("while running psu1 psu.output (op 1 of 3)")


def test_a_frame_without_ops_keeps_the_plain_message(tmp_path):
    res = _session(tmp_path, _Proc([]))._request({"cmd": "safe_off", "targets": []}, timeout=0.05)
    assert res["error"] == "daemon unresponsive after 0.05s"


def test_a_batch_waits_one_op_budget_per_frame_however_long_it_is(tmp_path, monkeypatch):
    seen = {}
    sess = _session(tmp_path, _Proc([]))
    monkeypatch.setattr(sess, "_request", lambda frame, timeout, on_progress=None: seen.update(t=timeout) or {"ok": True})
    sess.exec_ops(OPS * 10, {})
    assert seen["t"] == pp._TIMEOUT_REMOTE_EXEC      # not 45 + 5 x 30
