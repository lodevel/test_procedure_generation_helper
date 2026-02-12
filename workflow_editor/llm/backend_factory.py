"""
Backend Factory - Factory for creating LLM backend instances per-tab.

This module provides:
- BackendConfig: Unified configuration for all backend types
- BackendFactory: Factory for creating backend instances

Design Philosophy:
- Each tab gets its own backend INSTANCE with its own session state
- For OpenCode: Same server, different sessions
- For ExternalAPI: Independent request state
- Factory creates NEW instance on each call (no caching)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .backend_base import LLMBackend, NoneBackend
from .opencode_backend import OpenCodeBackend, OpenCodeConfig
from .external_api_backend import ExternalAPIBackend, ExternalAPIConfig
from .server_manager import OpenCodeServerManager

log = logging.getLogger(__name__)


# Backend type constants
BACKEND_TYPE_OPENCODE = "opencode"
BACKEND_TYPE_EXTERNAL_API = "external_api"
BACKEND_TYPE_NONE = "none"

# List of valid backend types
VALID_BACKEND_TYPES = [BACKEND_TYPE_OPENCODE, BACKEND_TYPE_EXTERNAL_API, BACKEND_TYPE_NONE]


@dataclass
class BackendConfig:
    """
    Unified configuration for backend creation.
    
    This uses COMPOSITION to reference existing config dataclasses
    rather than duplicating all their fields.
    
    Attributes:
        backend_type: Type of backend - "opencode", "external_api", or "none"
        opencode: Configuration for OpenCode backend (optional)
        external_api: Configuration for External API backend (optional)
        custom_prompts: Custom prompt templates keyed by task name
        custom_output_format: Custom output format override
    
    Example:
        # OpenCode configuration
        config = BackendConfig(
            backend_type="opencode",
            opencode=OpenCodeConfig(server_port=4096),
        )
        
        # External API configuration
        config = BackendConfig(
            backend_type="external_api",
            external_api=ExternalAPIConfig(
                base_url="https://api.openai.com/v1",
                model="gpt-4"
            ),
        )
        
        # Disabled
        config = BackendConfig(backend_type="none")
    """
    backend_type: str = BACKEND_TYPE_NONE
    opencode: Optional[OpenCodeConfig] = None
    external_api: Optional[ExternalAPIConfig] = None
    custom_prompts: dict = field(default_factory=dict)
    custom_output_format: str = ""
    
    def __post_init__(self):
        """Validate configuration after initialization."""
        if self.backend_type not in VALID_BACKEND_TYPES:
            log.warning(
                f"Invalid backend_type '{self.backend_type}', "
                f"must be one of {VALID_BACKEND_TYPES}. Defaulting to 'none'."
            )
            self.backend_type = BACKEND_TYPE_NONE
        
        # Ensure config exists for the selected backend type
        if self.backend_type == BACKEND_TYPE_OPENCODE and self.opencode is None:
            log.debug("Creating default OpenCodeConfig")
            self.opencode = OpenCodeConfig()
        
        if self.backend_type == BACKEND_TYPE_EXTERNAL_API and self.external_api is None:
            log.debug("Creating default ExternalAPIConfig")
            self.external_api = ExternalAPIConfig()
    
    @classmethod
    def create_opencode(
        cls,
        server_port: int = 4096,
        server_hostname: str = "127.0.0.1",
        model: Optional[str] = None,
        **kwargs
    ) -> "BackendConfig":
        """
        Convenience method to create OpenCode configuration.
        
        Args:
            server_port: Port for OpenCode server
            server_hostname: Hostname for OpenCode server
            model: Optional model override (e.g., "anthropic/claude-3-5-sonnet")
            **kwargs: Additional arguments for BackendConfig
            
        Returns:
            Configured BackendConfig for OpenCode
        """
        return cls(
            backend_type=BACKEND_TYPE_OPENCODE,
            opencode=OpenCodeConfig(
                server_port=server_port,
                server_hostname=server_hostname,
                model=model,
            ),
            **kwargs
        )
    
    @classmethod
    def create_external_api(
        cls,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4",
        api_key_env_var: str = "OPENAI_API_KEY",
        **kwargs
    ) -> "BackendConfig":
        """
        Convenience method to create External API configuration.
        
        Args:
            base_url: Base URL for the API
            model: Model name
            api_key_env_var: Environment variable containing API key
            **kwargs: Additional arguments for BackendConfig
            
        Returns:
            Configured BackendConfig for External API
        """
        return cls(
            backend_type=BACKEND_TYPE_EXTERNAL_API,
            external_api=ExternalAPIConfig(
                base_url=base_url,
                model=model,
                api_key_env_var=api_key_env_var,
            ),
            **kwargs
        )
    
    @classmethod
    def create_disabled(cls) -> "BackendConfig":
        """
        Create a disabled backend configuration.
        
        Returns:
            BackendConfig with backend_type="none"
        """
        return cls(backend_type=BACKEND_TYPE_NONE)


class BackendFactory:
    """
    Factory for creating LLM backend instances.
    
    This factory creates NEW backend instances for each tab.
    Each instance has its own session state:
    - OpenCode: New session_id per instance (same server)
    - ExternalAPI: Independent request state
    - None: Disabled backend
    
    Design:
    - Factory holds configuration and optional server manager
    - Each create_backend() call returns a NEW instance
    - No caching - caller is responsible for lifecycle
    
    Usage:
        # Setup (typically in MainWindow)
        server_manager = OpenCodeServerManager(config.opencode)
        factory = BackendFactory(config, server_manager)
        
        # In each tab
        backend = factory.create_backend(tab_id="text_json_tab")
        backend.start()
        # ... use backend ...
        backend.stop()
    """
    
    def __init__(
        self,
        config: BackendConfig,
        server_manager: Optional[OpenCodeServerManager] = None
    ):
        """
        Initialize factory with configuration and optional server manager.
        
        Args:
            config: Backend configuration
            server_manager: OpenCode server manager (required for OpenCode backend)
        """
        self._config = config
        self._server_manager = server_manager
        
        log.debug(
            f"BackendFactory initialized: type={config.backend_type}, "
            f"server_manager={'yes' if server_manager else 'no'}"
        )
    
    @property
    def backend_type(self) -> str:
        """
        Get the current backend type.
        
        Returns:
            Backend type string: "opencode", "external_api", or "none"
        """
        return self._config.backend_type
    
    @property
    def config(self) -> BackendConfig:
        """Get the current configuration."""
        return self._config
    
    @property
    def server_manager(self) -> Optional[OpenCodeServerManager]:
        """Get the server manager (if any)."""
        return self._server_manager
    
    def create_backend(self, tab_id: str) -> LLMBackend:
        """
        Create a new backend instance for a specific tab.
        
        Each call creates a NEW instance with its own session state:
        - OpenCode: Creates new session_id (but shares server via manager)
        - ExternalAPI: Independent request state
        - None: Disabled backend (always returns error)
        
        Args:
            tab_id: Identifier for the tab (used for logging)
            
        Returns:
            New LLMBackend instance configured for the tab.
            
        Note:
            The caller is responsible for calling start() on the backend
            and stop() when done. The factory does not track instances.
        """
        backend_type = self._config.backend_type
        log.info(f"Creating {backend_type} backend for tab '{tab_id}'")
        
        if backend_type == BACKEND_TYPE_OPENCODE:
            return self._create_opencode_backend(tab_id)
        
        elif backend_type == BACKEND_TYPE_EXTERNAL_API:
            return self._create_external_api_backend(tab_id)
        
        else:
            log.debug(f"Creating NoneBackend for tab '{tab_id}'")
            return NoneBackend()
    
    def _create_opencode_backend(self, tab_id: str) -> LLMBackend:
        """
        Create an OpenCode backend instance.
        
        The backend shares the server via server_manager but has its own session.
        
        Args:
            tab_id: Tab identifier for logging
            
        Returns:
            OpenCodeBackend instance or NoneBackend on error
        """
        if self._config.opencode is None:
            log.error("OpenCode config is None, cannot create backend")
            return NoneBackend()
        
        if self._server_manager is None:
            log.warning(
                "No server_manager provided for OpenCode backend. "
                "Backend will manage its own server (not recommended)."
            )
            # Create backend without shared server - it will start its own
            return OpenCodeBackend(
                config=self._config.opencode,
                custom_prompts=self._config.custom_prompts or None,
                custom_output_format=self._config.custom_output_format or None,
            )
        
        # Ensure server is running
        if not self._server_manager.is_running:
            log.debug("Server not running, starting via manager...")
            if not self._server_manager.start():
                log.error("Failed to start OpenCode server")
                return NoneBackend()
        
        # Create backend with same config (will create its own session)
        # The backend's start() will detect the running server and create a session
        backend = OpenCodeBackend(
            config=self._config.opencode,
            custom_prompts=self._config.custom_prompts or None,
            custom_output_format=self._config.custom_output_format or None,
        )
        
        log.debug(f"Created OpenCodeBackend for tab '{tab_id}'")
        return backend
    
    def _create_external_api_backend(self, tab_id: str) -> LLMBackend:
        """
        Create an External API backend instance.
        
        Each instance is independent with its own request state.
        
        Args:
            tab_id: Tab identifier for logging
            
        Returns:
            ExternalAPIBackend instance or NoneBackend on error
        """
        if self._config.external_api is None:
            log.error("External API config is None, cannot create backend")
            return NoneBackend()
        
        backend = ExternalAPIBackend(
            config=self._config.external_api,
            custom_prompts=self._config.custom_prompts or None,
            custom_output_format=self._config.custom_output_format or None,
        )
        
        log.debug(f"Created ExternalAPIBackend for tab '{tab_id}'")
        return backend
    
    def is_backend_available(self) -> bool:
        """
        Check if the configured backend is available.
        
        For OpenCode: Checks server_manager.is_available()
        For ExternalAPI: Always returns True
        For None: Returns True (disabled is "available")
        
        Returns:
            True if backend can be used.
        """
        backend_type = self._config.backend_type
        
        if backend_type == BACKEND_TYPE_NONE:
            return True  # Disabled is "available"
        
        if backend_type == BACKEND_TYPE_EXTERNAL_API:
            return True  # External API is always available
        
        if backend_type == BACKEND_TYPE_OPENCODE:
            if self._server_manager:
                return self._server_manager.is_available()
            # No server manager - check directly
            if self._config.opencode:
                temp_manager = OpenCodeServerManager(self._config.opencode)
                return temp_manager.is_available()
            return False
        
        return False
