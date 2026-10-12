"""Persisted window state and the recent-files list.

A small JSON file keeps this honest: no GSettings schema to compile at install
time, and nothing breaks if the file is deleted or corrupted.
"""

from __future__ import annotations

import json
import os
from typing import Any

from gi.repository import GLib

_MAX_RECENT = 12

_DEFAULTS: dict[str, Any] = {
    "window_width": 1100,
    "window_height": 760,
    "window_maximized": False,
    "sidebar_visible": True,
    "zoom_mode": "fit",          # "fit" | "fill" | "actual" | "custom"
    "continuous_scroll": True,
    "slideshow_interval": 4.0,
    "recent": [],
}


def _config_path() -> str:
    base = GLib.get_user_config_dir() or os.path.expanduser("~/.config")
    return os.path.join(base, "preview", "settings.json")


class Settings:
    def __init__(self) -> None:
        self._data = dict(_DEFAULTS)
        self._path = _config_path()
        self.load()

    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key in _DEFAULTS and isinstance(value, type(_DEFAULTS[key])):
                        self._data[key] = value
        except (OSError, ValueError):
            pass  # First run, or a file we cannot parse: defaults are fine.

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            pass  # Losing preferences must never take the app down.

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, _DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    # -- recent files ------------------------------------------------------
    @property
    def recent(self) -> list[str]:
        return [p for p in self._data.get("recent", []) if os.path.exists(p)]

    def push_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        items = [p for p in self._data.get("recent", []) if p != path]
        items.insert(0, path)
        self._data["recent"] = items[:_MAX_RECENT]

    def clear_recent(self) -> None:
        self._data["recent"] = []
