"""Config loading: defaults deep-merged with the user's TOML file."""

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

from claude_discord_rpc.paths import CONFIG_FILE

# Shared "Claude Code" Discord application. Client IDs are public identifiers, not secrets.
DEFAULT_CLIENT_ID = "1567121272188436551"

DEFAULTS: dict[str, Any] = {
    "client_id": DEFAULT_CLIENT_ID,
    "debug": False,
    "privacy": {
        "show_files": True,
        "show_commands": True,
        # Glob patterns matched against the project name and the full project path.
        "blacklist": [],
    },
    "timing": {
        # Ask `claude agents --json` which sessions are still running; stale_minutes is then
        # only a fallback for sessions the CLI doesn't list.
        "process_check": True,
        "stale_minutes": 30,
        "idle_exit_seconds": 120,
    },
    # Asset keys (or https URLs) of the shared app. Texts are templates: {model}, {state}.
    "images": {
        "large": "logo",
        "large_text": "claude.ai",
        "small": "claude-code",
        "small_text": "Clawd · {model}",
        # category -> asset key, overrides `small` for that state, e.g. editing = "edit"
        "small_states": {},
    },
    # [[buttons]] label = "...", url = "https://..." (max 2)
    "buttons": [],
    # category -> list of templates; replaces the default pool for that category
    "phrases": {},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load(path: Path = CONFIG_FILE) -> dict[str, Any]:
    user: dict[str, Any] = {}
    try:
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
    except FileNotFoundError:
        pass
    cfg = _merge(DEFAULTS, user)
    env_id = os.environ.get("CLAUDE_DISCORD_CLIENT_ID")
    if env_id:
        cfg["client_id"] = env_id
    cfg["client_id"] = str(cfg["client_id"]).strip() or DEFAULT_CLIENT_ID
    return cfg


class ConfigCache:
    """Reloads the config only when the file's mtime changes (used by the daemon)."""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self._mtime: float | None = -1.0
        self._cfg: dict[str, Any] = load(path)

    def get(self) -> dict[str, Any]:
        try:
            mtime: float | None = self.path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            self._cfg = load(self.path)
        return self._cfg
