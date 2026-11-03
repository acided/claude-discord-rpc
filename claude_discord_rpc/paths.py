"""Filesystem locations used by the hook and the daemon."""

import os
from pathlib import Path

APP_NAME = "claude-discord-rpc"


def _base(env_var: str, fallback: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else fallback


# Runtime state (session files, lock, logs) - machine-local.
DATA_DIR = _base("LOCALAPPDATA", Path.home() / ".local" / "share") / APP_NAME
SESSIONS_DIR = DATA_DIR / "sessions"
LOCK_FILE = DATA_DIR / "daemon.lock"
SPAWN_MARKER = DATA_DIR / "daemon.spawn"
STOP_FLAG = DATA_DIR / "daemon.stop"
DAEMON_LOG = DATA_DIR / "daemon.log"
HOOK_LOG = DATA_DIR / "hook-errors.log"
PAYLOAD_LOG = DATA_DIR / "payloads.log"

# User configuration - lives outside the repo so client_id / blacklist never get committed.
CONFIG_DIR = _base("APPDATA", Path.home() / ".config") / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.toml"

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent


def ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
