"""
Backend Factory - Factory for creating LLM backend instances per-tab.

This module provides:
- BackendConfig: Unified configuration for the OpenCode backend
- BackendFactory: Factory for creating backend instances

Design Philosophy:
- Each tab gets its own backend INSTANCE with its own session state
- For OpenCode: Same server, different sessions
- Factory creates NEW instance on each call (no caching)
"""

import logging
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Optional

from .backend_base import LLMBackend, NoneBackend
from .lifecycle_adapters import ManagerLifecycle
from .opencode_backend import OpenCodeBackend, OpenCodeConfig
from .server_manager import OpenCodeServerManager

log = logging.getLogger(__name__)


def start_backend(factory, tab_id: str) -> tuple[LLMBackend, bool]:
    """A new backend for *tab_id* from *factory*, started, and whether to keep it: what a
    Studio tab and a skill chat make on first use (off the GUI thread:
    ``procedo.studio.dock.backend_request``).

    A ``NoneBackend`` the factory answers as a *fallback* (the OpenCode server would not start)
    is not kept, so the next use tries the factory, and the server, again; one is kept when
    the configured backend type is ``"none"``, and without a factory."""
    if factory is None:
        log.warning(f"No backend factory for '{tab_id}'")
        return NoneBackend(), True
    backend = factory.create_backend(tab_id=tab_id)
    if hasattr(backend, "start"):
        backend.start()
    keep = not isinstance(backend, NoneBackend) or factory.backend_type == BACKEND_TYPE_NONE
    if not keep:
        log.info(f"Fallback NoneBackend for '{tab_id}': the next use tries again")
    return backend, keep


# Backend type constants
BACKEND_TYPE_OPENCODE = "opencode"
BACKEND_TYPE_NONE = "none"

# List of valid backend types
VALID_BACKEND_TYPES = [BACKEND_TYPE_OPENCODE, BACKEND_TYPE_NONE]


@dataclass
class BackendConfig:
    """
    Unified configuration for backend creation.

    Attributes:
        backend_type: Type of backend - "opencode" or "none"
        opencode: Configuration for OpenCode backend (optional)
        custom_prompts: Custom prompt templates keyed by task name
        custom_output_format: Custom output format override

    Example:
        # OpenCode configuration
        config = BackendConfig(
            backend_type="opencode",
            opencode=OpenCodeConfig(server_port=4096),
        )

        # Disabled
        config = BackendConfig(backend_type="none")
    """
    backend_type: str = BACKEND_TYPE_NONE
    opencode: Optional[OpenCodeConfig] = None
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
        # The server start running now, shared by every thread needing the server meanwhile.
        self._server_start: Optional[Future] = None
        self._server_start_lock = threading.Lock()

        log.debug(
            f"BackendFactory initialized: type={config.backend_type}, "
            f"server_manager={'yes' if server_manager else 'no'}"
        )

    @property
    def backend_type(self) -> str:
        """
        Get the current backend type.

        Returns:
            Backend type string: "opencode" or "none"
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
        # The server manager is the one spawner: it alone stops the server it started.
        if self._config.opencode is None or self._server_manager is None:
            log.error("OpenCode backend needs its config and the server manager")
            return NoneBackend()

        # Ensure server is running
        if not self._server_manager.is_running:
            log.debug("Server not running, starting via manager...")
            if not self._start_server():
                status = self._server_manager.last_status
                reason = status.message if status else None
                log.error(f"Failed to start OpenCode server: {reason}")
                # NoneBackend surfaces the classified reason when the user chats.
                return NoneBackend(reason=reason)

        # Create backend bound to the shared server via the manager (per-tab
        # session, one shared `opencode serve`). ManagerLifecycle delegates
        # server lifetime to the manager; the backend only reads its URL.
        backend = OpenCodeBackend(
            config=self._config.opencode,
            lifecycle=ManagerLifecycle(self._server_manager),
            custom_prompts=self._config.custom_prompts or None,
            custom_output_format=self._config.custom_output_format or None,
        )

        log.debug(f"Created OpenCodeBackend for tab '{tab_id}'")
        return backend

    def _start_server(self) -> bool:
        """Start the shared server: ONE attempt for every thread that asks while it runs (each
        waits for it and gets its outcome), never a second start stacked behind it."""
        with self._server_start_lock:
            attempt, mine = self._server_start, self._server_start is None
            if mine:
                attempt = self._server_start = Future()
        if not mine:
            return attempt.result()
        ok = False
        try:
            ok = self._server_manager.start()
        finally:
            with self._server_start_lock:
                self._server_start = None
            attempt.set_result(ok)
        return ok

    def is_backend_available(self) -> bool:
        """
        Check if the configured backend is available.

        For OpenCode: Checks server_manager.is_available()
        For None: Returns True (disabled is "available")

        Returns:
            True if backend can be used.
        """
        backend_type = self._config.backend_type

        if backend_type == BACKEND_TYPE_NONE:
            return True  # Disabled is "available"

        if backend_type == BACKEND_TYPE_OPENCODE:
            if self._server_manager:
                return self._server_manager.is_available()
            # No server manager - check directly
            if self._config.opencode:
                temp_manager = OpenCodeServerManager(self._config.opencode)
                return temp_manager.is_available()
            return False

        return False
