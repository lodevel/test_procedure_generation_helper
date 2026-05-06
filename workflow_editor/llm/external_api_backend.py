"""
External API Backend - LLM backend using OpenAI-compatible API.

Supports any OpenAI-compatible API (OpenAI, Anthropic via proxy, local LLMs, etc.)
"""

import json
import logging
from typing import Optional, Callable
from dataclasses import dataclass
import requests

from .backend_base import LLMBackend, LLMRequest, LLMResponse, LLMTask

log = logging.getLogger(__name__)


@dataclass
class ExternalAPIConfig:
    """Configuration for External API backend."""
    # API settings
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4"
    api_key: Optional[str] = None  # Bearer token; None/empty disables Authorization header

    # Request settings
    temperature: float = 0.2
    request_timeout: float = 120.0
    retry_count: int = 2


class ExternalAPIBackend(LLMBackend):
    """
    LLM backend using OpenAI-compatible API.
    
    Uses standard chat completions endpoint:
    POST /chat/completions
    """
    
    def __init__(self, 
                 config: Optional[ExternalAPIConfig] = None,
                 custom_prompts: Optional[dict] = None,
                 custom_output_format: Optional[str] = None):
        # Call base class init for common initialization
        super().__init__(custom_prompts, custom_output_format)
        
        self.config = config or ExternalAPIConfig()
        self._api_key: Optional[str] = None
    
    @property
    def name(self) -> str:
        return f"External API ({self.config.model})"
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    def is_available(self) -> bool:
        """Check if backend is available (always true for external API)."""
        return True
    
    def start(self) -> bool:
        """Start the backend, reading the API key from config only."""
        with self._lock:
            self._api_key = self.config.api_key or ""
            self._running = True
            return True
    
    def stop(self) -> None:
        """Stop the backend."""
        with self._lock:
            self._running = False
            self._api_key = None
    
    def cancel(self) -> None:
        """Cancel any in-progress request."""
        self._cancel_requested = True
    
    def send_request(
        self, 
        request: LLMRequest,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> LLMResponse:
        """Send a request to the external API."""
        self._cancel_requested = False
        
        if not self._running:
            return LLMResponse(
                success=False,
                error_message="API backend is not started",
            )
        
        try:
            # Build the prompt
            prompt = self._prompt_builder.build(request, output_contract_override=request.output_contract)
            
            if progress_callback:
                progress_callback("Sending request to API...")
            
            # Build messages
            messages = [
                {
                    "role": "system",
                    "content": self._get_system_prompt(request.task),
                },
                {
                    "role": "user",
                    "content": prompt,
                }
            ]
            
            # Build request body. We intentionally omit `max_tokens` so the
            # server uses its own default; the GUI's "Context Window"
            # setting is for UI display only, not for capping output.
            body = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
            }
            
            # Send with retry
            response = self._send_with_retry(body)
            
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
    
    def _send_with_retry(self, body: dict) -> LLMResponse:
        """Send request with retry logic."""
        last_error = None
        
        for attempt in range(self.config.retry_count + 1):
            if self._cancel_requested:
                break
            
            try:
                # Build headers
                headers = {"Content-Type": "application/json"}
                if self._api_key:  # Only add auth if API key exists
                    headers["Authorization"] = f"Bearer {self._api_key}"
                
                # Strip trailing slashes from base_url to avoid double slashes
                base_url = self.config.base_url.rstrip('/')
                
                response = requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=body,
                    timeout=self.config.request_timeout,
                )
                
                if response.status_code == 200:
                    return self._parse_api_response(response.json())
                elif response.status_code == 429:
                    # Rate limited, wait and retry
                    import time
                    time.sleep(2 ** attempt)
                    continue
                else:
                    last_error = f"API error: {response.status_code} - {response.text}"
                    
            except requests.exceptions.Timeout:
                # Treat timeouts as terminal — retrying just adds another
                # full timeout window of waiting. Operator can re-Send if
                # they actually want to try again.
                last_error = "Request timed out"
                break
            except requests.exceptions.RequestException as e:
                last_error = f"Request failed: {str(e)}"
        
        return LLMResponse(
            success=False,
            error_message=last_error or "Unknown error",
        )
    
    def _parse_api_response(self, api_response: dict) -> LLMResponse:
        """Parse the API response."""
        try:
            # Extract content from chat completion response
            choices = api_response.get("choices", [])
            if not choices:
                return LLMResponse(
                    success=False,
                    error_message="No response choices returned",
                    raw_response=json.dumps(api_response),
                )
            
            message = choices[0].get("message", {})
            content = message.get("content", "")
            
            # Parse the content as our response format
            response = self._response_parser.parse(content, None)
            
            # Extract token usage using base class method
            prompt_tokens, completion_tokens, total_tokens = self._extract_token_usage(api_response)
            response.prompt_tokens = prompt_tokens
            response.completion_tokens = completion_tokens
            response.total_tokens = total_tokens
            
            return response
            
        except Exception as e:
            return LLMResponse(
                success=False,
                error_message=f"Failed to parse API response: {str(e)}",
                raw_response=json.dumps(api_response),
            )
