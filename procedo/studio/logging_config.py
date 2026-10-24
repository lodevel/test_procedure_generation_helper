"""
Logging configuration for the Workflow Editor.

Provides a centralized logging setup with configurable levels
and formatted console output for debugging.
"""

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional


# Default format with timestamp, level, module, and message
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEBUG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"

#: Studio's own log (studio_settings.get_log_path); it keeps LOG_BACKUPS older files of LOG_MAX_BYTES.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 2


class _StderrToLog:
    """Studio's ``sys.stderr``: each line written lands in the log, whatever started Studio."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            if line.strip():
                self._logger.error("stderr: %s", line)
        return len(text)

    def flush(self) -> None:
        pass


def _log_uncaught(logger: logging.Logger) -> None:
    """Uncaught exceptions (main thread and threads) go to *logger*, and still to the stderr
    the process was started with (Bench reads it to report a failed start; a closed one is skipped)."""
    def hook(exc_type, exc, tb):
        logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        if sys.__stderr__ is not None:
            try:
                traceback.print_exception(exc_type, exc, tb, file=sys.__stderr__)
            except (OSError, ValueError):
                pass

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    debug: bool = False
) -> None:
    """
    Configure the root logger for the application.
    
    Args:
        level: Logging level (default INFO)
        log_file: Path of the log file (default: studio.log in Studio's settings folder)
        debug: If True, use DEBUG level and detailed format
    """
    if debug:
        level = logging.DEBUG
        fmt = DEBUG_FORMAT
    else:
        fmt = DEFAULT_FORMAT
    
    formatter = logging.Formatter(fmt, datefmt="%H:%M:%S")
    
    # Configure root logger for our package
    root_logger = logging.getLogger("procedo.studio")
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    
    # Console handler (a detached or windowed start has no stdout)
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # File handler: always, so a Studio started by Bench or alone logs to the same file
    if log_file is None:
        from .core import studio_settings
        log_file = studio_settings.get_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8', delay=True)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.debug(f"Logging to file: {log_file}")

    sys.stderr = _StderrToLog(root_logger)
    _log_uncaught(root_logger)
    
    root_logger.debug(f"Logging initialized at level {logging.getLevelName(level)}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a module.
    
    Args:
        name: Module name (typically __name__)
    
    Returns:
        Logger instance
    """
    if name.startswith("procedo.studio"):
        return logging.getLogger(name)
    return logging.getLogger(f"procedo.studio.{name}")
