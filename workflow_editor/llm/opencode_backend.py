"""
OpenCode Backend - LLM backend using WSL OpenCode CLI.

Uses a persistent OpenCode server for faster responses.
"""

import subprocess
import threading
import time
import json
import tempfile
import logging
import requests
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass

from .backend_base import LLMBackend, LLMRequest, LLMResponse, LLMTask

log = logging.getLogger(__name__)


@dataclass
class OpenCodeConfig:
    """Configuration for OpenCode backend."""
    # WSL executable path
    wsl_path: str = "wsl"
    
    # OpenCode server settings
    server_port: int = 4096
    server_hostname: str = "127.0.0.1"
    
    # Model settings (optional override)
    model: Optional[str] = None  # e.g., "anthropic/claude-3-5-sonnet"
    
    # Session mode: "persistent" (default) or "oneshot" (future: new session per request)
    session_mode: str = "persistent"
    
    # Timeouts
    startup_timeout: float = 30.0
    request_timeout: float = 120.0
    
    # Extra arguments
    extra_args: list[str] = None
    
    def __post_init__(self):
        if self.extra_args is None:
            self.extra_args = []
    
    @property
    def server_url(self) -> str:
        return f"http://{self.server_hostname}:{self.server_port}"


class OpenCodeBackend(LLMBackend):
    """
    LLM backend using WSL OpenCode CLI with persistent server.
    
    Startup sequence:
    1. Start WSL OpenCode server: wsl opencode serve --port 4096
    2. Wait for server ready
    3. Keep server running for application lifetime
    4. On close, terminate WSL process
    
    Request sequence:
    - Use HTTP API: POST to /session/<id>/message
    - Or use CLI: wsl opencode run --attach --format json
    """
    
    def __init__(self, 
                 config: Optional[OpenCodeConfig] = None,
                 custom_prompts: Optional[dict] = None,
                 custom_output_format: Optional[str] = None):
        # Call base class init for common initialization
        super().__init__(custom_prompts, custom_output_format)
        
        self.config = config or OpenCodeConfig()
        self._server_process: Optional[subprocess.Popen] = None
        self._session_id: Optional[str] = None
    
    @property
    def name(self) -> str:
        return "OpenCode CLI (WSL)"
    
    @property
    def is_running(self) -> bool:
        return self._running and self._server_process is not None
    
    def is_available(self) -> bool:
        """Check if WSL and OpenCode are available.
        
        This checks in two ways:
        1. Try to connect to an already-running OpenCode server
        2. Check if opencode command is available in WSL PATH
        """
        log.debug("Checking if OpenCode is available...")
        
        # First, try to connect to an already-running server
        try:
            log.debug(f"Checking for running server at {self.config.server_url}...")
            response = requests.get(
                f"{self.config.server_url}/health",
                timeout=1
            )
            if response.status_code == 200:
                log.info("OpenCode server is already running!")
                return True
        except requests.exceptions.RequestException:
            log.debug("No running server found, will check WSL installation")
        
        # If no server running, check if we can start one
        try:
            # Check WSL is available
            result = subprocess.run(
                [self.config.wsl_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                log.warning(f"WSL not available: {result.stderr}")
                return False
            
            log.debug("WSL is available")
            
            # Check OpenCode is installed in WSL
            # Use bash -lc to load user profile/PATH
            result = subprocess.run(
                [self.config.wsl_path, "bash", "-lc", "opencode --version"],
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
    
    def start(self) -> bool:
        """Start the OpenCode server in WSL."""
        with self._lock:
            if self._running:
                log.debug("Server already running")
                return True
            
            log.info("Starting OpenCode server...")
            try:
                # Build server command - use bash -lc to load user's PATH
                opencode_cmd = f"opencode serve --port {self.config.server_port} --hostname {self.config.server_hostname}"
                cmd = [
                    self.config.wsl_path,
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
                    self.stop()
                    return False
                
                # Create a session
                self._session_id = self._create_session()
                if self._session_id is None:
                    log.error("Failed to create session")
                    self.stop()
                    return False
                
                self._running = True
                log.info("OpenCode server started successfully")
                return True
                
            except Exception as e:
                log.error(f"Failed to start server: {e}")
                print(f"Failed to start OpenCode server: {e}")
                self.stop()
                return False
    
    def _wait_for_server(self) -> bool:
        """Wait for server to be ready by polling health endpoint."""
        log.debug("Waiting for server to be ready...")
        start_time = time.time()
        attempt = 0
        
        while time.time() - start_time < self.config.startup_timeout:
            attempt += 1
            log.debug(f"Health check attempt {attempt}...")
            try:
                response = requests.get(
                    f"{self.config.server_url}/health",
                    timeout=1
                )
                if response.status_code == 200:
                    return True
            except requests.exceptions.RequestException:
                pass
            
            # Check if process died
            if self._server_process and self._server_process.poll() is not None:
                return False
            
            time.sleep(0.5)
        
        log.error(f"Server failed to start within {self.config.startup_timeout}s timeout")
        return False
    
    def _create_session(self) -> Optional[str]:
        """Create a new session and return session ID."""
        log.debug("Creating new session...")
        try:
            response = requests.post(
                f"{self.config.server_url}/session",
                json={"title": "LLM Workflow Editor"},
                timeout=5
            )
            log.debug(f"Session response: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                session_id = data.get("id")
                log.info(f"Session created: {session_id}")
                return session_id
        except requests.exceptions.RequestException as e:
            log.error(f"Failed to create session: {e}")
            pass
        return None
    
    def reset_session(self) -> Optional[str]:
        """Reset the LLM session (clears conversation history)."""
        log.info("Resetting LLM session...")
        self.stop()
        if self.start():
            log.info(f"Session reset complete: {self._session_id}")
            return self._session_id
        return None
    
    def stop(self) -> None:
        """Stop the OpenCode server."""
        with self._lock:
            self._running = False
            self._session_id = None
            
            if self._server_process:
                try:
                    self._server_process.terminate()
                    self._server_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._server_process.kill()
                finally:
                    self._server_process = None
    
    def cancel(self) -> None:
        """Cancel any in-progress request."""
        self._cancel_requested = True
    
    def send_request(
        self, 
        request: LLMRequest,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> LLMResponse:
        """Send a request to OpenCode.
        
        Note: Session optimization (tracking what was already sent) is handled
        by TabContext, not the backend. The request.include_* flags are already
        set appropriately before reaching this method.
        """
        self._cancel_requested = False
        
        if not self._running or self._session_id is None:
            return LLMResponse(
                success=False,
                error_message="OpenCode server is not running",
            )
        
        try:
            # Build the prompt (TabContext has already set include_* flags)
            prompt = self._prompt_builder.build(request, output_contract_override=request.output_contract)
            
            if progress_callback:
                progress_callback("Sending request to LLM...")
            
            # Send via HTTP API
            response = self._send_via_api(prompt, request)
            
            if self._cancel_requested:
                return LLMResponse(
                    success=False,
                    error_message="Request cancelled",
                )
            
            return response
            
        except Exception as e:
            return LLMResponse(
                success=False,
                error_message=f"Request failed: {str(e)}",
            )
    
    def _send_via_api(self, prompt: str, request: LLMRequest) -> LLMResponse:
        """Send request via HTTP API."""
        try:
            # Build request body
            body = {
                "parts": [{"type": "text", "text": prompt}],
            }
            
            # Add model override if configured
            if self.config.model:
                provider, model = self.config.model.split("/", 1)
                body["model"] = {
                    "providerID": provider,
                    "modelID": model,
                }
            
            # Send request
            response = requests.post(
                f"{self.config.server_url}/session/{self._session_id}/message",
                json=body,
                timeout=self.config.request_timeout,
            )
            
            log.debug(f"HTTP response: status={response.status_code}, content-length={len(response.text)}")
            log.debug(f"Response headers: {dict(response.headers)}")
            
            if response.status_code != 200:
                return LLMResponse(
                    success=False,
                    error_message=f"API error: {response.status_code} - {response.text}",
                    raw_response=response.text,
                )
            
            # Parse response
            raw_response = response.text
            
            # Log response details for debugging
            log.debug(f"Raw response length: {len(raw_response)} chars")
            if len(raw_response) == 0:
                log.error("OpenCode returned empty response body despite HTTP 200 status")
                log.error(f"Request body size: {len(str(body))} chars")
                log.error(f"Session ID: {self._session_id}")
                return LLMResponse(
                    success=False,
                    error_message="OpenCode returned empty response (0 chars)",
                    raw_response="",
                )
            
            # Check for context length exceeded error
            try:
                response_data = json.loads(raw_response)
                if "info" in response_data and "error" in response_data["info"]:
                    error_info = response_data["info"]["error"]
                    if error_info.get("name") == "UnknownError" and "data" in error_info:
                        error_data_str = error_info.get("data", {}).get("message", "")
                        if "context_length_exceeded" in error_data_str:
                            log.warning("Context length exceeded error detected")
                            return LLMResponse(
                                success=False,
                                error_message="Context length exceeded",
                                context_exceeded=True,
                                raw_response=raw_response,
                            )
            except (json.JSONDecodeError, KeyError):
                pass  # Not a context error, continue normal parsing
            
            log.debug(f"Raw response preview: {raw_response[:200]}")
            
            # Parse the response
            llm_response = self._response_parser.parse(raw_response, request.task)
            
            # Extract and assign token usage using base class method
            try:
                response_data = json.loads(raw_response)
                prompt_tokens, completion_tokens, total_tokens = self._extract_token_usage(response_data)
                llm_response.prompt_tokens = prompt_tokens
                llm_response.completion_tokens = completion_tokens
                llm_response.total_tokens = total_tokens
            except (json.JSONDecodeError, KeyError) as e:
                log.warning(f"Failed to extract token usage: {e}")
            
            return llm_response
            
        except requests.exceptions.Timeout:
            return LLMResponse(
                success=False,
                error_message="Request timed out",
            )
        except requests.exceptions.RequestException as e:
            return LLMResponse(
                success=False,
                error_message=f"Request failed: {str(e)}",
            )
    
    def _send_via_cli(self, prompt: str, request: LLMRequest) -> LLMResponse:
        """Alternative: Send request via CLI (fallback method)."""
        try:
            # Write prompt to temp file
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8"
            ) as f:
                f.write(prompt)
                prompt_file = Path(f.name)
            
            try:
                # Build command
                cmd = [
                    self.config.wsl_path,
                    "opencode", "run",
                    "--attach", self.config.server_url,
                    "--format", "json",
                    "-f", str(prompt_file),
                    "Process the attached prompt",
                ]
                
                if self.config.model:
                    cmd.extend(["-m", self.config.model])
                
                # Run command
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.request_timeout,
                )
                
                if result.returncode != 0:
                    return LLMResponse(
                        success=False,
                        error_message=f"CLI error: {result.stderr}",
                        raw_response=result.stdout,
                    )
                
                return self._response_parser.parse(result.stdout, request.task)
                
            finally:
                prompt_file.unlink()
                
        except subprocess.TimeoutExpired:
            return LLMResponse(
                success=False,
                error_message="Request timed out",
            )
        except Exception as e:
            return LLMResponse(
                success=False,
                error_message=f"CLI request failed: {str(e)}",
            )
