"""Pure function: live sessions + config -> Discord activity payload."""

from typing import Any

from claude_discord_rpc.phrases import LABELS, MAX_LEN, render, truncate


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = truncate(value, MAX_LEN)
    return value if len(value) >= 2 else None  # Discord rejects strings shorter than 2 chars


def pick_session(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    return max(sessions, key=lambda s: float(s.get("updated_at", 0)))


def build(sessions: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any] | None:
    if not sessions:
        return None
    session = pick_session(sessions)
    count = len(sessions)
    category = session.get("category", "working")

    activity: dict[str, Any] = {}
    details = _text(session.get("details_text")) or "Claude Code"
    activity["details"] = details
    state = _text(session.get("state_text"))
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
    return activity
