"""Procedo Studio's settings file: ``settings.json`` in Studio's settings folder, its path,
load and save."""

import json
import logging
from pathlib import Path

from procedo.core import app_identity

from .fileio import atomic_write_text

log = logging.getLogger(__name__)


def get_settings_path() -> Path:
    """Get the settings file path: in Procedo Studio's settings folder, the only place Studio
    names it (tests/test_studio_settings_dir.py)."""
    settings_dir = app_identity.studio_settings_dir()
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.json"


def get_log_path() -> Path:
    """Studio's log file: in the same folder as its settings."""
    return get_settings_path().with_name("studio.log")


def read_settings() -> dict:
    """The settings as stored ({} when there is no file yet); raises OSError when the file
    cannot be read and ValueError when it is not a JSON object. A writer that keeps the keys
    it does not own reads with this: a fallback to {} would make it wipe them."""
    path = get_settings_path()
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is damaged: {e}") from e
    if not isinstance(settings, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return settings


def load_settings() -> dict:
    """Load settings from file, the defaults ({}) when it cannot be read: start-up never
    crashes on a damaged file."""
    try:
        return read_settings()
    except (OSError, ValueError):
        log.warning("Could not load settings from %s; using defaults", get_settings_path())
        return {}


def save_settings(settings: dict):
    """Save settings to file, whole or not at all (a failed save leaves the old file)."""
    atomic_write_text(get_settings_path(), json.dumps(settings, indent=2))
