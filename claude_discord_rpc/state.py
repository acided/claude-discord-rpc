"""Per-session state files shared between hook processes and the daemon."""

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_discord_rpc import paths

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def session_file(session_id: str, directory: Path | None = None) -> Path:
    return (directory or paths.SESSIONS_DIR) / (_SAFE_ID.sub("_", session_id) + ".json")


def read_session(session_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    try:
        with open(session_file(session_id, directory), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_session(session: dict[str, Any], directory: Path | None = None) -> None:
    target = session_file(session["session_id"], directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    # On Windows os.replace fails while another process has the target open - retry briefly.
    for attempt in range(10):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
    tmp.unlink(missing_ok=True)


def delete_session(session_id: str, directory: Path | None = None) -> None:
    try:
        session_file(session_id, directory).unlink(missing_ok=True)
    except OSError:
        pass


PURGE_SECONDS = 24 * 3600


def load_sessions(
    stale_seconds: float,
    directory: Path | None = None,
    now: float | None = None,
    purge_seconds: float = PURGE_SECONDS,
    is_alive: Callable[[dict[str, Any]], bool | None] | None = None,
) -> list[dict[str, Any]]:
    """Sessions to show.

    `is_alive` decides first: True shows the session however long it has been idle, False
    deletes its file (the process is gone). None, or no `is_alive`, falls back to timeouts:
    sessions without activity within `stale_seconds` are only hidden (the user may come
    back and keep the elapsed timer), files are deleted after `purge_seconds`.
    """
    directory = directory or paths.SESSIONS_DIR
    now = now or time.time()
    sessions: list[dict[str, Any]] = []
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return sessions
    for file in files:
        try:
            with open(file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        alive = is_alive(data) if is_alive else None
        if alive:
            sessions.append(data)
            continue
        age = now - float(data.get("updated_at", 0))
        if alive is False or age > purge_seconds:
            try:
                file.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if age > stale_seconds:
            continue
        sessions.append(data)
    return sessions
