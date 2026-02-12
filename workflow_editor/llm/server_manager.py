"""
OpenCode Server Manager - Manages the shared OpenCode WSL server process.

This module provides a singleton-like manager for the OpenCode server process.
ONE server instance is shared across all tabs, while each tab has its own session.
"""

import logging
import subprocess
import threading
import time
from typing import Optional

import requests

from .opencode_backend import OpenCodeConfig

log = logging.getLogger(__name__)


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
                # Note: _server_process remains None - we didn't start it
                return True
            
            log.info("Starting OpenCode server...")
            try:
                # Build server command - use bash -lc to load user's PATH
                opencode_cmd = f"opencode serve --port {self._config.server_port} --hostname {self._config.server_hostname}"
                cmd = [
                    self._config.wsl_path,
                    "bash", "-lc",
                    opencode_cmd,
                ]
                log.debug(f"Server command: {' '.join(cmd)}")
                
                # Start server process
                self._server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                log.debug(f"Server process started, PID: {self._server_process.pid}")
                
                # Wait for server to be ready
                if not self._wait_for_server():
                    log.error("Server failed to become ready")
                    self._stop_process()
                    return False
                
                self._running = True
                log.info(f"OpenCode server started successfully at {self.server_url}")
                return True
                
            except Exception as e:
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
            return True
        
        # Check WSL and opencode installation
        return self._check_wsl_installation()
    
    def _check_external_server(self) -> bool:
        """
        Check if an external OpenCode server is already running.
        
        Returns:
            True if a server is responding at the configured URL.
        """
        try:
            log.debug(f"Checking for running server at {self.server_url}...")
            response = requests.get(
                f"{self.server_url}/health",
                timeout=1
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False
    
    def _check_wsl_installation(self) -> bool:
        """
        Check if WSL and OpenCode are properly installed.
        
        Returns:
            True if both WSL and opencode are available.
        """
        try:
            # Check WSL is available
            result = subprocess.run(
                [self._config.wsl_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                log.warning(f"WSL not available: {result.stderr}")
                return False
            
            log.debug("WSL is available")
            
            # Check OpenCode is installed in WSL
            result = subprocess.run(
                [self._config.wsl_path, "bash", "-lc", "opencode --version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            available = result.returncode == 0
            if available:
                log.debug(f"OpenCode is installed in WSL: {result.stdout.strip()}")
            else:
                log.warning(f"OpenCode not found in WSL PATH: {result.stderr}")
            return available
            
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.error(f"Error checking OpenCode availability: {e}")
            return False
    
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
            
            # Check if process died
            if self._server_process and self._server_process.poll() is not None:
                stderr = ""
                if self._server_process.stderr:
                    stderr = self._server_process.stderr.read()
                log.error(f"Server process died during startup: {stderr}")
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
