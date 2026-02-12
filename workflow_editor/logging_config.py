"""
Logging configuration for the Workflow Editor.

Provides a centralized logging setup with configurable levels
and formatted console output for debugging.
"""

import logging
import sys
from pathlib import Path
from typing import Optional


# Default format with timestamp, level, module, and message
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEBUG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    debug: bool = False
) -> None:
    """
    Configure the root logger for the application.
    
    Args:
        level: Logging level (default INFO)
        log_file: Optional path to log file
        debug: If True, use DEBUG level and detailed format
    """
    if debug:
        level = logging.DEBUG
        fmt = DEBUG_FORMAT
    else:
        fmt = DEFAULT_FORMAT
    
    formatter = logging.Formatter(fmt, datefmt="%H:%M:%S")
    
    # Configure root logger for our package
    root_logger = logging.getLogger("workflow_editor")
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        root_logger.debug(f"Logging to file: {log_file}")
    
    root_logger.debug(f"Logging initialized at level {logging.getLevelName(level)}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a module.
    
    Args:
        name: Module name (typically __name__)
    
    Returns:
        Logger instance
    """
    if name.startswith("workflow_editor"):
        return logging.getLogger(name)
    return logging.getLogger(f"workflow_editor.{name}")
