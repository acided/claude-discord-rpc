"""Live sessions + config -> Discord activity payload (pure, apart from the default clock)."""

import time
import zlib
from typing import Any

from claude_discord_rpc.phrases import LABELS, MAX_LEN, pool_for, render, truncate

WAITING_CATEGORIES = {"approval", "question"}
PERMISSION_WAITS = {"permission prompt", "sandbox request"}
# The CLI status is applied only if no hook event arrived within this many seconds before the
# snapshot: right after a hook the registry may not have caught up yet.
STATUS_MARGIN = 3.0
OTHERS_WAITING = "{n} waiting on you"
IDLE_CATEGORIES = {"idle", "starting"}
# Same value as hook.AGENT_STALE_SECONDS: a subagent quiet for this long is not shown.
AGENT_STALE_SECONDS = 15 * 60


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = truncate(value, MAX_LEN)
    return value if len(value) >= 2 else None  # Discord rejects strings shorter than 2 chars


def active_agents(session: dict[str, Any], now: float) -> list[dict[str, Any]]:
    agents = session.get("agents")
    if not isinstance(agents, dict):
        return []
    return [
        a for a in agents.values()
        if isinstance(a, dict) and now - float(a.get("updated_at", 0)) <= AGENT_STALE_SECONDS
    ]


def _agents_label(agents: list[dict[str, Any]]) -> str:
    if len(agents) == 1:
        kind = agents[0].get("type")
        return f"the {kind} agent" if kind else "a subagent"
    return f"{len(agents)} subagents" if agents else "subagents"


def _rank(session: dict[str, Any], now: float) -> int:
    """0: working (itself or through subagents), 1: waiting on the user, 2: idle."""
    category = session.get("category")
    if category in WAITING_CATEGORIES:
        return 1
    if category in IDLE_CATEGORIES and not active_agents(session, now):
        return 2
    return 0


def pick_session(
    sessions: list[dict[str, Any]], previous_id: str | None = None, now: float | None = None
) -> dict[str, Any]:
    """The session to show: the busiest tier wins, and within it the one already shown stays
    (two sessions working in parallel don't take turns), else the most recently active."""
    now = time.time() if now is None else now
    best = min(_rank(s, now) for s in sessions)
    candidates = [s for s in sessions if _rank(s, now) == best]
    for s in candidates:
        if previous_id is not None and s.get("session_id") == previous_id:
            return s
    return max(candidates, key=lambda s: float(s.get("updated_at", 0)))


def _state_text(session: dict[str, Any], now: float) -> str | None:
    agents = active_agents(session, now)
    template = session.get("state_template")
    if isinstance(template, str):
        variables = session.get("state_vars")
        variables = {**(variables if isinstance(variables, dict) else {}), "agents": _agents_label(agents)}
        text: Any = render(template, variables)
    else:
        text = session.get("state_text")  # written by an older hook version
    if not isinstance(text, str):
        return None
    if agents and session.get("category") != "subagents":
        text += f" · {len(agents)} subagent" + ("s" if len(agents) > 1 else "")  # background agents
    return text


def _cli_category(session: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """Category the CLI status implies, or None when the hook-based one is consistent with it."""
    status = entry.get("status")
    category = session.get("category")
    if status == "idle" and category != "idle":
        return "idle"  # e.g. a turn interrupted with Esc: no Stop hook fires
    if status == "waiting" and category not in WAITING_CATEGORIES:
        # blocked on a prompt or dialog that no hook reported
        return "approval" if entry.get("waitingFor") in PERMISSION_WAITS else "question"
    if status == "busy" and category == "idle":
        return "thinking"  # a turn started without UserPromptSubmit (e.g. a task notification)
    return None


def apply_cli_status(
    session: dict[str, Any], entry: dict[str, Any] | None, snapshot_at: float, cfg: dict[str, Any]
) -> dict[str, Any]:
    """Correct a session's hook-based state with the status from `claude agents --json`."""
    if entry is None or snapshot_at - float(session.get("updated_at", 0)) < STATUS_MARGIN:
        return session
    category = _cli_category(session, entry)
    if category is None:
        return session
    # Stable phrase per session and state: the daemon re-evaluates this every tick.
    pool = pool_for(category, cfg)
    idx = zlib.crc32(f"{session.get('session_id')}|{category}".encode()) % len(pool)
    template = pool[idx]
    return {
        **session,
        "category": category,
        "state_template": template,
        "state_vars": {},
        "state_text": render(template, {}),
    }


def _is_waiting(session: dict[str, Any]) -> bool:
    return session.get("category") in WAITING_CATEGORIES


def build(
    sessions: list[dict[str, Any]],
    cfg: dict[str, Any],
    live: dict[str, dict[str, Any]] | None = None,
    snapshot_at: float = float("-inf"),
) -> dict[str, Any] | None:
    return compose(sessions, cfg, live, snapshot_at)[0]


def compose(
    sessions: list[dict[str, Any]],
    cfg: dict[str, Any],
    live: dict[str, dict[str, Any]] | None = None,
    snapshot_at: float = float("-inf"),
    previous_id: str | None = None,
    now: float | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Activity plus the id of the session it shows.

    `live` is the last `claude agents --json` listing (by session id), taken at `snapshot_at`;
    `previous_id` is the session shown last time, kept on screen while it stays as busy as the rest.
    """
    if not sessions:
        return None, None
    now = time.time() if now is None else now
    live = live or {}
    sessions = [apply_cli_status(s, live.get(str(s.get("session_id"))), snapshot_at, cfg) for s in sessions]
    known = {str(s.get("session_id")) for s in sessions}
    # Running sessions without hook data yet still count.
    cli_only = [e for sid, e in live.items() if sid not in known]
    session = pick_session(sessions, previous_id, now)
    count = len(sessions) + len(cli_only)
    others_waiting = sum(_is_waiting(s) for s in sessions if s is not session)
    others_waiting += sum(e.get("status") == "waiting" for e in cli_only)
    category = session.get("category", "working")

    activity: dict[str, Any] = {}
    details = _text(session.get("details_text")) or "Claude Code"
    activity["details"] = details
    state_text = _state_text(session, now)
    if others_waiting and isinstance(state_text, str):
        state_text = f"{state_text} · {OTHERS_WAITING.format(n=others_waiting)}"
    state = _text(state_text)
    if state:
        activity["state"] = state
    started = session.get("started_at")
    if started:
        activity["timestamps"] = {"start": int(float(started))}

    images = cfg.get("images", {})
    variables = {"model": session.get("model") or "Claude", "state": LABELS.get(category, "Working")}
    sessions_suffix = f" · {count} sessions" if count > 1 else ""
    large_text = render(str(images.get("large_text") or "Claude Code"), variables)
    small_text = render(str(images.get("small_text") or "{state}"), variables)
    states = images.get("small_states")
    small_key = (states.get(category) if isinstance(states, dict) else None) or images.get("small")

    assets: dict[str, str] = {}
    if small_key and isinstance(small_key, str):
        assets["small_image"] = small_key
        assets["small_text"] = _text(small_text + sessions_suffix) or "Claude Code"
    else:
        large_text += sessions_suffix  # no badge - show the session count on the large image
    if images.get("large"):
        assets["large_image"] = str(images["large"])
        assets["large_text"] = _text(large_text) or "Claude Code"
    if assets:
        activity["assets"] = assets

    buttons = []
    for button in cfg.get("buttons", [])[:2]:
        if isinstance(button, dict) and button.get("label") and str(button.get("url", "")).startswith("http"):
            buttons.append({"label": str(button["label"])[:32], "url": str(button["url"])})
    if buttons:
        activity["buttons"] = buttons
    return activity, str(session.get("session_id"))
