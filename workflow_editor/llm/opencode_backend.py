"""
OpenCode Backend - LLM backend using WSL OpenCode CLI.

Uses a persistent OpenCode server for faster responses.
"""

import os
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


def safe_wsl_cwd() -> str:
    """A Windows directory wsl.exe can always translate to a WSL path (the
    system drive root, mounted at /mnt/c).

    Used as the cwd for every wsl.exe call: if the editor was launched from a
    non-translatable drive (e.g. an ``L:\\`` mapped/network drive), wsl.exe
    inherits that cwd and fails — "wsl: Failed to translate 'L:\\...'" — which
    degrades the shell so even an installed `opencode` reports "command not
    found". Anchoring the cwd to the system drive avoids that.
    """
    return os.environ.get("SystemDrive", "C:") + "\\"


def resolve_context_window(
    model_str: Optional[str],
    providers_doc: Optional[dict],
) -> Optional[int]:
    """The active model's real context window from a ``/config/providers`` doc.

    ``model_str`` is OpenCode's ``"providerID/modelID"`` string (modelID may
    itself contain slashes — only the FIRST ``/`` splits provider from model).
    ``providers_doc`` is the parsed ``GET /config/providers`` JSON, shaped
    ``{"providers": [{"id": ..., "models": {modelID: {"limit": {"context": N}}}}]}``.

    Returns the model's ``limit.context`` as a positive int, or ``None`` when the
    model can't be resolved / carries no positive context limit (caller falls
    back to the static setting). Pure (no HTTP) so it's unit-testable.
    """
    if not model_str or "/" not in model_str:
        return None
    provider_id, model_id = model_str.split("/", 1)
    if not isinstance(providers_doc, dict):
        return None
    for provider in providers_doc.get("providers") or []:
        if not isinstance(provider, dict) or provider.get("id") != provider_id:
            continue
        model = (provider.get("models") or {}).get(model_id)
        if isinstance(model, dict):
            ctx = (model.get("limit") or {}).get("context")
            if isinstance(ctx, (int, float)) and ctx > 0:
                return int(ctx)
    return None


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

    # Models whose chat template has no system role (e.g. Gemma) silently
    # drop body["system"]; when True the backend folds the system prompt into
    # the leading user part so the skill still governs. Default False keeps the
    # real-system-role path (OpenAI/Anthropic) byte-identical.
    fold_system_into_prompt: bool = False

    # Working directory for the spawned `opencode serve` (a Windows path;
    # wsl.exe translates it). Set to the open project so OpenCode loads THAT
    # project's opencode.json (its providers/model) instead of the home/global
    # config. None -> inherit the launching process's cwd.
    working_directory: Optional[str] = None

    # Session mode: "persistent" (default) or "oneshot" (future: new session per request)
    session_mode: str = "persistent"
    
    # Timeouts
    startup_timeout: float = 30.0
    request_timeout: float = 120.0
    
    # Extra arguments
    extra_args: list[str] = None

    # Per-request tool-gate universe: {server: [tool names]} for every discovered
    # skill-owned / common tool folder. Set IN PLACE by main_window._prewarm_server
    # (the SAME OpenCodeConfig object this backend holds), read in
    # _build_message_body to force every non-active skill tool to an explicit
    # False (OpenCode's tool override is additive).
    skill_tools: dict = None

    # Per-process HMAC secret signing the run_skill chain-token. Minted ONCE in
    # main_window._prewarm_server and carried on this shared OpenCodeConfig to both
    # the token-mint (skill_chat_widget) and the run_skill MCP subprocess (block
    # environment). None/blank = run_skill refuses every call (fail-closed).
    run_skill_secret: Optional[str] = None
    
    def __post_init__(self):
        if self.extra_args is None:
            self.extra_args = []
        if self.skill_tools is None:
            self.skill_tools = {}
    
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
        # Cached real context window for the active model (fetched once from the
        # running server's /config + /config/providers). ``None`` = not yet
        # resolved or unavailable; the readout falls back to the static setting.
        self._context_window: Optional[int] = None
    
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
            # Check WSL is available (cwd anchored to a translatable drive)
            result = subprocess.run(
                [self.config.wsl_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=safe_wsl_cwd(),
            )
            if result.returncode != 0:
                log.warning(f"WSL not available: {result.stderr}")
                return False

            log.debug("WSL is available")

            # Check OpenCode is installed in WSL
            # Use bash -ic to load the user's PATH (opencode is added to
            # ~/.bashrc by its installer, which a login shell -lc skips).
            result = subprocess.run(
                [self.config.wsl_path, "bash", "-ic", "opencode --version"],
                capture_output=True,
                text=True,
                cwd=safe_wsl_cwd(),
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
                # Build server command - use bash -ic to load user's PATH.
                # OPENCODE_ENABLE_EXA enables the keyless Exa websearch tool for
                # non-OpenCode providers (set inline; wsl.exe won't forward it).
                opencode_cmd = f"OPENCODE_ENABLE_EXA=1 opencode serve --port {self.config.server_port} --hostname {self.config.server_hostname}"
                cmd = [
                    self.config.wsl_path,
                    "bash", "-ic",
                    opencode_cmd,
                ]
                log.debug(f"Server command: {' '.join(cmd)}")
                
                # Start server process (in the project dir so OpenCode loads
                # that project's opencode.json — providers/model).
                self._server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=self.config.working_directory or safe_wsl_cwd(),
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

    def new_session(self) -> Optional[str]:
        """Start a FRESH session (new conversation) WITHOUT restarting the
        server — the cheap counterpart of :meth:`reset_session` (which stops +
        starts the whole server). Used when the caller WANTS a clean slate (the
        skill chat's trash / skill-switch), discarding the server-side history."""
        self._session_id = self._create_session()
        return self._session_id

    def ensure_session(self) -> Optional[str]:
        """Return the current session, creating one ONLY if none exists.

        The persistent-session counterpart of :meth:`new_session`: a multi-turn
        chat calls this before each send so OpenCode KEEPS one session and retains
        the whole history — including MCP tool results (the netlist / BOM) — across
        turns, instead of throwing it away every send. After a server restart the
        same id stays valid (OpenCode persists sessions on disk), so this also
        REATTACHES; it only mints a new session when there is genuinely none."""
        if self._session_id is None:
            self._session_id = self._create_session()
        return self._session_id

    @staticmethod
    def _is_session_not_found(response) -> bool:
        """True iff a response is OpenCode's 'session lost' signal: HTTP 404 whose
        JSON body name == 'NotFoundError'. Distinguishes a lost session (mint +
        replay) from server-down (a RequestException, never a 404) and from a
        model error (200 with an error body)."""
        if getattr(response, "status_code", None) != 404:
            return False
        try:
            return (response.json() or {}).get("name") == "NotFoundError"
        except (ValueError, AttributeError):
            return False

    def _rehydrate_session(self) -> bool:
        """Mint a FRESH server-side session after the old one was lost; return
        True on success (new id replaces _session_id). Only mints — never
        injects — so it cannot itself 404."""
        new_id = self._create_session()
        if new_id:
            log.warning(f"Session lost; rehydrated with fresh session {new_id}")
            self._session_id = new_id
            return True
        log.error("Session lost AND rehydrate failed: could not mint a new session")
        return False

    def compact(self) -> bool:
        """Manually compact the active session — OpenCode summarizes the prior
        history in place, freeing context while keeping the SAME session id.

        POSTs (no body) to ``/api/session/{id}/compact`` and returns True on a 2xx
        (the server returns 204). NOTE the ``/api`` prefix — unlike /message (which
        the OpenCode server exposes at both /session and /api/session), compact is
        ONLY under /api/session. Returns False — never raises — when the server
        isn't running, there's no session, or the request fails, so the caller
        can report a clean outcome on the UI thread."""
        if not self._running or self._session_id is None:
            log.debug("compact: no running server / session")
            return False
        try:
            response = requests.post(
                f"{self.config.server_url}/api/session/{self._session_id}/compact",
                timeout=self.config.request_timeout,
            )
            ok = 200 <= response.status_code < 300
            if ok:
                log.info(f"Compacted session {self._session_id}")
            else:
                log.warning(f"compact failed: HTTP {response.status_code}")
            return ok
        except requests.exceptions.RequestException as e:
            log.warning(f"compact request failed: {e}")
            return False

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
        """Cancel any in-progress request.
        
        Also sends abort to the OpenCode server to stop in-flight generation.
        """
        self._cancel_requested = True
        # Try to abort the running session on the server
        if self._running and self._session_id:
            try:
                requests.post(
                    f"{self.config.server_url}/session/{self._session_id}/abort",
                    timeout=3,
                )
                log.debug("Sent abort request to OpenCode server")
            except requests.exceptions.RequestException:
                log.debug("Failed to send abort request (server may be busy)")
    
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
            prompt = (
                request.raw_prompt
                if request.raw_prompt is not None
                else self._prompt_builder.build(
                    request, output_contract_override=request.output_contract
                )
            )
            
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
    
    def send_request_streaming(
        self,
        request: LLMRequest,
        thinking_callback: Optional[Callable[[str], None]] = None,
        text_callback: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """Send a request to OpenCode with streaming SSE events.
        
        Uses prompt_async + GET /event SSE stream to receive progressive
        thinking/reasoning and text chunks as they are generated.
        Falls back to synchronous send_request if SSE fails.
        
        Args:
            request: The LLM request to send.
            thinking_callback: Called with reasoning text deltas as they arrive.
            text_callback: Called with response text deltas as they arrive.
            
        Returns:
            Complete LLMResponse (same as send_request).
        """
        self._cancel_requested = False
        
        if not self._running or self._session_id is None:
            return LLMResponse(
                success=False,
                error_message="OpenCode server is not running",
            )
        
        try:
            prompt = (
                request.raw_prompt
                if request.raw_prompt is not None
                else self._prompt_builder.build(
                    request, output_contract_override=request.output_contract
                )
            )
            
            # Build request body (shared with the sync path).
            body = self._build_message_body(prompt, request)

            session_id = self._session_id
            
            # 1. Open SSE connection FIRST (so we don't miss early events)
            sse_response = None
            try:
                sse_response = requests.get(
                    f"{self.config.server_url}/event",
                    stream=True,
                    # connect 5s; read = inactivity cap so a hung/unreachable
                    # model (no SSE bytes) can't leave the chat stuck forever.
                    timeout=(5, self.config.request_timeout),
                    headers={"Accept": "text/event-stream"},
                )
                if sse_response.status_code != 200:
                    log.warning(f"SSE connection failed ({sse_response.status_code}), falling back to sync")
                    if sse_response:
                        sse_response.close()
                    return self.send_request(request)
            except requests.exceptions.RequestException as e:
                log.warning(f"SSE connection failed ({e}), falling back to sync")
                return self.send_request(request)
            
            # 2. Send prompt asynchronously (returns 204 immediately)
            try:
                async_response = requests.post(
                    f"{self.config.server_url}/session/{session_id}/prompt_async",
                    json=body,
                    timeout=10,
                )
                if async_response.status_code not in (200, 204):
                    log.warning(f"prompt_async failed ({async_response.status_code}), falling back to sync")
                    sse_response.close()
                    return self.send_request(request)
            except requests.exceptions.RequestException as e:
                log.warning(f"prompt_async failed ({e}), falling back to sync")
                sse_response.close()
                return self.send_request(request)
            
            # 3. Read SSE events until session goes idle or error
            log.debug("Streaming: listening for SSE events...")
            response = self._consume_sse_events(
                sse_response, session_id, request,
                thinking_callback, text_callback,
            )
            
            return response
            
        except Exception as e:
            log.error(f"Streaming request failed: {e}", exc_info=True)
            return LLMResponse(
                success=False,
                error_message=f"Streaming request failed: {str(e)}",
            )
    
    def _consume_sse_events(
        self,
        sse_response,
        session_id: str,
        request: LLMRequest,
        thinking_callback: Optional[Callable[[str], None]],
        text_callback: Optional[Callable[[str], None]],
    ) -> LLMResponse:
        """Consume SSE events, emit streaming callbacks, then fetch final response.
        
        Listens for:
        - message.part.updated: reasoning/text deltas
        - session.idle: completion signal
        - session.error: error signal 
        """
        # Track accumulated content for fallback
        accumulated_thinking = []
        accumulated_text = []
        # Track part IDs we've seen to compute deltas from full text
        part_snapshots = {}  # part_id -> last known text length
        # Track whether we've seen an assistant part (reasoning proves it)
        seen_assistant_part = [False]
        
        try:
            event_data_buffer = ""
            event_type = ""
            
            # decode_unicode=False → iter_lines yields raw bytes, decoded as
            # UTF-8 explicitly below. With decode_unicode=True, requests guesses
            # the charset and defaults to ISO-8859-1 for text/* responses
            # without an explicit charset, mojibaking UTF-8 (e.g. the apostrophe
            # ’ U+2019 → "â€™", rendering as boxes in the chat thinking view).
            for line in sse_response.iter_lines(decode_unicode=False):
                if self._cancel_requested:
                    log.debug("Streaming: cancelled by user")
                    break

                if line is None:
                    continue

                line_str = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                
                # SSE protocol parsing
                if line_str.startswith("event:"):
                    event_type = line_str[6:].strip()
                elif line_str.startswith("data:"):
                    event_data_buffer += line_str[5:].strip()
                elif line_str == "":
                    # Empty line = end of event
                    if event_data_buffer:
                        try:
                            event_json = json.loads(event_data_buffer)
                            self._process_sse_event(
                                event_json, session_id,
                                thinking_callback, text_callback,
                                accumulated_thinking, accumulated_text,
                                part_snapshots, seen_assistant_part,
                            )
                        except json.JSONDecodeError:
                            log.debug(f"Streaming: failed to parse SSE data: {event_data_buffer[:100]}")
                        
                        # Check for completion events
                        try:
                            event_json = json.loads(event_data_buffer)
                            payload = event_json.get("payload", event_json)
                            evt_type = payload.get("type", "")
                            props = payload.get("properties", {})
                            
                            if evt_type == "session.idle":
                                if props.get("sessionID") == session_id:
                                    log.debug("Streaming: session.idle received, fetching final response")
                                    break
                            
                            if evt_type == "session.error":
                                err_session = props.get("sessionID", "")
                                if err_session == session_id or not err_session:
                                    error_info = props.get("error", {})
                                    error_msg = error_info.get("data", {}).get("message", str(error_info))
                                    log.warning(f"Streaming: session.error: {error_msg}")
                                    break
                            
                            if evt_type == "session.status":
                                if props.get("sessionID") == session_id:
                                    status = props.get("status", {})
                                    if isinstance(status, dict) and status.get("type") == "idle":
                                        log.debug("Streaming: session.status idle, fetching final response")
                                        break
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    
                    event_data_buffer = ""
                    event_type = ""

        except requests.exceptions.RequestException as e:
            # No SSE data within the read-timeout window (or the stream broke):
            # the model/backend is unresponsive. Surface a clear error rather than
            # leaving the chat stuck on "Thinking…" forever.
            log.warning(f"Streaming: SSE stalled/failed ({e}); treating as timeout")
            return LLMResponse(
                success=False,
                error_message=(
                    f"The model did not respond within "
                    f"{int(self.config.request_timeout)}s — check that the model "
                    f"server is reachable."
                ),
            )
        finally:
            try:
                sse_response.close()
            except Exception:
                pass

        if self._cancel_requested:
            return LLMResponse(
                success=False,
                error_message="Request cancelled",
            )
        
        # 4. Fetch the final complete response via GET /session/{id}/message
        return self._fetch_final_response(session_id, request, accumulated_thinking)
    
    def _process_sse_event(
        self,
        event_json: dict,
        session_id: str,
        thinking_callback: Optional[Callable[[str], None]],
        text_callback: Optional[Callable[[str], None]],
        accumulated_thinking: list,
        accumulated_text: list,
        part_snapshots: dict,
        seen_assistant_part: list,
    ):
        """Process a single SSE event and invoke callbacks for streaming content.
        
        Args:
            seen_assistant_part: Single-element list [bool] used as mutable flag
                to track whether an assistant part has been seen."""
        # Events can be wrapped in GlobalEvent format: { directory, payload }
        payload = event_json.get("payload", event_json)
        evt_type = payload.get("type", "")
        properties = payload.get("properties", {})
        
        if evt_type != "message.part.updated":
            return
        
        part = properties.get("part", {})
        delta = properties.get("delta")  # Optional delta string
        part_session = part.get("sessionID", "")
        
        # Only process events for our session
        if part_session and part_session != session_id:
            return
        
        # Skip user message parts — only stream assistant content
        # OpenCode sends message.part.updated for ALL messages including the
        # user's prompt.  The "role" field distinguishes them.
        part_role = part.get("role", "")
        if part_role == "user":
            return
        if part_role == "assistant":
            seen_assistant_part[0] = True
        
        part_type = part.get("type", "")
        part_id = part.get("id", "")
        part_text = part.get("text", "")
        
        log.debug(
            "SSE part: type=%s, role=%s, id=%s, delta_len=%s, text_len=%s, keys=%s",
            part_type, part_role, part_id,
            len(delta) if delta else 0,
            len(part_text),
            list(part.keys()),
        )
        
        if part_type == "reasoning":
            # Reasoning parts are always from the assistant
            seen_assistant_part[0] = True
            # Use delta if available, otherwise compute from snapshot
            if delta and thinking_callback:
                thinking_callback(delta)
                accumulated_thinking.append(delta)
            elif part_text and thinking_callback:
                prev_len = part_snapshots.get(part_id, 0)
                if len(part_text) > prev_len:
                    new_text = part_text[prev_len:]
                    thinking_callback(new_text)
                    accumulated_thinking.append(new_text)
                    part_snapshots[part_id] = len(part_text)
        
        elif part_type == "text":
            # If role field is available, trust it (already filtered above).
            # If role is absent, only stream text after we've confirmed the
            # assistant is active (by seeing a reasoning or assistant-role part).
            if not part_role and not seen_assistant_part[0]:
                log.debug("SSE: skipping text part (no role, no assistant part seen yet)")
                return
            if delta and text_callback:
                text_callback(delta)
                accumulated_text.append(delta)
            elif part_text and text_callback:
                prev_len = part_snapshots.get(part_id, 0)
                if len(part_text) > prev_len:
                    new_text = part_text[prev_len:]
                    text_callback(new_text)
                    accumulated_text.append(new_text)
                    part_snapshots[part_id] = len(part_text)
    
    def _turn_peak_token_usage(self, messages: list, last_assistant: dict) -> tuple[int, int, int]:
        """Token usage for the whole last agentic turn, not just one message.

        A tool-using turn produces MULTIPLE assistant messages — N intermediate
        ``finish:"tool-calls"`` steps then one final ``finish:"stop"`` message.
        The model's true context is the PEAK input+cache.read across those steps:
        for OpenAI the peak lands on the final stop message (cache.read carries
        the tool result), but for providers that don't populate cache.read
        (vLLM/gemma) the big tool-result prompt is billed on an INTERMEDIATE
        tool-calls message and the final stop reports only a tiny delta.

        Walk back from the end to the previous ``user`` message (the start of
        this turn) and return the assistant-step usage with the largest prompt
        context (``prompt_tokens`` = input + cache.read), falling back to the
        single ``last_assistant`` message if no turn boundary is found.
        """
        best = None  # (prompt_tokens, completion_tokens, total_tokens)
        for msg in reversed(messages):
            info = msg.get("info", {})
            role = info.get("role")
            if role == "user":
                break  # reached the start of this turn
            if role != "assistant":
                continue
            try:
                usage = self._extract_token_usage(msg)
            except (KeyError, TypeError):
                continue
            # Peak by prompt context (input + cache.read), then total as tiebreak.
            if best is None or (usage[0], usage[2]) > (best[0], best[2]):
                best = usage
        if best is None:
            return self._extract_token_usage(last_assistant)
        return best

    def _fetch_final_response(self, session_id: str, request: LLMRequest,
                               accumulated_thinking: Optional[list] = None) -> LLMResponse:
        """Fetch the final complete response after streaming is done.
        
        Uses GET /session/{id}/message to get all messages, then takes
        the last assistant message and formats it like the sync API response.
        
        Args:
            accumulated_thinking: Thinking text chunks collected during SSE
                streaming. Used as fallback if the parser can't extract
                thinking from the final response JSON.
        """
        try:
            response = requests.get(
                f"{self.config.server_url}/session/{session_id}/message",
                # Fetch a turn-sized window: an agentic tool turn produces
                # multiple assistant messages (N tool-call steps + one final
                # stop). We need them all so the token readout can peak over
                # the whole turn, not just the final (delta-only) message.
                params={"limit": 20},
                timeout=10,
            )
            
            if response.status_code != 200:
                return LLMResponse(
                    success=False,
                    error_message=f"Failed to fetch final response: {response.status_code}",
                )
            
            messages = response.json()
            
            if not messages:
                return LLMResponse(
                    success=False,
                    error_message="No messages returned after streaming",
                )
            
            # Find the last assistant message
            last_assistant = None
            for msg in reversed(messages):
                info = msg.get("info", {})
                if info.get("role") == "assistant":
                    last_assistant = msg
                    break
            
            if not last_assistant:
                return LLMResponse(
                    success=False,
                    error_message="No assistant message found after streaming",
                )
            
            # Format as the same JSON structure that _send_via_api returns
            raw_response = json.dumps(last_assistant)
            
            log.debug(f"Streaming: fetched final response, length={len(raw_response)}")
            
            # Check for errors in the assistant message
            info = last_assistant.get("info", {})
            error_info = info.get("error")
            if error_info:
                error_name = error_info.get("name", "")
                if error_name == "MessageAbortedError":
                    return LLMResponse(
                        success=False,
                        error_message="Request was aborted",
                        raw_response=raw_response,
                    )
                error_data = error_info.get("data", {})
                error_msg = error_data.get("message", str(error_info))
                if "context_length_exceeded" in error_msg:
                    return LLMResponse(
                        success=False,
                        error_message="Context length exceeded",
                        context_exceeded=True,
                        raw_response=raw_response,
                    )
            
            # Parse through the standard response parser
            llm_response = self._response_parser.parse(
                raw_response, request.task, plain_text=request.raw_prompt is not None
            )
            
            # If the parser didn't find thinking content, use the
            # accumulated thinking from SSE streaming
            if not llm_response.thinking_content and accumulated_thinking:
                llm_response.thinking_content = "".join(accumulated_thinking)
            
            # Extract token usage. Use the PEAK across the whole agentic turn
            # (not just last_assistant): a tool turn's full prompt context can
            # land on an intermediate tool-call message when the provider
            # doesn't report it under cache.read on the final stop message.
            try:
                prompt_tokens, completion_tokens, total_tokens = self._turn_peak_token_usage(
                    messages, last_assistant
                )
                llm_response.prompt_tokens = prompt_tokens
                llm_response.completion_tokens = completion_tokens
                llm_response.total_tokens = total_tokens
            except (KeyError, TypeError) as e:
                log.warning(f"Failed to extract token usage from streaming response: {e}")
            
            return llm_response
            
        except requests.exceptions.RequestException as e:
            return LLMResponse(
                success=False,
                error_message=f"Failed to fetch final response: {str(e)}",
            )
    
    def get_context_window(self) -> Optional[int]:
        """The active model's REAL context window (tokens), from the server.

        Resolves the active model — the editor's ``config.model`` override if set,
        else the server's configured ``model`` from ``GET /config`` — then looks
        up its ``limit.context`` in ``GET /config/providers`` (the full model
        catalogue). The result is cached on the instance (fetched once); returns
        ``None`` on any failure or when the model carries no positive limit, so
        the readout falls back to the static ``common_llm.context_window`` setting.
        """
        if self._context_window is not None:
            return self._context_window
        try:
            model_str = self.config.model
            if not model_str:
                cfg = requests.get(
                    f"{self.config.server_url}/config", timeout=5
                )
                if cfg.status_code == 200:
                    model_str = (cfg.json() or {}).get("model")
            if not model_str:
                return None
            providers = requests.get(
                f"{self.config.server_url}/config/providers", timeout=5
            )
            if providers.status_code != 200:
                return None
            window = resolve_context_window(model_str, providers.json())
        except (requests.exceptions.RequestException, ValueError) as e:
            log.debug(f"get_context_window: could not resolve model window: {e}")
            return None
        if window:
            self._context_window = window
        return window

    def _build_message_body(self, prompt: str, request: LLMRequest) -> dict:
        """Build the POST body for ``/session/{id}/message``.

        Single source for the per-request overrides (model + web tools) so the
        streaming and sync send paths can never drift apart.
        """
        body = {"parts": [{"type": "text", "text": prompt}]}

        # Optional per-request model override (blank = OpenCode auto-picks a
        # supported model from its launch config — the reliable default).
        if self.config.model and "/" in self.config.model:
            provider, model = self.config.model.split("/", 1)
            body["model"] = {"providerID": provider, "modelID": model}

        # Per-request web-tool override (the skill chat's 🌐 toggle). Sent
        # explicitly on AND off so the model gets NO web access unless the user
        # opts in. webfetch is keyless; websearch uses Exa, made available at
        # server launch via OPENCODE_ENABLE_EXA.
        body["tools"] = {
            # SECURITY: the editor's LLM converses and drafts text — it must NEVER
            # read or search the user's filesystem, run a shell, write files, or
            # spawn sub-agents. OpenCode's tool override is ADDITIVE, so any
            # built-in we DON'T list keeps its (enabled) default — we therefore
            # force EVERY built-in OFF here. (Real OpenCode tool IDs, confirmed
            # against its API: the write-via-diff tool is `patch`, NOT
            # `apply_patch`; `read`/`glob`/`grep`/`list` are the filesystem
            # readers; `task` spawns sub-agents.) Without this the model wanders
            # the disk (e.g. reading recovered projects from the recycle bin)
            # instead of using our MCP tools.
            "bash": False,
            "edit": False,
            "write": False,
            "patch": False,
            "apply_patch": False,  # harmless alias on builds that use this name
            "read": False,
            "glob": False,
            "grep": False,
            "list": False,
            "task": False,
            "todowrite": False,
            "todoread": False,
            # Web tools ride the per-request web toggle. read_pdf (the pdf_tools
            # MCP server) fetches URLs, so it shares the same gate. OpenCode
            # namespaces a local-server tool as "<server>_<tool>" ->
            # "pdf_tools_read_pdf"; VERIFY that exact key on a live model (Gemma
            # retest) — a wrong key is simply ignored.
            "webfetch": request.web_enabled,
            "websearch": request.web_enabled,
            "pdf_tools_read_pdf": request.web_enabled,
            # save_pdf WRITES the fetched datasheet into the documents folder, so
            # it rides BOTH the web gate (it fetches) and the per-chat 💾 toggle.
            "pdf_tools_save_pdf": request.web_enabled and request.save_docs_enabled,
            # Local document tools (sandboxed to the project's documents folder,
            # NO network) — ALWAYS available, even without the 🌐 web toggle, so
            # the LLM can discover + read attached datasheets. read_pdf above
            # stays web-gated because fetching a URL IS a network action.
            "pdf_tools_list_documents": True,
            "pdf_tools_read_document": True,
            # Grammar / rule files (read-only, sandboxed, no network) — also always
            # available so the LLM can pull the procedure grammar it must write in.
            "pdf_tools_list_rules": True,
            "pdf_tools_read_rule": True,
            # Project-data tools ride the per-request project-tools toggle (the
            # project_tools MCP server). Sent explicitly on AND off so the model
            # gets NO project-data access unless the user opts in. OpenCode
            # namespaces a local-server tool as "<server>_<tool>" ->
            # "project_tools_<tool>"; VERIFY those exact keys on a live model
            # (like pdf_tools_read_pdf) — a wrong key is simply ignored.
            "project_tools_list_property_fields": request.project_tools_enabled,
            "project_tools_list_components": request.project_tools_enabled,
            "project_tools_get_component": request.project_tools_enabled,
            "project_tools_query_net": request.project_tools_enabled,
            "project_tools_netlist": request.project_tools_enabled,
            "project_tools_get_bom": request.project_tools_enabled,
            "project_tools_list_test_points": request.project_tools_enabled,
            # run_skill (infra): the recursion tool, gated by ONE per-request bool
            # (True only for a skill declaring `mcp_tools: [run_skill]`). A literal
            # key here is authoritative -- the setdefault loop below cannot clobber it.
            "run_skill_run_skill": request.run_skill_enabled,
        }
        # Skill-owned tools: force every registered skill tool to an explicit
        # on/off (the active skill's servers -> True, all others -> False).
        # OpenCode's tool override is ADDITIVE, so an un-listed tool keeps its
        # enabled default and would leak across skills — this closes that hole.
        from .mcp_config import skill_tool_overrides
        # setdefault (NOT update): a skill tool key is added only if it does not
        # already exist above — so a (trusted) tool folder whose <server>_<tool>
        # flattens onto a built-in/infra key can never clobber it. The literal
        # keys above are always the authority; skills only ADD their own tools.
        for _k, _v in skill_tool_overrides(
                request.skill_servers_enabled, self.config.skill_tools).items():
            body["tools"].setdefault(_k, _v)
        # Message-level system prompt (e.g. the skill chat's SKILL.md) so the
        # caller's instructions GOVERN, rather than sitting in the user body.
        if request.system_prompt:
            if self.config.fold_system_into_prompt:
                # Gemma & co. have no system role in their chat template, so
                # OpenCode/vLLM silently drop body["system"]. Fold the skill
                # into the leading user part instead, so it still governs.
                body["parts"][0]["text"] = (
                    f"{request.system_prompt}\n\n{prompt}")
            else:
                body["system"] = request.system_prompt
        return body

    def _send_via_api(self, prompt: str, request: LLMRequest) -> LLMResponse:
        """Send request via HTTP API."""
        try:
            body = self._build_message_body(prompt, request)

            # Send request
            response = requests.post(
                f"{self.config.server_url}/session/{self._session_id}/message",
                json=body,
                timeout=self.config.request_timeout,
            )

            # Lost server-side session (OpenCode replaced by a fresh instance) ->
            # 404 NotFoundError. Self-heal ONCE: mint a fresh session and replay
            # the client transcript carried on the request as a 'conversation so
            # far' preamble prepended to this prompt, then retry. The preamble is
            # CONSUMED (set None) BEFORE the retry so a second loss returns the
            # error instead of looping -- the hard anti-loop guard. Gated on a
            # non-empty preamble, so it only fires for the dock chat.
            rehydrated = False
            if (
                self._is_session_not_found(response)
                and request.conversation_preamble
            ):
                preamble = request.conversation_preamble
                request.conversation_preamble = None  # consume -> single-shot
                if self._rehydrate_session():
                    rehydrated = True
                    body = self._build_message_body(
                        f"{preamble}\n\n{prompt}", request
                    )
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
                    session_rehydrated=rehydrated,
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
                                session_rehydrated=rehydrated,
                            )
            except (json.JSONDecodeError, KeyError):
                pass  # Not a context error, continue normal parsing
            
            log.debug(f"Raw response preview: {raw_response[:200]}")
            
            # Parse the response
            llm_response = self._response_parser.parse(
                raw_response, request.task, plain_text=request.raw_prompt is not None
            )
            llm_response.session_rehydrated = rehydrated

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
        """Alternative: Send request via CLI (fallback method).

        NOTE: dead path today — both send_request and send_request_streaming go
        through _send_via_api. It does NOT honour request.web_enabled (the `opencode
        run` CLI has no per-request tools override), so if it is ever re-wired the
        web toggle would be silently bypassed.
        """
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
                
                # Run command (cwd anchored to a translatable drive)
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.request_timeout,
                    cwd=safe_wsl_cwd(),
                )
                
                if result.returncode != 0:
                    return LLMResponse(
                        success=False,
                        error_message=f"CLI error: {result.stderr}",
                        raw_response=result.stdout,
                    )
                
                return self._response_parser.parse(
                    result.stdout, request.task, plain_text=request.raw_prompt is not None
                )
                
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
