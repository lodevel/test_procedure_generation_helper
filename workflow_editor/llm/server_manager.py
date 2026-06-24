"""
OpenCode Server Manager - Manages the shared OpenCode WSL server process.

This module provides a singleton-like manager for the OpenCode server process.
ONE server instance is shared across all tabs, while each tab has its own session.
"""

import logging
import subprocess
import threading
import time
from collections import deque
from typing import Optional

import requests

from .opencode_backend import OpenCodeConfig, safe_wsl_cwd
from .server_health import (
    ServerError,
    ServerStatus,
    classify_install,
    find_free_port,
    is_port_conflict,
)

log = logging.getLogger(__name__)


def fetch_opencode_models(server_url: str, timeout: float = 2.0) -> list:
    """Query a running OpenCode server's ``/config`` and return its available
    ``<providerID>/<modelID>`` ids (sorted, de-duped).

    Returns ``[]`` when the server is unreachable or isn't OpenCode (same JSON
    signature check as :meth:`OpenCodeServerManager._check_external_server`).
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
    
    @property
    def is_running(self) -> bool:
        """
        Check if the server is running.
        
        This checks both the internal state AND verifies the process
        is still alive.
        
        Returns:
            True if server process is running.
        """
        with self._lock:
            if not self._running or self._server_process is None:
                return False
            
            # Check if process is still alive
            if self._server_process.poll() is not None:
                # Process has terminated
                log.warning("Server process terminated unexpectedly")
                self._running = False
                self._server_process = None
                return False
            
            return True
    
    def start(self) -> bool:
        """
        Start the server if not already running.
        
        This method:
        1. Checks if server is already running (returns True)
        2. Checks if an external server is available (attaches to it)
        3. Starts a new server process if needed
        
        Returns:
            True if server is running (either started or already running).
            False if server failed to start.
        """
        with self._lock:
            if self._running and self._server_process is not None:
                log.debug("Server already running")
                return True
            
            # First, check if an external server is already running
            if self._check_external_server():
                log.info(f"Attached to existing OpenCode server at {self.server_url}")
                self._running = True
                self.last_status = ServerStatus.healthy()
                # Note: _server_process remains None - we didn't start it
                return True

            # Pre-flight: a missing WSL / opencode gives a precise reason
            # instead of an opaque spawn failure later.
            diag = self._diagnose_installation()
            if not diag.ok:
                self.last_status = diag
                log.error(f"OpenCode unavailable: {diag.message}")
                return False

            log.info("Starting OpenCode server...")
            try:
                # Bind the configured port if free, else an OS-assigned one, and
                # propagate the choice to the shared config so backends POST to
                # the right URL.
                port = find_free_port(
                    self._config.server_port, self._config.server_hostname
                )
                self._config.server_port = port

                # bash -lc loads the user's PATH so `opencode` resolves.
                opencode_cmd = (
                    f"opencode serve --port {port} "
                    f"--hostname {self._config.server_hostname}"
                )
                cmd = [self._config.wsl_path, "bash", "-ic", opencode_cmd]
                log.debug(f"Server command: {' '.join(cmd)}")

                # stdout discarded; stderr PIPE is drained by a thread so the
                # pipe buffer never fills and deadlocks the server.
                self._server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    # Launch from the editor master-config dir (a translatable
                    # Windows path) so OpenCode loads the editor's opencode.json.
                    # Fall back to the system drive — NEVER the inherited cwd,
                    # which may be a non-translatable drive (e.g. L:).
                    cwd=self._config.working_directory or safe_wsl_cwd(),
                )
                self._start_stderr_drain()
                log.debug(f"Server process started, PID: {self._server_process.pid}")

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
    
    def stop(self) -> None:
        """
        Stop the server if running.
        
        This method:
        1. Terminates the server process gracefully
        2. Force kills if termination times out
        3. Cleans up internal state
        
        Safe to call multiple times.
        """
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
        Check if OpenCode is available (WSL and opencode installed).
        
        This checks:
        1. If an OpenCode server is already running
        2. If WSL is available
        3. If opencode command is installed in WSL
        
        Returns:
            True if OpenCode can be used.
        """
        log.debug("Checking if OpenCode is available...")

        # First, check if a server is already running
        if self._check_external_server():
            log.info("OpenCode server is already running")
            self.last_status = ServerStatus.healthy()
            return True

        # Check WSL and opencode installation (records the classified reason).
        diag = self._diagnose_installation()
        self.last_status = diag
        return diag.ok
    
    def _check_external_server(self) -> bool:
        """
        Check if an external OpenCode server is already running.
        
        Returns:
            True if a server is responding at the configured URL.
        """
        try:
            log.debug(f"Checking for an OpenCode server at {self.server_url}...")
            # Verify it's GENUINELY OpenCode, not merely any HTTP 200. The web UI
            # (and any stray server) answer 200 on /health with HTML; the /config
            # API returns JSON — a reliable signature. Without this, the editor
            # would falsely attach to a non-OpenCode server holding the port and
            # then hang forever on the SSE stream.
            response = requests.get(
                f"{self.server_url}/config",
                headers={"Accept": "application/json"},
                timeout=2,
            )
            if response.status_code != 200:
                return False
            if "application/json" not in response.headers.get("Content-Type", ""):
                return False
            return isinstance(response.json(), dict)
        except (requests.exceptions.RequestException, ValueError):
            return False
    
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
            except requests.exceptions.RequestException:
                pass
            
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
