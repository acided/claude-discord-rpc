"""Background process that owns the Discord IPC connection and mirrors session state into presence.

Started automatically by the hook; exits on its own when no Claude Code sessions are left.
Usage: daemon.py [--debug]   (run in a console to watch logs)
       daemon.py --stop      (ask a running daemon to clear presence and exit)
"""

import argparse
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_discord_rpc import config, lock, paths, presence, state
from claude_discord_rpc.agents import AgentsPoller
from claude_discord_rpc.ipc import DiscordIPC

TICK = 2.0
MIN_SEND_INTERVAL = 4.0  # Discord allows ~5 SET_ACTIVITY per 20 s
RECONNECT_INTERVAL = 15.0
# Re-send an unchanged activity periodically: Discord silently drops it in some cases
# (e.g. a client reload right after we reconnect) and gives no event we could react to.
REFRESH_INTERVAL = 30.0

log = logging.getLogger("claude-discord-rpc")


def _setup_logging(debug: bool) -> None:
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        paths.DAEMON_LOG, maxBytes=256 * 1024, backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if debug and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        log.addHandler(console)


class Daemon:
    def __init__(self) -> None:
        self.cfg_cache = config.ConfigCache()
        self.agents = AgentsPoller()
        self.shown_id: str | None = None
        self.rpc: DiscordIPC | None = None
        self.last_activity: Any = object()  # sentinel: forces the first send
        self.last_send = 0.0
        self.last_connect_try = float("-inf")
        self.empty_since: float | None = None
        self.warned_no_client_id = False

    def _disconnect(self) -> None:
        if self.rpc:
            self.rpc.close()
        self.rpc = None
        self.last_activity = object()

    def _connect(self, client_id: str, now: float) -> None:
        if now - self.last_connect_try < RECONNECT_INTERVAL:
            return
        self.last_connect_try = now
        rpc = DiscordIPC(client_id)
        try:
            ready = rpc.connect()
        except OSError as exc:
            log.debug("connect failed: %s", exc)
            return
        user = (ready.get("data") or {}).get("user") or {}
        log.info("connected to Discord as %s", user.get("username", "?"))
        self.rpc = rpc
        self.last_activity = object()

    def _send(self, activity: dict[str, Any] | None, now: float) -> None:
        if self.rpc is None:
            return
        if activity == self.last_activity and (activity is None or now - self.last_send < REFRESH_INTERVAL):
            return
        if now - self.last_send < MIN_SEND_INTERVAL:
            return  # debounce; the latest state is sent on a later tick
        try:
            response = self.rpc.set_activity(activity)
        except (OSError, ValueError) as exc:
            log.warning("send failed, reconnecting: %s", exc)
            self._disconnect()
            return
        if response.get("evt") == "ERROR":
            log.error("Discord rejected activity: %s", response.get("data"))
        else:
            log.debug("activity sent: %s -> %s", activity, response)
        self.last_activity = activity
        self.last_send = now

    def tick(self) -> bool:
        """One loop iteration. Returns False when the daemon should exit."""
        now = time.time()
        cfg = self.cfg_cache.get()
        timing = cfg.get("timing", {})
        is_alive = None
        if timing.get("process_check", True):
            self.agents.poll(now)
            is_alive = self.agents.is_alive
        sessions = state.load_sessions(float(timing.get("stale_minutes", 30)) * 60, now=now, is_alive=is_alive)

        if sessions:
            self.empty_since = None
        else:
            self.empty_since = self.empty_since or now
            if now - self.empty_since > float(timing.get("idle_exit_seconds", 120)):
                log.info("no sessions left, exiting")
                return False

        client_id = cfg.get("client_id", "")
        if not client_id:
            if not self.warned_no_client_id:
                log.warning("client_id is empty, check %s", paths.CONFIG_FILE)
                self.warned_no_client_id = True
            return True
        self.warned_no_client_id = False

        if self.rpc and self.rpc.client_id != client_id:
            log.info("client_id changed, reconnecting")
            self._disconnect()
            self.last_connect_try = float("-inf")
        if self.rpc is None:
            self._connect(client_id, now)

        live = self.agents.live if is_alive else None
        activity, self.shown_id = presence.compose(
            sessions, cfg, live, self.agents.polled_at, previous_id=self.shown_id, now=now
        )
        self._send(activity, now)
        return True

    def run(self) -> None:
        try:
            while True:
                if paths.STOP_FLAG.exists():
                    paths.STOP_FLAG.unlink(missing_ok=True)
                    log.info("stop requested")
                    break
                try:
                    if not self.tick():
                        break
                except Exception:  # noqa: BLE001 - keep the daemon alive on unexpected errors
                    log.exception("tick failed")
                time.sleep(TICK)
        finally:
            if self.rpc:
                try:
                    self.rpc.set_activity(None)
                except OSError:
                    pass
            self._disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="verbose logging, also to the console")
    parser.add_argument("--stop", action="store_true", help="ask the running daemon to exit")
    args = parser.parse_args()

    paths.ensure_dirs()
    if args.stop:
        if lock.is_held(paths.LOCK_FILE):
            paths.STOP_FLAG.touch()
            print("stop requested")
        else:
            print("daemon is not running")
        return

    fd = lock.acquire(paths.LOCK_FILE)
    if fd is None:
        if args.debug:
            print("daemon is already running; stop it with --stop first")
        return
    paths.STOP_FLAG.unlink(missing_ok=True)
    _setup_logging(args.debug)
    log.info("daemon started")
    Daemon().run()
    log.info("daemon stopped")


if __name__ == "__main__":
    main()
