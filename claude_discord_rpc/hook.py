"""Claude Code hook entrypoint: update this session's state file and make sure the daemon runs.

Must stay fast, print nothing to stdout (SessionStart/UserPromptSubmit stdout is added to
Claude's context) and always exit 0 so it can never break Claude Code.
"""

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_discord_rpc import config, launcher, lock, paths, phrases, state
from claude_discord_rpc.events import classify, model_from_transcript, pretty_model, resolve_project

SPAWN_COOLDOWN = 5.0


def _log(path: Path, text: str, max_bytes: int = 512 * 1024) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            path.unlink()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def update_session(payload: dict[str, Any], cfg: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """Apply one hook event to the session file. Returns the new session (None if removed)."""
    session_id = payload.get("session_id")
    if not session_id:
        return None
    event_name = payload.get("hook_event_name", "")
    if event_name == "SessionEnd":
        state.delete_session(session_id)
        return None

    now = now or time.time()
    session = state.read_session(session_id) or {
        "session_id": session_id,
        "started_at": now,
        "last_idx": {},
    }
    last_idx: dict[str, int] = session.setdefault("last_idx", {})
    privacy = cfg.get("privacy", {})

    cwd = payload.get("cwd") or session.get("cwd") or str(Path.cwd())
    project = resolve_project(cwd, list(privacy.get("blacklist", [])))
    project_key = [project.kind, project.name]
    if session.get("project_key") != project_key or "details_text" not in session:
        session["details_text"] = phrases.choose(project.pool, {"project": project.name}, last_idx, cfg)
        session["project_key"] = project_key

    secret = project.kind == "secret"
    event = classify(
        payload,
        show_files=bool(privacy.get("show_files", True)) and not secret,
        show_commands=bool(privacy.get("show_commands", True)) and not secret,
    )
    if session.get("state_key") != event.key:
        session["state_text"] = phrases.choose(event.category, event.variables, last_idx, cfg)
        session["state_key"] = event.key
        session["category"] = event.category

    model = pretty_model(payload.get("model"))
    # Hook payloads carry no model; read it from the transcript at turn boundaries
    # (or once, for sessions that started before the hooks were installed).
    needs_model = event_name in ("SessionStart", "Stop") or "model" not in session
    if not model and needs_model and payload.get("transcript_path"):
        model = model_from_transcript(str(payload["transcript_path"]))
    if model:
        session["model"] = model

    session["cwd"] = cwd
    session["updated_at"] = now
    state.write_session(session)
    return session


def ensure_daemon() -> None:
    if lock.is_held(paths.LOCK_FILE):
        return
    try:
        if time.time() - paths.SPAWN_MARKER.stat().st_mtime < SPAWN_COOLDOWN:
            return  # a daemon was just spawned and is still starting
    except OSError:
        pass
    paths.SPAWN_MARKER.touch()

    interpreter, env = launcher.daemon_command()
    daemon = str(paths.PACKAGE_DIR / "daemon.py")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(paths.DATA_DIR),
        "env": env,
    }
    if sys.platform == "win32":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # Escape Claude Code's job object (if any) so the daemon outlives the hook process.
        for flags in (base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base):
            try:
                subprocess.Popen([interpreter, daemon], creationflags=flags, **kwargs)
                return
            except OSError:
                continue
    else:
        subprocess.Popen([interpreter, daemon], start_new_session=True, **kwargs)


def main() -> None:
    raw = b""
    try:
        paths.ensure_dirs()
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8-sig") or "{}")
        cfg = config.load()
        if cfg.get("debug"):
            _log(paths.PAYLOAD_LOG, raw.decode("utf-8", "replace"))
        update_session(payload, cfg)
        if payload.get("hook_event_name") != "SessionEnd":
            ensure_daemon()
    except Exception:  # noqa: BLE001 - a hook must never fail
        _log(paths.HOOK_LOG, traceback.format_exc() + f"payload: {raw[:2000]!r}")
    sys.exit(0)


if __name__ == "__main__":
    main()
