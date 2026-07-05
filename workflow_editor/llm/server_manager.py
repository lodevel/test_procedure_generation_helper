"""
OpenCode Server Manager - Manages the shared OpenCode WSL server process.

This module provides a singleton-like manager for the OpenCode server process.
ONE server instance is shared across all tabs, while each tab has its own session.
"""

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

from .mcp_config import win_to_wsl_path
from .opencode_backend import OpenCodeConfig, safe_wsl_cwd
from .server_health import (
    ServerError,
    ServerStatus,
    classify_install,
    find_free_port,
    is_port_conflict,
)

log = logging.getLogger(__name__)


class PidIdentity(Enum):
    """Tri-state verdict for a recorded pid's identity in the orphan sweep.

    The third state is the load-bearing one: a /proc read that times out or
    hits a WSL hiccup is INCONCLUSIVE, not "not ours". Treating inconclusive as
    "not ours" would delete the pid file and permanently lose a still-running
    orphan. On UNKNOWN the sweep keeps the pid file and retries next launch.
    """

    OURS = "ours"          # live, an opencode serve, carries OUR config → kill
    NOT_OURS = "not_ours"  # definitely dead/recycled/unrelated → safe to forget
    UNKNOWN = "unknown"    # read error / timeout / WSL hiccup → keep, retry later


def fetch_opencode_models(server_url: str, timeout: float = 2.0) -> list:
    """Query a running OpenCode server's ``/config`` and return its available
    ``<providerID>/<modelID>`` ids (sorted, de-duped).

    Returns ``[]`` when the server is unreachable or isn't OpenCode (a JSON
    signature check on ``/config``: the web UI / any stray server answers HTML on
    /health, but the /config API returns a JSON object).
    Pure + side-effect-free so the Settings model picker can call it directly.
    """
    try:
        resp = requests.get(
            f"{server_url}/config",
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        if "application/json" not in resp.headers.get("Content-Type", ""):
            return []
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    models = set()
    providers = data.get("provider") or {}
    if isinstance(providers, dict):
        for provider_id, info in providers.items():
            provider_models = (info or {}).get("models") or {}
            if isinstance(provider_models, dict):
                for model_id in provider_models:
                    models.add(f"{provider_id}/{model_id}")
    return sorted(models)


class OpenCodeServerManager:
    """
    Manages the OpenCode WSL server lifecycle.
    
    This class is responsible for:
    - Starting the OpenCode server in WSL
    - Monitoring server health
    - Stopping the server on application exit
    
    Design:
    - ONE server instance per application (not per tab)
    - Multiple sessions can connect to the same server
    - Thread-safe server start/stop operations
    
    Usage:
        manager = OpenCodeServerManager(config)
        if manager.start():
            # Server is running at manager.server_url
            # Create sessions via POST to /session endpoint
            pass
        manager.stop()  # Call on app exit
    """
    
    def __init__(self, config: Optional[OpenCodeConfig] = None):
        """
        Initialize the server manager with OpenCode configuration.
        
        Args:
            config: OpenCode configuration. Uses defaults if not provided.
        """
        self._config = config or OpenCodeConfig()
        self._server_process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = False
        # Single-use retirement marker (set by stop() BEFORE the lock). A retired
        # manager is permanent: start() refuses to launch and an in-flight boot's
        # _wait_for_server() aborts. This kills the prewarm/manager-swap orphan
        # race — a stale prewarm daemon on a retired manager never spawns — and
        # lets a stop() during boot interrupt the wait instead of freezing the UI.
        self._retired = False
        # Last classified outcome of start()/is_available(), read by the backend
        # factory to show a clear reason to the user. None until the first check.
        self.last_status: Optional[ServerStatus] = None
        # Drained server stderr (tail) for the failure reason; the drain thread
        # prevents the pipe-buffer deadlock a chatty server would otherwise hit.
        self._stderr_tail: "deque[str]" = deque(maxlen=50)
        self._stderr_thread: Optional[threading.Thread] = None
    
    @property
    def config(self) -> OpenCodeConfig:
        """Get the current configuration."""
        return self._config
    
    @property
    def server_url(self) -> str:
        """
        Get the server URL (http://host:port).
        
        Returns:
            Server URL string, e.g., "http://127.0.0.1:4096"
        """
        return self._config.server_url
    
    def _process_is_alive_locked(self) -> bool:
        """REAL process liveness — the caller MUST already hold ``self._lock``.

        ``self._running`` alone is a stale flag: a server that CRASHES mid-session
        leaves it True forever (the process object lingers) so the picker reports
        "running" while nothing answers. The load-bearing check is
        ``poll() is None`` — a crashed/exited process polls to its return code and
        reads here as NOT alive, and we self-heal the stale state (drop the dead
        process + flag) so a later ``start()`` relaunches instead of short-circuiting
        on a corpse.
        """
        if not self._running or self._server_process is None:
            return False
        # poll() returns None only while the process is still running; any int
        # (an exit code, incl. a crash signal) means it has terminated.
        if self._server_process.poll() is not None:
            log.warning("Server process terminated unexpectedly")
            self._running = False
            self._server_process = None
            return False
        return True

    @property
    def is_running(self) -> bool:
        """
        Check if the server is running (NON-BLOCKING process poll).

        This checks both the internal state AND verifies the process
        is still alive.

        ``start()``/``stop()`` hold ``self._lock`` for the WHOLE boot — up to
        ``startup_timeout`` (~30s cold, longer on port-conflict retries). A
        UI-thread liveness poll (the 5s health tick, the settings poll, the
        indicator refresh) must NEVER block on that lock, so we try-acquire
        and report not-running when a start/stop is in flight. A genuinely
        running server holds the lock only for the microsecond of
        ``_process_is_alive_locked()``, so the try-acquire effectively always
        succeeds for it; only an in-flight boot is reported as not-yet-running,
        which is the truthful answer (it cannot serve requests mid-boot) and
        self-corrects on the next poll once the lock is free.

        Returns:
            True if server process is running.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            return self._process_is_alive_locked()
        finally:
            self._lock.release()

    @property
    def is_alive(self) -> bool:
        """Strong, end-to-end liveness: OUR process is alive AND the HTTP
        ``/health`` endpoint answers 200.

        ``is_running`` only proves the OS process exists; a server can be a live
        process yet wedged (not accepting requests). ``is_alive`` is the verdict
        the app-level "running / down" signal and the auto-recovery poll trust —
        a crashed OR hung server both read as NOT alive, triggering a relaunch.
        The HTTP probe is fast (2s) and runs OUTSIDE the lock so a slow/hung
        server never blocks ``stop()`` / ``start()`` on the lock.
        """
        if not self.is_running:
            return False
        return self.health_check()

    def _force_stop_for_relaunch(self) -> None:
        """Tear down a wedged process so the next ``start()`` respawns — withOUT
        retiring the manager (unlike ``stop()``, which sets ``_retired=True`` and
        would permanently block relaunch). Runs under the lock so it is ordered
        against an in-flight boot."""
        with self._lock:
            self._running = False
            self._stop_process()

    def ensure_running(self) -> bool:
        """Idempotent self-heal: return True if the server is already alive,
        otherwise (re)launch it via ``start()`` and report the outcome.

        This is the single auto-recovery seam — the app-level liveness poll and
        the model picker both call it on a daemon thread when the server reads as
        down. The down verdict is ``is_alive`` (process up AND ``/health`` 200),
        so a WEDGED server (process alive but ``/health`` dead) also reads as
        down here. ``start()``'s reuse-guard is a process poll only, so it would
        short-circuit on a wedged-but-alive corpse and never respawn — we
        proactively tear it down first so ``start()`` sees a dead handle and
        relaunches. A retired manager stays retired (``start()`` refuses); the
        caller must build a fresh manager in that case.

        ``is_alive`` reads False for EITHER "process dead" OR "a start/stop is in
        flight" (``is_running`` try-acquires ``self._lock`` and reports
        not-running while the lock is held). We must NOT relaunch on the latter:
        another thread is already booting this server, and barging in would only
        block on the lock and then redundantly respawn / fight the in-flight
        boot. So we disambiguate up front: if the lock is busy, a start/stop is
        in flight — treat it as "recovery already in progress" and bail, letting
        the caller's next poll re-check once the boot settles.
        """
        if self.is_alive:
            return True
        # Disambiguate "dead" from "lock busy (a start/stop is in flight)": a
        # non-blocking probe of the SAME lock start()/stop() hold for the whole
        # boot. Busy -> a relaunch is already under way; do not stack another.
        if not self._lock.acquire(blocking=False):
            log.debug(
                "ensure_running: a start/stop is already in flight — deferring "
                "to it instead of relaunching")
            return False
        self._lock.release()
        # Wedge guard: process alive but /health dead -> kill the corpse so
        # start()'s poll-only reuse-guard does not short-circuit on it.
        if self.is_running:
            log.warning(
                "Server wedged (process alive, /health dead) — tearing down "
                "before relaunch")
            self._force_stop_for_relaunch()
        log.info("Server found down — attempting auto-recovery (relaunch)")
        return self.start()

    # Number of fresh-port spawn attempts before giving up on a port conflict.
    # A Windows-side free-port probe and the WSL2 bind namespace can disagree
    # (C5): the probed port looks free on Windows yet is taken inside WSL. Each
    # retry OS-assigns a brand-new port, so a probe/bind mismatch self-recovers
    # instead of dead-ending the whole chat backend.
    _PORT_RETRY_ATTEMPTS = 4

    def start(self) -> bool:
        """
        Start OUR OWN server if not already running.

        We NEVER reuse an external/stray server — process ownership is the only
        marker of "running". The sequence is:
        1. If our own process is alive, return True (session reuse).
        2. Sweep any orphan we left behind from a crashed prior session.
        3. Verify WSL + opencode are installed.
        4. Spawn ``opencode serve`` (retrying with a fresh OS-assigned port on a
           port conflict), pointed at our launch config via ``OPENCODE_CONFIG``.

        Returns:
            True if our server is running (started or already ours).
            False if it failed to start.
        """
        with self._lock:
            # A retired manager NEVER launches — this also catches a stale
            # prewarm daemon thread that only now enters start() on a manager
            # that stop() already retired (the orphan race). Retirement is
            # permanent; the caller builds a FRESH manager for a new server.
            if self._retired:
                log.debug("start() on a retired manager; refusing to launch")
                return False

            # Our-own session reuse — but only if the process is ACTUALLY alive.
            # _process_is_alive_locked() polls the process: a server that crashed
            # mid-session no longer counts as "running", so we fall through and
            # relaunch instead of returning True on a dead process (the bug where
            # a crashed server was never auto-restarted). It also self-heals the
            # stale flag/handle so the orphan-sweep + respawn below run cleanly.
            if self._process_is_alive_locked():
                log.debug("Server already running")
                return True

            # Pre-flight: a missing WSL / opencode gives a precise reason
            # instead of an opaque spawn failure later.
            diag = self._diagnose_installation()
            if not diag.ok:
                self.last_status = diag
                log.error(f"OpenCode unavailable: {diag.message}")
                return False

            # Kill any of OUR leftovers from a crashed prior session (PID-file
            # gated; never a blind pkill-by-name) before binding a new port.
            self._sweep_orphan()

            log.info("Starting OpenCode server...")
            last_err = ServerError.START_FAILED
            last_detail = ""
            # Retry with a FRESH OS-assigned port on a port conflict (C5).
            for attempt in range(1, self._PORT_RETRY_ATTEMPTS + 1):
                try:
                    # OS-assign the port (Q1) and propagate it to the shared
                    # config so backends POST to the right URL.
                    port = find_free_port(0, self._config.server_hostname)
                    self._config.server_port = port

                    cmd = [self._config.wsl_path, "bash", "-ic",
                           self._build_serve_command(port)]
                    log.debug(f"Server command (attempt {attempt}): {' '.join(cmd)}")

                    # stdout discarded; stderr PIPE is drained by a thread so the
                    # pipe buffer never fills and deadlocks the server.
                    self._server_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        # Launch from the editor launch dir (a translatable
                        # Windows path). The config is ALSO passed explicitly via
                        # OPENCODE_CONFIG (C1) so we never rely on cwd. Fall back
                        # to the system drive — NEVER the inherited cwd, which may
                        # be a non-translatable drive (e.g. L:).
                        cwd=self._config.working_directory or safe_wsl_cwd(),
                    )
                    self._start_stderr_drain()
                    log.debug(f"Server process started, PID: {self._server_process.pid}")
                    # Record OUR process so a crashed next session can sweep it.
                    self._write_pid_file(self._server_process.pid, port)

                    if not self._wait_for_server():
                        detail = "".join(self._stderr_tail).strip()
                        died = self._server_process.poll() is not None
                        if died and is_port_conflict(detail):
                            err = ServerError.PORT_IN_USE
                        elif died:
                            err = ServerError.START_FAILED
                        else:
                            err = ServerError.START_TIMEOUT
                        self.last_status = ServerStatus.failure(err, detail)
                        log.error(f"Server failed to become ready: {self.last_status.message}")
                        self._stop_process()
                        # Only a port conflict is worth a fresh-port retry; a
                        # timeout or hard start failure won't change on rebind.
                        if err is ServerError.PORT_IN_USE and attempt < self._PORT_RETRY_ATTEMPTS:
                            last_err, last_detail = err, detail
                            log.warning(
                                f"Port {port} conflicted; retrying with a fresh port "
                                f"(attempt {attempt + 1}/{self._PORT_RETRY_ATTEMPTS})"
                            )
                            continue
                        return False

                    self._running = True
                    self.last_status = ServerStatus.healthy()
                    log.info(f"OpenCode server started successfully at {self.server_url}")
                    return True

                except Exception as e:
                    self.last_status = ServerStatus.failure(ServerError.START_FAILED, str(e))
                    log.error(f"Failed to start server: {e}")
                    self._stop_process()
                    return False

            # Exhausted the retries on repeated port conflicts.
            self.last_status = ServerStatus.failure(last_err, last_detail)
            return False

    def _build_serve_command(self, port: int) -> str:
        """Build the ``opencode serve`` shell command (run via ``bash -ic`` so
        the user's PATH resolves ``opencode``).

        Two inline env vars (wsl.exe does not forward Windows env without WSLENV):

        - ``OPENCODE_ENABLE_EXA=1`` turns on the keyless Exa-backed websearch
          tool for non-OpenCode providers. The tool is only EXPOSED per-request
          via the message body's ``tools`` override, so this is availability,
          not usage.
        - ``OPENCODE_CONFIG=<launch_dir_wsl>/opencode.json`` (C1) points OpenCode
          at OUR derived launch config EXPLICITLY — we do NOT rely on cwd, which
          was the likely cause of bare config-less servers. The launch dir comes
          from ``config.working_directory`` (a Windows path set by main_window to
          ``build_launch_config``'s return), translated to its ``/mnt/c`` WSL
          form. When no launch dir is set we omit OPENCODE_CONFIG and fall back
          to OpenCode's own discovery.
        """
        # Every value that lands in the bash -ic string is shell-quoted so a
        # space or metacharacter in the home/launch path (or a crafted hostname)
        # can neither break the command nor inject a second one. The env-var
        # names + `opencode serve` literal are fixed and need no quoting.
        env = "OPENCODE_ENABLE_EXA=1 "
        launch_dir = self._config.working_directory
        if launch_dir:
            cfg_wsl = win_to_wsl_path(str(launch_dir)).rstrip("/") + "/opencode.json"
            env += f"OPENCODE_CONFIG={shlex.quote(cfg_wsl)} "
        return (
            f"{env}opencode serve --port {int(port)} "
            f"--hostname {shlex.quote(str(self._config.server_hostname))}"
        )
    
    def stop(self) -> None:
        """
        Stop the server if running.
        
        This method:
        1. Terminates the server process gracefully
        2. Force kills if termination times out
        3. Cleans up internal state
        
        Safe to call multiple times.

        Retirement is set BEFORE the lock so a boot already holding the lock
        (mid ``_wait_for_server``) observes it and aborts within one poll tick
        instead of blocking the caller for the full startup timeout; a prewarm
        thread that has not yet entered ``start()`` will refuse to launch.
        """
        self._retired = True
        with self._lock:
            self._running = False
            self._stop_process()
    
    def health_check(self) -> bool:
        """
        Check server health by calling the health endpoint.
        
        This performs an HTTP health check to verify the server
        is responsive and accepting requests.
        
        Returns:
            True if server is healthy and responsive.
            False if server is not responding or returned an error.
        """
        if not self.is_running:
            return False
        
        try:
            response = requests.get(
                f"{self.server_url}/health",
                timeout=2
            )
            healthy = response.status_code == 200
            if healthy:
                log.debug("Server health check passed")
            else:
                log.warning(f"Server health check failed: status {response.status_code}")
            return healthy
        except requests.exceptions.RequestException as e:
            log.warning(f"Server health check failed: {e}")
            return False
    
    def is_available(self) -> bool:
        """
        Check whether OpenCode can be USED — i.e. WSL and the ``opencode``
        command are installed. This is an INSTALL check only; it does NOT probe
        for (and never reuses) a running server. ``start()`` always launches our
        own.

        Returns:
            True if OpenCode is installed and could be launched.
        """
        log.debug("Checking if OpenCode is available...")
        diag = self._diagnose_installation()
        self.last_status = diag
        return diag.ok

    def _diagnose_installation(self) -> ServerStatus:
        """Probe WSL + opencode and classify the outcome, capturing stderr as
        the failure detail. ``ok`` when both are present."""
        detail = ""
        try:
            result = subprocess.run(
                [self._config.wsl_path, "--version"],
                capture_output=True, text=True, timeout=5,
                cwd=safe_wsl_cwd(),
            )
            wsl_ok = result.returncode == 0
            if not wsl_ok:
                detail = (result.stderr or "").strip()
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return ServerStatus.failure(ServerError.WSL_MISSING, str(e))

        opencode_ok = False
        if wsl_ok:
            try:
                result = subprocess.run(
                    [self._config.wsl_path, "bash", "-ic", "opencode --version"],
                    capture_output=True, text=True, timeout=5,
                    cwd=safe_wsl_cwd(),
                )
                opencode_ok = result.returncode == 0
                if not opencode_ok:
                    detail = (result.stderr or "").strip()
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                return ServerStatus.failure(ServerError.OPENCODE_MISSING, str(e))

        err = classify_install(wsl_ok, opencode_ok)
        return ServerStatus.healthy() if err is ServerError.NONE \
            else ServerStatus.failure(err, detail)

    def _start_stderr_drain(self) -> None:
        """Drain the server's stderr into a bounded tail on a daemon thread —
        keeps the pipe from filling (which would deadlock the server) and keeps
        the last lines available as a failure reason."""
        proc = self._server_process
        if proc is None or proc.stderr is None:
            return
        # Fresh per-spawn tail so a lingering drain thread from a previous spawn
        # can't write into this spawn's reason blob.
        tail: "deque[str]" = deque(maxlen=50)
        self._stderr_tail = tail

        def _drain() -> None:
            try:
                for line in proc.stderr:
                    tail.append(line)
            except Exception:  # noqa: BLE001 — best-effort drain
                pass

        self._stderr_thread = threading.Thread(
            target=_drain, daemon=True, name="opencode-stderr-drain"
        )
        self._stderr_thread.start()
    
    def _wait_for_server(self) -> bool:
        """
        Wait for server to be ready by polling health endpoint.
        
        Returns:
            True if server became ready within timeout.
            False if timeout expired or process died.
        """
        log.debug("Waiting for server to be ready...")
        start_time = time.time()
        attempt = 0
        
        while time.time() - start_time < self._config.startup_timeout:
            # A stop() during boot retires us — bail within one tick so the
            # synchronous wait never holds the caller (e.g. a swap on the UI
            # thread) for the full timeout. start() then _stop_process()es the
            # half-started server on this False return.
            if self._retired:
                log.debug("_wait_for_server: manager retired mid-wait; aborting")
                return False
            attempt += 1
            log.debug(f"Health check attempt {attempt}...")
            try:
                response = requests.get(
                    f"{self.server_url}/health",
                    timeout=1
                )
                if response.status_code == 200:
                    log.debug(f"Server ready after {attempt} attempts")
                    return True
            except requests.exceptions.RequestException as e:
                # Expected while the server is still booting; keep polling.
                log.debug(f"Health check attempt {attempt} not ready yet: {e}")
            
            # Check if process died (stderr is captured by the drain thread).
            if self._server_process and self._server_process.poll() is not None:
                log.error("Server process died during startup")
                return False
            
            time.sleep(0.5)
        
        log.error(f"Server failed to start within {self._config.startup_timeout}s timeout")
        return False
    
    def _stop_process(self) -> None:
        """
        Stop the server process if we own it.
        
        Note: If we attached to an external server (_server_process is None),
        this method does nothing - we don't stop servers we didn't start.
        """
        if self._server_process is None:
            return
        
        log.debug(f"Stopping server process PID {self._server_process.pid}...")
        try:
            self._server_process.terminate()
            try:
                self._server_process.wait(timeout=5)
                log.debug("Server process terminated gracefully")
            except subprocess.TimeoutExpired:
                log.warning("Server didn't terminate gracefully, killing...")
                self._server_process.kill()
                self._server_process.wait(timeout=2)
        except Exception as e:
            log.error(f"Error stopping server process: {e}")
        finally:
            self._server_process = None

        # Terminating wsl.exe can leave the Linux-side `opencode serve` running
        # (a WSL relay gotcha). We reach here only when WE started the server
        # (the early return above guards the attached case), so best-effort kill
        # it by OUR exact port — never the user's manually-launched one — so it
        # doesn't outlive the editor on close OR crash (via the atexit hook).
        try:
            subprocess.run(
                [self._config.wsl_path, "bash", "-ic",
                 f"pkill -f 'opencode serve --port {self._config.server_port}'"],
                capture_output=True, timeout=3, cwd=safe_wsl_cwd(),
            )
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            log.debug(f"WSL-side pkill of opencode serve failed (best-effort): {e}")

        # Our process is gone — drop the PID file so the next session's
        # orphan-sweep has nothing stale to chase.
        self._remove_pid_file()

    # ------------------------------------------------------------------ #
    # Orphan-sweep via a PID file (C3)
    #
    # On a clean exit ``stop()`` tears the process down. But a crash / hard kill
    # of the editor can leave the Linux-side ``opencode serve`` alive. We record
    # OUR pid+port in ``<launch_dir>/opencode.pid`` after a successful spawn, and
    # at the START of the next ``start()`` read it back and kill that process —
    # but ONLY after confirming the live pid is an ``opencode serve`` launched
    # with OUR ``OPENCODE_CONFIG`` (the launch-dir opencode.json), checked
    # against /proc cmdline+environ. We NEVER pkill-by-name blindly: the recorded
    # pid may have been recycled to an unrelated process, and the user may run
    # their own opencode pointed at a DIFFERENT config.
    # ------------------------------------------------------------------ #

    def _pid_file_path(self) -> Optional[Path]:
        """Path to OUR pid file (``<launch_dir>/opencode.pid``) or ``None`` when
        no launch dir is configured (the file lives beside the derived config so
        each editor install tracks only its own server)."""
        launch_dir = self._config.working_directory
        if not launch_dir:
            return None
        return Path(launch_dir) / "opencode.pid"

    def _write_pid_file(self, pid: int, port: int) -> None:
        """Record OUR spawned server as JSON ``{pid, port, started_at}`` so a
        crashed next session can sweep it. Best-effort — a write failure must
        not abort an otherwise-healthy launch."""
        path = self._pid_file_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"pid": pid, "port": port, "started_at": time.time()}),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001 — best-effort bookkeeping
            log.debug(f"Could not write pid file {path}: {e}")

    def _remove_pid_file(self) -> None:
        """Delete OUR pid file on a clean stop (best-effort)."""
        path = self._pid_file_path()
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 — best-effort
            log.debug(f"Could not remove pid file {path}: {e}")

    def _read_pid_file(self) -> Optional[dict]:
        """Read OUR pid file, or ``None`` when it is absent / unreadable / not a
        JSON object carrying an int ``pid``."""
        path = self._pid_file_path()
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
            return None
        return data

    def _our_config_wsl_path(self) -> Optional[str]:
        """The WSL ``OPENCODE_CONFIG`` path THIS manager launches with
        (``<launch_dir_wsl>/opencode.json``), or ``None`` when no launch dir is
        set. This is OUR launch identity: the orphan sweep requires the recorded
        pid's cmdline/environ to carry exactly this path before it kills."""
        launch_dir = self._config.working_directory
        if not launch_dir:
            return None
        return win_to_wsl_path(str(launch_dir)).rstrip("/") + "/opencode.json"

    # Sentinels the identity probe emits so we can tell a DEAD pid apart from a
    # WSL-level read failure: a clean run prints ALIVE/DEAD; if neither shows up
    # the shell itself failed and the verdict is UNKNOWN (never NOT_OURS).
    _ID_ALIVE = "__OC_ALIVE__"
    _ID_DEAD = "__OC_DEAD__"
    _ID_SEP = "__OC_ENV__"

    def _pid_is_our_opencode(self, pid: int) -> "PidIdentity":
        """Tri-state identity of the recorded ``pid`` (a Linux pid) in WSL.

        - ``OURS``: the LIVE process is an ``opencode serve`` AND was launched
          with OUR ``OPENCODE_CONFIG`` (the launch-dir ``opencode.json``),
          matched as an EXACT token (not a substring) against its cmdline and
          environ — so a sibling config like ``<our_cfg>.backup`` never matches
          and the USER's own opencode at a DIFFERENT config is spared.
        - ``NOT_OURS``: the pid is definitely gone/recycled, OR it is alive but
          not an opencode serve / not carrying our config — safe to forget.
        - ``UNKNOWN``: the /proc read timed out or WSL hiccuped — INCONCLUSIVE.
          The caller must keep the pid file and retry; treating this as
          NOT_OURS would permanently lose a still-running orphan.

        When this manager has no launch dir (no config identity to assert), we
        fall back to a bare ``opencode serve`` token check — the strongest
        marker available, and the pid file is per-launch-dir anyway."""
        our_cfg = self._our_config_wsl_path()
        # Probe pid liveness explicitly (ALIVE/DEAD marker) so a dead pid is a
        # DEFINITE verdict, and emit cmdline + environ NUL-delimited so we can
        # split into exact tokens. One wsl.exe round-trip.
        script = (
            f"if [ -e /proc/{int(pid)} ]; then echo {self._ID_ALIVE}; "
            f"else echo {self._ID_DEAD}; fi; "
            f"cat /proc/{int(pid)}/cmdline 2>/dev/null; echo; "
            f"echo {self._ID_SEP}; "
            f"cat /proc/{int(pid)}/environ 2>/dev/null"
        )
        try:
            result = subprocess.run(
                [self._config.wsl_path, "bash", "-ic", script],
                capture_output=True, text=True, timeout=3, cwd=safe_wsl_cwd(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return PidIdentity.UNKNOWN
        out = result.stdout or ""
        # The shell ran but produced neither marker → WSL-level failure, not a
        # statement about the pid. Inconclusive.
        if self._ID_ALIVE not in out and self._ID_DEAD not in out:
            return PidIdentity.UNKNOWN
        if self._ID_DEAD in out:
            return PidIdentity.NOT_OURS  # /proc/<pid> absent → definitely gone

        # Split the NUL-delimited cmdline + environ into exact tokens.
        cmdline_blob, _, environ_blob = out.partition(self._ID_SEP)
        # Drop the ALIVE marker line from the cmdline blob.
        cmdline_blob = cmdline_blob.split("\n", 1)[-1]
        # Split on NUL, then strip the surrounding-newline noise the `echo`
        # separators inject (argv/environ tokens carry no real newlines) so the
        # EXACT-token compare below sees clean tokens.
        cmd_tokens = [t.strip("\r\n") for t in cmdline_blob.split("\0")]
        cmd_tokens = [t for t in cmd_tokens if t]
        env_tokens = [t.strip("\r\n") for t in environ_blob.split("\0")]
        env_tokens = [t for t in env_tokens if t]

        # Must be an `opencode serve` — adjacent exact argv tokens, not a
        # substring of some unrelated command line.
        if not self._is_opencode_serve(cmd_tokens):
            return PidIdentity.NOT_OURS
        if our_cfg is None:
            # No config identity to assert — the serve check is the marker.
            return PidIdentity.OURS
        # EXACT-token config match (MAJOR-2): the environ token must equal
        # `OPENCODE_CONFIG=<our_cfg>` exactly (so `<our_cfg>.backup` is NOT a
        # match), or argv must carry `--config <our_cfg>` / `--config=<our_cfg>`
        # as exact tokens. No substring containment.
        if f"OPENCODE_CONFIG={our_cfg}" in env_tokens:
            return PidIdentity.OURS
        if self._argv_has_config(cmd_tokens, our_cfg):
            return PidIdentity.OURS
        return PidIdentity.NOT_OURS

    @staticmethod
    def _is_opencode_serve(cmd_tokens: list) -> bool:
        """True iff argv contains the adjacent exact tokens ``opencode serve``
        (the basename may be a full path like ``/usr/bin/opencode``)."""
        for i in range(len(cmd_tokens) - 1):
            tok = cmd_tokens[i]
            base = tok.rsplit("/", 1)[-1]
            if base == "opencode" and cmd_tokens[i + 1] == "serve":
                return True
        return False

    @staticmethod
    def _argv_has_config(cmd_tokens: list, our_cfg: str) -> bool:
        """True iff argv carries OUR config as an exact token: either
        ``--config <our_cfg>`` (two tokens) or ``--config=<our_cfg>`` (one)."""
        joined = f"--config={our_cfg}"
        for i, tok in enumerate(cmd_tokens):
            if tok == joined:
                return True
            if tok == "--config" and i + 1 < len(cmd_tokens) \
                    and cmd_tokens[i + 1] == our_cfg:
                return True
        return False

    def _sweep_orphan(self) -> None:
        """Kill OUR leftover ``opencode serve`` from a crashed prior session.

        Tri-state gated (MAJOR-1): we only ever kill on a conclusive ``OURS``,
        and we only delete the pid file on a conclusive verdict (OURS after the
        kill, or NOT_OURS). On ``UNKNOWN`` (a /proc read timeout / WSL hiccup)
        we KEEP the pid file and skip — deleting it would permanently lose a
        still-running orphan; the next launch retries the sweep.

        The kill is a graceful TERM, a brief wait, an identity RE-CHECK, and a
        KILL -9 ONLY if the re-check is still conclusively ``OURS`` (MAJOR-3).
        If the pid went away or was recycled in the gap the re-check is no
        longer OURS and we do NOT -9, closing the PID-reuse TOCTOU. NEVER a
        blind pkill-by-name."""
        data = self._read_pid_file()
        if data is None:
            return
        pid = data["pid"]

        identity = self._pid_is_our_opencode(pid)
        if identity is PidIdentity.UNKNOWN:
            # Inconclusive — could not read /proc (timeout / WSL hiccup). Keep
            # the pid file and retry on the next launch; never lose an orphan.
            log.warning(
                f"Orphan-sweep identity check for pid {pid} was inconclusive; "
                f"keeping pid file and retrying next launch"
            )
            return
        if identity is PidIdentity.NOT_OURS:
            # Definitely gone / recycled / unrelated — forget the stale file.
            log.debug(f"PID-file pid {pid} is not OUR live opencode serve; not killing")
            self._remove_pid_file()
            return

        # identity is OURS — graceful TERM first.
        log.info(f"Sweeping orphan opencode serve (pid {pid}) from a prior session")
        try:
            subprocess.run(
                [self._config.wsl_path, "bash", "-ic", f"kill {int(pid)} 2>/dev/null"],
                capture_output=True, timeout=5, cwd=safe_wsl_cwd(),
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.debug(f"Orphan sweep TERM failed for pid {pid}: {e}")

        # Brief grace for the process to exit on its own.
        time.sleep(1.0)

        # RE-CHECK identity before escalating to -9 (MAJOR-3): if the pid is
        # gone or was recycled to a different process in the TERM→wait gap, this
        # is no longer OURS and we MUST NOT -9 (PID-reuse TOCTOU). Only escalate
        # on a fresh, conclusive OURS.
        post = self._pid_is_our_opencode(pid)
        if post is PidIdentity.OURS:
            try:
                subprocess.run(
                    [self._config.wsl_path, "bash", "-ic", f"kill -9 {int(pid)} 2>/dev/null"],
                    capture_output=True, timeout=5, cwd=safe_wsl_cwd(),
                )
            except Exception as e:  # noqa: BLE001 — best-effort
                log.debug(f"Orphan sweep KILL -9 failed for pid {pid}: {e}")
            # We forcibly -9'd OUR process — the pid file has served its purpose.
            self._remove_pid_file()
        elif post is PidIdentity.NOT_OURS:
            # Gone or recycled to a non-ours process in the TERM→wait gap: no -9
            # (closes the PID-reuse TOCTOU), and the stale file can go.
            log.debug(f"pid {pid} no longer OURS after TERM; skipping KILL -9")
            self._remove_pid_file()
        else:  # UNKNOWN
            # Could NOT confirm the TERM took effect (a /proc read timeout / WSL
            # hiccup) — the orphan may still be alive. KEEP the pid file so the
            # next launch retries the sweep; never delete on an inconclusive
            # post-TERM check (gpt-5.5 edge).
            log.warning(
                f"pid {pid} identity inconclusive after TERM; keeping pid file "
                f"for next-launch retry"
            )
