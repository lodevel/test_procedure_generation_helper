"""Bench-session client channel (S3): the host side of the run-handle frames.

Thin subclass of :class:`~procedo.studio.pack_ops.pack_parsers.RemoteSession`
adding ``acquire_run`` / ``plan_only`` (dry-run reconcile) / ``run_test`` /
``cancel_run`` / ``release_run``.
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
* ``prompt_response`` and an in-run ``cancel_run`` are written from WITHIN
  the in-flight ``run_test`` exchange; one dedicated write lock serialises
  those two writers (the GUI stop button vs. the run loop's prompt answer).
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any, Optional

from .pack_parsers import RemoteSession, _TIMEOUT_REMOTE_EXEC

# Daemon->host stream frames run_test consumes before its final response.
# Anything NOT in this set is the final response (error kinds like
# "ExecError" never collide — the daemon only tags stream frames).
_STREAM_KINDS = frozenset({"output", "prompt", "ack", "reject", "progress"})


class BenchSessionClient(RemoteSession):
    """One persistent bench-holder channel speaking the S3 run-handle frames."""

    def __init__(self, procedure_path: Path | str, project_root: Path) -> None:
        super().__init__(procedure_path, project_root)
        self._write_lock = threading.Lock()   # prompt_response vs in-run cancel
        self._in_run = False

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

    def cancel_run(self, run_id: str,
                   timeout: Optional[int] = None) -> dict[str, Any]:
        """Request an operator stop.  While a ``run_test`` is in flight the
        frame is POSTED into that exchange (the daemon acknowledges with an
        in-stream "ack" frame and the run returns aborted); idle it is a
        normal request/response."""
        if self._in_run:
            try:
                self._post({"cmd": "cancel_run", "run_id": run_id})
                return {"ok": True, "run_id": run_id, "posted": True}
            except Exception as exc:  # noqa: BLE001 — dead pipe: report, don't raise
                return {"ok": False, "kind": "SessionDead",
                        "error": f"cancel write failed: {exc}"}
        return self._request({"cmd": "cancel_run", "run_id": run_id},
                             timeout or _TIMEOUT_REMOTE_EXEC)

    # -- in-flight writer ----------------------------------------------------
    def _post(self, frame: dict[str, Any]) -> None:
        """Write one frame into the in-flight run_test exchange (no response
        read here — the run loop consumes everything the daemon emits)."""
        payload = {**frame, "_bundle_dir": self._bundle_dir,
                   "procedure_path": self._procedure_path}
        with self._write_lock:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

    # -- the long exchange ---------------------------------------------------
    def run_test(self, run_id: str, module_path: str | Path,
                 results_dir: str | Path, *, on_output=None,
                 on_prompt=None) -> dict[str, Any]:
        """Run one generated test in the holder.  BLOCKS (no per-frame
        timeout) until the daemon's final response.

        ``on_output(line)`` receives each streamed stdout line;
        ``on_prompt(text) -> str`` supplies the operator's answer (called on
        THIS thread; the reply is posted back as ``prompt_response`` from
        within the exchange).  Returns the daemon's final response — see
        ``_bench_session_runner`` for the ``result`` summary shape; verdicts
        are read from ``results.json`` in ``results_dir``.  A response with
        ``session_ended`` means the ladder ended the session (daemon exits);
        this channel is then marked dead."""
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
            self._in_run = True
            try:
                try:
                    self._post({"cmd": "run_test", "run_id": run_id,
                                "module_path": str(module_path),
                                "results_dir": str(results_dir)})
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
                    # "ack" / "reject" / "progress": informational — consumed.
            finally:
                self._in_run = False
