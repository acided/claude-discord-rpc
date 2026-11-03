"""Live Claude Code sessions, as reported by `claude agents --json`.

The CLI verifies the PID and process start time of every registered session, so a session
whose terminal was closed without SessionEnd drops out of the list right away. The daemon
uses that instead of waiting for the stale timeout.
"""

import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

POLL_INTERVAL = 10.0
TIMEOUT = 15.0

log = logging.getLogger("claude-discord-rpc")


def _claude_executable() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    # Native installer location, in case the daemon was started with a reduced PATH.
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    return str(fallback) if fallback.exists() else None


def run_cli() -> str:
    exe = _claude_executable()
    if not exe:
        raise FileNotFoundError("claude executable not found")
    # The daemon has no console: without CREATE_NO_WINDOW every poll would flash a window.
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        [exe, "agents", "--json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT,
        creationflags=flags,
        check=True,
    )
    return result.stdout.decode("utf-8", "replace")


def parse(output: str) -> dict[str, dict[str, Any]]:
    """`claude agents --json` output -> {session_id: entry}."""
    data = json.loads(output)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    return {str(e["sessionId"]): e for e in data if isinstance(e, dict) and e.get("sessionId")}


class AgentsPoller:
    """Polls the CLI at most every `interval` seconds and answers liveness questions."""

    def __init__(self, runner: Callable[[], str] = run_cli, interval: float = POLL_INTERVAL):
        self.runner = runner
        self.interval = interval
        self.live: dict[str, dict[str, Any]] | None = None  # None: unknown (CLI unavailable)
        self.seen: set[str] = set()  # sessions the CLI has listed at least once
        self.polled_at = float("-inf")  # moment of the snapshot in `live`
        self._last_try = float("-inf")
        self._failing = False

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self._last_try < self.interval:
            return
        self._last_try = now
        try:
            live = parse(self.runner())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if not self._failing:
                log.warning("claude agents --json unavailable, using the stale timeout: %s", exc)
            self._failing = True
            self.live = None
            return
        if self._failing:
            log.info("claude agents --json works again")
        self._failing = False
        self.live = live
        self.seen.update(live)
        self.polled_at = now

    def is_alive(self, session: dict[str, Any]) -> bool | None:
        """True: running. False: its process is gone. None: unknown, fall back to the timeout.

        Only a session the CLI has listed before and no longer lists counts as gone. Sessions
        it never lists (`claude -p`, SDK, a parked terminal seen only after parking) stay
        on the timeout, and so does one with hook activity newer than the snapshot.
        """
        if self.live is None:
            return None
        session_id = session.get("session_id")
        if session_id in self.live:
            return True
        if session_id in self.seen and float(session.get("updated_at", 0)) < self.polled_at:
            return False
        return None
