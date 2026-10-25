"""The host side of one engine process: :class:`RemoteSession`, the persistent
NDJSON channel to ``engine_client/_execute_op_subprocess.py`` in the project venv (start,
one stdin writer, per-frame read timeout, stderr tail, venv claim, terminate and
reap). :mod:`.engine` owns the instances, one per project and role;
:mod:`.bench_client` adds the RUN frames; Studio's ``pack_parsers`` sends ops through it.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Optional

from procedo.core import run_provenance, temp_root
from . import project_venv

# Default timeouts (seconds).
_TIMEOUT_DEFAULT = 30
# Live single-op remote execution: connect + one driver call + close. Generous
# margin over a device's own VISA/serial timeout so a slow bench doesn't trip
# the transport before the instrument answers.
_TIMEOUT_REMOTE_EXEC = 45

# env-var overrides
_ENV_TIMEOUT = "PROCEDO_PACK_PARSER_TIMEOUT"


class ParserUnavailable(RuntimeError):
    """Raised when the deterministic parser/codegen path can't run."""


def _timeout() -> int:
    try:
        return int(os.environ.get(_ENV_TIMEOUT, _TIMEOUT_DEFAULT))
    except (ValueError, TypeError):
        return _TIMEOUT_DEFAULT


def _resolve_project_python(project_root: Path) -> project_venv.ProjectVenv:
    """The project's interpreter AND bundle dir; ParserUnavailable when there is none."""
    resolved = project_venv.venv(project_root)
    if resolved is None:
        raise ParserUnavailable(project_venv.why_no_venv(project_root))
    return resolved


def _in_flight(ops, done: int) -> str:
    """Which op of a batch the daemon was on: `` while running <device> <op>
    (op N of M)``; empty for a frame without ops or once the batch was over."""
    ops = ops or []
    if done + 1 >= len(ops):
        return ""
    op = ops[done + 1].get("op") or {}
    return (
        f" while running {op.get('device', '?')} {op.get('op', '?')} (op {done + 2} of {len(ops)})"
    )


class RemoteSession:
    """Persistent channel to one engine process (``engine_client/_execute_op_subprocess.py``
    in the project venv); :mod:`.engine` owns them, one per project and role.

    ``role`` is the daemon's job (``--role``). ``tools`` serves ops only. ``run``
    (as :class:`~.bench_client.BenchSessionClient`) keeps ``per_session`` devices
    OPEN across steps, so a held PSU output survives between ops — the whole
    reason the daemon exists; its holder (the guided dialog or the bench session)
    ends it with ``shutdown``, or on a wedge ``kill`` + :func:`safe_off_remote`.

    Thread-safe: a lock serialises requests (the daemon's loop is single-threaded),
    ``_post`` every write. Requests NEVER raise: ``{"ok": False, "kind": "SessionDead"|
    "SessionTimeout"|"ParserUnavailable", "error", "stderr"}`` lets the holder recover
    (fresh safe-off). ``_bundle_dir`` rides each frame, the document too (``procedure_json``).
    """

    def __init__(self, project_root: Path, role: str = "run") -> None:
        self._project_root = Path(project_root)
        self._role = role
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_tail: deque = deque(maxlen=80)
        self._lock = threading.Lock()
        self._write_lock = threading.RLock()  # one frame at a time on stdin, re-entrant: see _post
        self._dead = False
        self._bundle_dir: Optional[str] = None
        #: The bundle this process runs on, read where its venv is resolved (``_start``): what
        #: a bench session's runs record (``BenchSession.bundle``). None until it starts, or
        #: when the project folder did not answer then.
        self.bundle: Optional[run_provenance.BundleUsed] = None
        self._claim: Optional[tuple[Optional[Path], int]] = None  # (venv folder, pid) claimed

    # -- lifecycle ---------------------------------------------------------
    def _start(self) -> None:
        venv = _resolve_project_python(self._project_root)  # may raise ParserUnavailable
        self._bundle_dir = str(venv.bundle_dir)
        try:
            self.bundle = run_provenance.bundle_used(self._project_root)
        except OSError:
            self.bundle = None
        runner = Path(__file__).resolve().parent / "_execute_op_subprocess.py"
        # The child's temp files go with this app's, and so does a relative write of a
        # frame without a working folder (an exec_ops frame names the run's own).
        temp = str(temp_root.root())
        self._proc = subprocess.Popen(
            [str(venv.python), str(runner), "--role", self._role],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=temp,
            env={**os.environ, "TEMP": temp, "TMP": temp},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._release_claim()
        self._claim = (
            project_venv.claim_engine(self._project_root, self._proc.pid, self._role),
            self._proc.pid,
        )
        self._dead = False
        self._stderr_tail.clear()
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()

    def _post(self, frame: dict[str, Any]) -> None:
        """Write one frame, reading nothing back: the one stdin writer (no two frames interleave)."""
        with self._write_lock:
            self._proc.stdin.write(json.dumps({**frame, "_bundle_dir": self._bundle_dir}) + "\n")
            self._proc.stdin.flush()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stderr.readline, ""):
                self._stderr_tail.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            pass

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._dead

    def _terminate(self) -> None:
        """Terminate AND reap the daemon, so its serial/VISA handle is released
        before any fresh recovery daemon re-opens the same port."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)  # reap -> device handle freed
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _dead_resp(self, kind: str, error: str) -> dict[str, Any]:
        # dead == True is a HARD guarantee the proc is GONE (and its device handle
        # released) — so _session()'s lazy-recreate and the dialog's recovery
        # safe-off never open a 2nd process on a device the old daemon still holds.
        self._dead = True
        self._terminate()
        return {"ok": False, "kind": kind, "error": error, "stderr": list(self._stderr_tail)}

    # -- transport ---------------------------------------------------------
    def _request(self, frame: dict[str, Any], timeout: float, on_progress=None) -> dict[str, Any]:
        with self._lock:
            if self._dead:
                return {
                    "ok": False,
                    "kind": "SessionDead",
                    "error": "remote session already dead",
                    "stderr": list(self._stderr_tail),
                }
            try:
                if not self._alive():
                    self._start()
            except ParserUnavailable as exc:
                return self._dead_resp("ParserUnavailable", str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._dead_resp("SessionDead", f"failed to start daemon: {exc}")
            try:
                self._post(frame)
            except (BrokenPipeError, OSError, ValueError) as exc:
                return self._dead_resp("SessionDead", f"write failed: {exc}")

            # Read to the final frame past per-op ``progress`` frames; the timeout is per frame,
            # so a long batch with steady progress never wedges. ``on_progress`` runs on THIS thread.
            done = -1  # index of the last op the daemon finished
            while True:
                box: dict = {}

                def _read() -> None:
                    try:
                        box["line"] = self._proc.stdout.readline()
                    except Exception as exc:  # noqa: BLE001
                        box["err"] = exc

                reader = threading.Thread(target=_read, daemon=True)
                reader.start()
                reader.join(timeout)
                if reader.is_alive():  # wedged synchronous VISA read
                    return self._dead_resp(  # _dead_resp terminates+reaps
                        "SessionTimeout",
                        f"daemon unresponsive after {timeout}s"
                        + _in_flight(frame.get("ops"), done),
                    )
                if "err" in box or not box.get("line"):
                    rc = self._proc.poll()
                    return self._dead_resp("SessionDead", f"daemon closed pipe (exit={rc})")
                try:
                    msg = json.loads(box["line"])
                except json.JSONDecodeError as exc:
                    return self._dead_resp("SessionDead", f"corrupt frame: {exc}")
                if isinstance(msg, dict) and msg.get("kind") == "progress":
                    done = msg.get("i", done)
                    # Always consumed (every exec_ops batch streams them), callback or not: else a
                    # caller with no on_progress would get one as its result (the ⚡ single-op exec).
                    if on_progress is not None:
                        try:
                            on_progress(msg)
                        except Exception:  # noqa: BLE001 — never break the run
                            pass
                    continue
                return msg

    # -- API ---------------------------------------------------------------
    def exec_ops(
        self,
        ops: list[dict[str, Any]],
        bench_map: dict[str, Any],
        timeout: Optional[int] = None,
        on_progress=None,
        inputs: Optional[dict] = None,
        procedure_json: Optional[dict] = None,
        *,
        work_dir: Path,
    ) -> dict[str, Any]:
        """Run a contiguous batch of ``procedure_json`` (the holder's document; the
        daemon refuses a batch without one); per_session devices stay HELD afterward.
        The daemon runs it in ``work_dir`` (the guided run's folder), so a relative
        file write (a scope screenshot) lands there.

        ``on_progress`` (optional): invoked on the CALLING thread with each
        per-op ``{"kind": "progress", "node_path", "ok", "verdict", "i",
        "total"}`` frame the daemon streams, so the caller can advance a live
        cursor and paint the PASS/FAIL as the reading lands.

        ``inputs`` (optional): the run's inputs known BEFORE this batch
        (``measurements``, ``controls``, ``operator_verdicts``), as every engine
        call takes them. The daemon folds in each reading as it executes and
        materializes the live ``verdict`` via the same ``build_manual_run`` the
        caller uses at batch end (so they agree)."""
        frame: dict[str, Any] = {
            "cmd": "exec_ops",
            "ops": ops,
            "bench_map": bench_map,
            "procedure_json": procedure_json,
            "work_dir": str(work_dir),
            **(inputs or {}),
        }
        # The deadline is per frame and the daemon streams one progress frame
        # per op, so one op's budget bounds the wait however long the batch.
        return self._request(frame, timeout or _TIMEOUT_REMOTE_EXEC, on_progress=on_progress)

    def exec_op(
        self,
        target_op: dict[str, Any],
        bench: dict[str, Any],
        timeout: Optional[int] = None,
        procedure_json: Optional[dict] = None,
        *,
        work_dir: Path,
    ) -> dict[str, Any]:
        """⚡ single op — batch of one, unwrapped to the flat shape the dialog
        consumes: ``{"ok", "has_value", "value", "unit", "ref", "log"}``."""
        device = target_op.get("device")
        res = self.exec_ops(
            [{"node_path": "", "op": target_op}],
            {device: bench},
            timeout,
            procedure_json=procedure_json,
            work_dir=work_dir,
        )
        if not res.get("ok"):
            return res
        r0 = (res.get("results") or [{}])[0]
        if not r0.get("ok"):
            out = {
                "ok": False,
                "kind": "ExecError",
                "error": r0.get("error", "remote execution failed"),
                "log": res.get("log", []),
            }
            if res.get("unsafe"):
                out["unsafe"] = res["unsafe"]
            return out
        out = {
            "ok": True,
            "has_value": r0.get("has_value", False),
            "value": r0.get("value"),
            "unit": r0.get("unit", ""),
            "ref": r0.get("ref"),
            "log": res.get("log", []),
        }
        if res.get("unsafe"):
            out["unsafe"] = res["unsafe"]
        return out

    def safe_off(
        self,
        targets: list[dict[str, Any]],
        bench_map: dict[str, Any],
        timeout: Optional[int] = None,
        procedure_json: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Turn the given outputs OFF (ELOAD before PSU), keeping devices held.
        ``procedure_json``, when the holder has one, becomes the daemon's document."""
        return self._request(
            {
                "cmd": "safe_off",
                "safe_off": targets,
                "bench_map": bench_map,
                "procedure_json": procedure_json,
            },
            timeout or _TIMEOUT_REMOTE_EXEC,
        )

    def exec_raw_command(
        self,
        device: str,
        etype: str,
        subtype: str,
        text: str,
        bench: dict[str, Any],
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """Send ONE raw command line to ``device`` over the live session and
        return ``{"ok", "response", "log"}`` (or a typed failure).  ``etype`` is
        the DECLARED equipment type + ``subtype`` (the daemon resolves the pack
        namespace from them — controller->fncore).  Reuses the device if a step
        already armed it (held), else opens a transient link — the same connect
        semantics as a per_step op.  ``"?"`` in ``text`` makes it a query (reply
        returned); otherwise it is a write (empty reply)."""
        return self._request(
            {
                "cmd": "raw",
                "device": device,
                "etype": etype,
                "subtype": subtype,
                "text": text,
                "bench_map": {device: bench},
            },
            timeout or _TIMEOUT_REMOTE_EXEC,
        )

    def engine_op(self, op: str, timeout: Optional[int] = None, **fields: Any) -> dict[str, Any]:
        """One engine op (``engine_client/_engine_ops.py``) in this live daemon, with its
        document in ``fields`` (``procedure_json``). Returns the op's result
        dict, or the session's typed failure."""
        return self._request({"cmd": "op", "op": op, **fields}, timeout or _timeout())

    def ping(self, timeout: float = 5) -> dict[str, Any]:
        return self._request({"cmd": "ping"}, timeout)

    def shutdown(self, timeout: Optional[int] = None) -> dict[str, Any]:
        """Safe-off all held + close (unlock) + exit; idempotent."""
        res = {"ok": True, "log": []}
        if self._proc is not None and not self._dead:
            res = self._request({"cmd": "shutdown"}, timeout or (_TIMEOUT_REMOTE_EXEC + 30))
            try:
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self._terminate()
        self._dead = True
        self._release_claim()
        return res

    def kill(self) -> None:
        """Hard-stop a wedged daemon (the parent's last-resort)."""
        self._dead = True
        self._terminate()
        self._release_claim()

    def close(self, grace: float = 5) -> None:
        """End the daemon as this process's exit would: close its stdin, so it tears
        down and exits on its own (a venv launcher only after its python); kill it
        if it is still there after ``grace`` seconds."""
        self._dead = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=grace)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self._terminate()
        self._release_claim()

    def _release_claim(self) -> None:
        if self._claim is not None:
            project_venv.release_engine(*self._claim, self._role)
            self._claim = None

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def alive(self) -> bool:
        """Its daemon was started and is still running."""
        return self._alive()

    @property
    def ended(self) -> bool:
        """Ended by its holder, or its process is gone: it can never serve again."""
        return self._dead or (self._proc is not None and self._proc.poll() is not None)
