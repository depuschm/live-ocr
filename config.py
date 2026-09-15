#!/usr/bin/env python3
"""
config.py - persist regions and settings between runs.

Stored as JSON in the user's home directory. A missing or unreadable file is
never fatal: the app just starts empty.
"""

import json
import os
import tempfile
from pathlib import Path

from capture import Region

CONFIG_DIR = Path.home() / ".live-ocr"
CONFIG_PATH = CONFIG_DIR / "config.json"
VERSION = 1

DEFAULT_SETTINGS = {
    "interval": 1.0,
    "enhance": True,
    "new_lines_only": True,
    "scale_with_window": False,
    "always_on_top": False,
}


def load():
    """Return (regions, settings). Never raises."""
    settings = dict(DEFAULT_SETTINGS)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return [], settings

    if not isinstance(data, dict):
        return [], settings

    saved = data.get("settings")
    if isinstance(saved, dict):
        # Only take keys we recognise, so an old or hand-edited file cannot
        # inject junk attributes.
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})

    regions = []
    for entry in data.get("regions", []):
        try:
            regions.append(Region.from_dict(entry))
        except Exception:
            continue  # skip one malformed region rather than losing them all

    return regions, settings


def save(regions, settings):
    """Write atomically so a crash mid-write cannot corrupt the config."""
    payload = {
        "version": VERSION,
        "settings": {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()},
        "regions": [r.to_dict() for r in regions],
    }

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError:
        return False
    return True
