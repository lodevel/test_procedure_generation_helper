"""Procedo Studio's settings file: ``settings.json`` in Studio's settings folder, its path,
load and save."""

import json
import logging
from pathlib import Path

from procedo.core import app_identity

log = logging.getLogger(__name__)


def get_settings_path() -> Path:
    """Get the settings file path: in Procedo Studio's settings folder, the only place Studio
    names it (tests/test_studio_settings_dir.py)."""
    settings_dir = app_identity.studio_settings_dir()
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.json"


def load_settings() -> dict:
    """Load settings from file."""
    path = get_settings_path()
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            # Unreadable or corrupt settings file (JSONDecodeError is a
            # ValueError) — fall back to defaults rather than crash startup.
            log.warning("Could not load settings from %s; using defaults", path)
    return {}


def save_settings(settings: dict):
    """Save settings to file."""
    path = get_settings_path()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)
