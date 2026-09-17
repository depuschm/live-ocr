#!/usr/bin/env python3
"""
config.py - named profiles, each with its own regions and settings.

Layout:
    ~/.live-ocr/
        state.json            which profile was last active
        profiles/
            my-project.json   one file per profile

One file per profile rather than one big file, so a profile can be copied,
shared, or dropped into a project repo on its own.

Nothing here raises on a bad file: a missing or corrupt profile loads empty.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from capture import Region

CONFIG_DIR = Path.home() / ".live-ocr"
PROFILE_DIR = CONFIG_DIR / "profiles"
CAPTURE_DIR = CONFIG_DIR / "captures"
STATE_PATH = CONFIG_DIR / "state.json"

VERSION = 1
DEFAULT_PROFILE = "Default"

DEFAULT_SETTINGS = {
    "interval": 1.0,
    "enhance": True,
    "new_lines_only": True,
    "scale_with_window": False,
    "always_on_top": False,
    "preview_processed": False,
    "jsonl_enabled": False,
    "jsonl_path": "",
    "webhook_enabled": False,
    "webhook_url": "",
    "webhook_cooldown": 10.0,
    "snapshot_events": False,
}


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def slug(name):
    """Filesystem-safe stem for a profile name."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-. ")
    return (s or "profile").lower()[:60]


def path_for(name):
    return PROFILE_DIR / f"{slug(name)}.json"


def default_capture_path(name):
    """Where a profile's JSONL lands unless the user picks somewhere else."""
    return CAPTURE_DIR / f"{slug(name)}.jsonl"


def _write_json(path, payload):
    """Atomic write, so an interrupted save cannot truncate the file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError:
        return False
    return True


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def open_folder():
    """
    Show the config directory in the OS file manager.

    Uses Popen rather than run so a slow file manager cannot block the UI
    thread. Returns False if the platform command is missing.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)  # may not exist yet
        if sys.platform == "win32":
            os.startfile(CONFIG_DIR)  # noqa: S606 - Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(CONFIG_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(CONFIG_DIR)])
    except (OSError, AttributeError):
        return False
    return True


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


def list_profiles():
    """Display names of every stored profile, sorted."""
    names = []
    try:
        files = sorted(PROFILE_DIR.glob("*.json"))
    except OSError:
        return [DEFAULT_PROFILE]

    for f in files:
        data = _read_json(f)
        names.append((data or {}).get("name") or f.stem)

    return sorted(set(names), key=str.lower) or [DEFAULT_PROFILE]


def load(name):
    """Return (regions, settings) for a profile. Never raises."""
    settings = dict(DEFAULT_SETTINGS)
    data = _read_json(path_for(name))
    if data is None:
        return [], settings

    saved = data.get("settings")
    if isinstance(saved, dict):
        # Only recognised keys, so an old or hand-edited file cannot inject
        # junk attributes.
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})

    regions = []
    for entry in data.get("regions", []):
        try:
            regions.append(Region.from_dict(entry))
        except Exception:
            continue  # skip one malformed region rather than losing them all

    return regions, settings


def save(name, regions, settings):
    payload = {
        "version": VERSION,
        "name": name,
        "settings": {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()},
        "regions": [r.to_dict() for r in regions],
    }
    return _write_json(path_for(name), payload)


def delete(name):
    try:
        path_for(name).unlink()
        return True
    except OSError:
        return False


def rename(old, new):
    """Returns True on success, False if the target name is already taken."""
    src, dst = path_for(old), path_for(new)
    if src == dst:  # same slug, e.g. only capitalisation changed
        data = _read_json(src) or {}
        data["name"] = new
        return _write_json(dst, data)
    if dst.exists():
        return False
    data = _read_json(src) or {"version": VERSION, "regions": [], "settings": {}}
    data["name"] = new
    if not _write_json(dst, data):
        return False
    try:
        src.unlink()
    except OSError:
        pass
    return True


def exists(name):
    return path_for(name).exists()


# --------------------------------------------------------------------------
# Active profile
# --------------------------------------------------------------------------


def get_state():
    """Whole UI state blob: active profile, window geometry."""
    return _read_json(STATE_PATH) or {}


def set_state(**values):
    """Merge into state.json, so one key cannot clobber the others."""
    data = get_state()
    data.update(values)
    return _write_json(STATE_PATH, data)


def get_active():
    name = get_state().get("active")
    if name and exists(name):
        return name
    known = list_profiles()
    return known[0] if known else DEFAULT_PROFILE


def set_active(name):
    return set_state(active=name)


def get_geometry():
    geo = get_state().get("geometry")
    # Only accept WxH or WxH+X+Y, so a hand-edited file cannot wedge the app
    # into an unusable size.
    if isinstance(geo, str) and re.fullmatch(r"\d+x\d+([+-]-?\d+[+-]-?\d+)?", geo):
        return geo
    return None


def set_geometry(geo):
    return set_state(geometry=geo)
