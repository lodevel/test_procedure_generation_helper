"""The RUN instance's channel: the host side of the instrument and run-handle frames.

:func:`.engine.run` hands one per project to its holder — the guided dialog or the
bench session. Thin subclass of :class:`~procedo.studio.pack_ops.pack_parsers.RemoteSession`
adding ``acquire_run`` / ``plan_only`` (dry-run reconcile) / ``run_test`` /
``cancel_run`` / ``release_run``, and a guided run's ``call_function`` / ``cancel_call``.
``end_session()`` IS the inherited ``shutdown()`` — deliberately no new frame.

Transport discipline (plan S3):

* Control frames (acquire/release/idle-cancel) ride the inherited
  ``_request`` — its short per-frame timeout + ``_dead_resp``
  kill-on-timeout machinery stays untouched and confined to them — behind
  this class's ``_request`` override, which turns a daemon dead between
  exchanges into ``SessionDead`` instead of the base class's lazy respawn.
* ``run_test`` uses NO per-frame timeout: liveness is the process poll plus
  the ``output`` / ``prompt`` frames the daemon streams.  A wedged run is
  ended by the operator (``cancel_run``, or kill+recovery if the daemon is
  truly wedged), never by a transport timer.
* ``prompt_response`` and ``cancel_run`` are POSTED, never answered: the
  stop button never waits on the channel (a request would wait for the run in
  flight); the base class's write lock keeps every writer's frame whole.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .pack_parsers import RemoteSession, _TIMEOUT_REMOTE_EXEC

# Daemon->host stream frames run_test consumes before its final response.
# Anything NOT in this set is the final response (error kinds like
# "ExecError" never collide — the daemon only tags stream frames).
_STREAM_KINDS = frozenset({"output", "prompt", "ack", "reject", "progress"})


class _CallNotSent(Exception):
    """A guided Call stopped before its frame went out: nothing ran."""


class BenchSessionClient(RemoteSession):
    """One persistent bench-holder channel speaking the S3 run-handle frames."""

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root, role="run")
        # The run_test in flight, as a Stop's deadline judges it (read on the GUI thread):
        self.in_run_since: Optional[float] = None  # monotonic start of the run_test in flight
        self.prompting = False     # ... and waits on the operator's answer
        self.acked_at: Optional[float] = None  # ... and when the daemon acknowledged a cancel ("ack")
        self._call_stop: Optional[threading.Event] = None  # the Stop of the Call being sent

    # ``end_session`` IS shutdown (safe-off all held + close/unlock + exit).
    end_session = RemoteSession.shutdown

    # -- transport guard ----------------------------------------------------
    def _request(self, frame: dict[str, Any], timeout: float,
                 on_progress=None) -> dict[str, Any]:
        """Inherited transport MINUS its lazy respawn. Invariant: once this
        client has started a holder, a daemon found dead between exchanges
        is ``SessionDead`` on EVERY frame — the base ``_request``'s
        ``if not self._alive(): self._start()`` must never hand a control
        frame (acquire/release/cancel/shutdown/ping) to a FRESH daemon
        holding nothing, silently skipping the host's crash-recovery
        safe-off (design row 5). A NEVER-started client still lazy-starts
        on its first frame."""
        with self._lock:
            if (not self._dead and self._proc is not None
                    and not self._alive()):
                rc = self._proc.poll()
                return self._dead_resp(
                    "SessionDead",
                    f"daemon died between exchanges (exit={rc})")
        return super()._request(frame, timeout, on_progress)

    def _post(self, frame: dict[str, Any]) -> None:
        """Every frame under the base class's write lock (re-entered by its ``_post``), so a Call's
        frame and its Stop go out in the order they happen: a Call whose Stop is already set is
        never written, so a Stop is never lost — set before the frame, the frame does not go; after
        it, its ``cancel_call`` follows it."""
        with self._write_lock:
            if frame.get("cmd") == "call" and self._call_stop is not None and self._call_stop.is_set():
                raise _CallNotSent
            super()._post(frame)

    # -- control frames (short timeouts, inherited transport) ---------------
    def acquire_run(self, claims: list[dict[str, Any]], *,
                    policies: Optional[dict] = None, keep_state: bool = False,
                    results_dir: str | Path = "",
                    timeout: Optional[int] = None) -> dict[str, Any]:
        """Reconcile ``claims`` into an endpoint plan and take the exclusive
        run handle.  ``claims`` = ``[{"test_id","local_name","etype",
        "subtype"?,"bench":{...}}]``; ``policies`` =
        ``{test_id: {local_name: "per_step"|"per_session"}}``."""
        frame: dict[str, Any] = {"cmd": "acquire_run", "claims": claims,
                                 "keep_state": bool(keep_state),
                                 "results_dir": str(results_dir)}
        if policies:
            frame["policies"] = policies
        return self._request(frame, timeout or _TIMEOUT_REMOTE_EXEC)

    def plan_only(self, claims: list[dict[str, Any]],
                  policies: Optional[dict] = None, *,
                  timeout: Optional[int] = None) -> dict[str, Any]:
        """Dry-run reconcile: the full endpoint plan (or precise conflict)
        WITHOUT opening any endpoint, holding anything, creating a run, or
        reading identities.  Same claims/policies shapes as ``acquire_run``;
        allowed even while a run holds the bench (it takes nothing)."""
        frame: dict[str, Any] = {"cmd": "acquire_run", "claims": claims,
                                 "dry_run": True}
        if policies:
            frame["policies"] = policies
        return self._request(frame, timeout or _TIMEOUT_REMOTE_EXEC)

    def release_run(self, run_id: str,
                    timeout: Optional[int] = None) -> dict[str, Any]:
        """Release the run handle; held devices STAY open (persistent session)."""
        return self._request({"cmd": "release_run", "run_id": run_id},
                             timeout or _TIMEOUT_REMOTE_EXEC)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Request an operator stop, never waiting: the frame is POSTED. Read
        during the run it names, the daemon acknowledges it in-stream ("ack")
        and the run returns aborted; read outside it (its run_test frame comes
        next, or the run ended first) it flags the run, unanswered — a run
        still to come aborts at once."""
        try:
            self._post({"cmd": "cancel_run", "run_id": run_id})
        except Exception as exc:  # noqa: BLE001 — no daemon or a dead pipe: report, don't raise
            return {"ok": False, "kind": "SessionDead", "error": f"cancel write failed: {exc}"}
        return {"ok": True, "run_id": run_id, "posted": True}

    # -- a guided run's Call (its function in the engine's own worker) ------
    def call_function(self, node_path: str, inputs: dict, procedure_json: dict, *, work_dir: Path,
                      stop: threading.Event, timeout: Optional[int] = None) -> dict[str, Any]:
        """Make the guided run's Call at ``node_path`` (the run's ``inputs``): the daemon runs it in
        a one-call functions worker in ``work_dir`` (the run's folder, its functions snapshot
        there). ``{"ok": true, "reading"}`` — the row's reading — or a typed failure: ``Stopped``
        when ``stop`` (the run's Stop, set before :meth:`cancel_call`) was set before the frame
        went out, or the daemon ended the function on it. The daemon streams a progress frame each
        half second while the function runs, so the per-frame deadline bounds a silent daemon,
        never a long function."""
        self._call_stop = stop
        try:
            return self._request({"cmd": "call", "node_path": node_path, "procedure_json": procedure_json,
                                  "work_dir": str(work_dir), **inputs}, timeout or _TIMEOUT_REMOTE_EXEC)
        except _CallNotSent:
            return {"ok": False, "kind": "Stopped", "error": "the call was stopped before it started"}
        finally:
            self._call_stop = None

    def cancel_call(self) -> None:
        """Stop the call in flight (its worker killed within a second; it answers ``Stopped``).
        POSTED, never answered: it never waits on the channel the call holds. The caller sets
        the call's ``stop`` first (see :meth:`_post`)."""
        try:
            self._post({"cmd": "cancel_call"})
        except Exception:  # noqa: BLE001 — no daemon or a dead pipe: no call to stop
            pass

    # -- the long exchange ---------------------------------------------------
    def run_test(self, run_id: str, module_path: str | Path,
                 results_dir: str | Path, *, on_output=None,
                 on_prompt=None, stop_at_first_fail: bool = False,
                 switch_off_on_stop: bool = True) -> dict[str, Any]:
        """Run one generated test in the holder.  BLOCKS (no per-frame
        timeout) until the daemon's final response.

        ``on_output(line)`` receives each streamed stdout line;
        ``on_prompt(text) -> str`` supplies the operator's answer (called on
        THIS thread; the reply is posted back as ``prompt_response`` from
        within the exchange).  Returns the daemon's final response — see
        ``_bench_session_runner`` for the ``result`` summary shape; verdicts
        are read from ``results.json`` in ``results_dir``.  A response with
        ``session_ended`` means the ladder ended the session (daemon exits);
        this channel is then marked dead.

        ``stop_at_first_fail`` / ``switch_off_on_stop`` ride the frame only
        when they differ from the defaults, so default frames are unchanged."""
        frame = {"cmd": "run_test", "run_id": run_id,
                 "module_path": str(module_path),
                 "results_dir": str(results_dir)}
        if stop_at_first_fail:
            frame["stop_at_first_fail"] = True
        if not switch_off_on_stop:
            frame["switch_off_on_stop"] = False
        with self._lock:
            if self._dead:
                return {"ok": False, "kind": "SessionDead",
                        "error": "remote session already dead",
                        "stderr": list(self._stderr_tail)}
            if self._proc is not None and not self._alive():
                # The holder died BETWEEN exchanges (e.g. killed). Lazy
                # respawn here would hand the run to a FRESH daemon holding
                # nothing (UnknownRun) and silently skip the host's
                # crash-recovery safe-off (design row 5) — report the crash.
                rc = self._proc.poll()
                return self._dead_resp(
                    "SessionDead", f"daemon died between exchanges (exit={rc})")
            try:
                if not self._alive():
                    self._start()
            except Exception as exc:  # noqa: BLE001 — ParserUnavailable included
                return self._dead_resp("SessionDead",
                                       f"failed to start daemon: {exc}")
            self.in_run_since, self.acked_at = time.monotonic(), None
            try:
                try:
                    self._post(frame)
                except (BrokenPipeError, OSError, ValueError) as exc:
                    return self._dead_resp("SessionDead", f"write failed: {exc}")
                while True:
                    try:
                        line = self._proc.stdout.readline()  # NO timeout: liveness = poll
                    except Exception as exc:  # noqa: BLE001
                        return self._dead_resp("SessionDead",
                                               f"read failed: {exc}")
                    if not line:
                        rc = self._proc.poll()
                        return self._dead_resp(
                            "SessionDead", f"daemon closed pipe (exit={rc})")
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError as exc:
                        return self._dead_resp("SessionDead",
                                               f"corrupt frame: {exc}")
                    kind = msg.get("kind") if isinstance(msg, dict) else None
                    if kind not in _STREAM_KINDS:   # the final response
                        if isinstance(msg, dict) and msg.get("session_ended"):
                            self._dead = True       # daemon exits after this
                            self._terminate()       # reap -> device handles freed
                        return msg
                    if kind == "output":
                        if on_output is not None:
                            try:
                                on_output(msg.get("text", ""))
                            except Exception:  # noqa: BLE001 — never break the run
                                pass
                    elif kind == "prompt":
                        answer = ""
                        self.prompting = True
                        if on_prompt is not None:
                            try:
                                answer = str(on_prompt(msg.get("text", "")))
                            except Exception:  # noqa: BLE001
                                answer = ""
                        try:
                            self._post({"cmd": "prompt_response",
                                        "id": msg.get("id"), "text": answer})
                        except (BrokenPipeError, OSError, ValueError) as exc:
                            return self._dead_resp("SessionDead",
                                                   f"write failed: {exc}")
                        self.prompting = False
                    elif kind == "ack" and msg.get("cmd") == "cancel_run" and self.acked_at is None:
                        self.acked_at = time.monotonic()
                    # "reject" / "progress": informational — consumed.
            finally:
                self.in_run_since, self.prompting = None, False
