"""Phrase pools per state, no-repeat picking and safe rendering."""

import random
from typing import Any

MAX_LEN = 128  # Discord limit for details / state / large_text
MAX_FILE_LEN = 48

DEFAULT_POOLS: dict[str, list[str]] = {
    # Top line (details), picked once per session/project.
    "details": [
        "Working on {project}",
        "Building {project}",
        "Deep in {project}",
        "Crafting {project}",
        "Shipping {project}",
    ],
    "home": [
        "Working in terminal",
        "Hanging out in ~",
        "Freestyling in the terminal",
    ],
    "secret": [
        "Secret project",
        "Working on something secret 🤫",
        "Classified project",
    ],
    # Bottom line (state), picked on every state change.
    "starting": [
        "Starting session",
        "Booting up",
        "Warming up",
    ],
    "thinking": [
        "Thinking…",
        "Pondering the prompt",
        "Connecting the dots",
        "Cooking something up",
        "Reasoning it through",
    ],
    "editing": [
        "Editing {file}",
        "Tweaking {file}",
        "Rewriting {file}",
        "Polishing {file}",
        "Hacking on {file}",
    ],
    "reading": [
        "Reading {file}",
        "Studying {file}",
        "Skimming {file}",
        "Digging into {file}",
    ],
    "searching": [
        "Searching codebase",
        "Grepping around",
        "Hunting for clues",
        "Exploring the repo",
    ],
    "running": [
        "Running {cmd}",
        "Executing {cmd}",
        "Firing up {cmd}",
        "Typing {cmd} in the terminal",
    ],
    "web": [
        "Browsing the web",
        "Reading the docs",
        "Googling stuff",
    ],
    "subagents": [
        "Delegating to subagents",
        "Spawning helpers",
        "Running a team of agents",
    ],
    "mcp": [
        "Using {server}",
        "Talking to {server}",
        "Calling {server} tools",
    ],
    "planning": [
        "Planning next steps",
        "Making a to-do list",
        "Drafting a plan",
    ],
    "question": [
        "Asking a question",
        "Needs your input",
        "Waiting for an answer",
    ],
    "approval": [
        "Waiting for approval",
        "Needs a green light",
        "Asking permission",
    ],
    "idle": [
        "Waiting for input",
        "Awaiting orders",
        "Ready for the next task",
        "Idle, coffee break",
    ],
    "working": [
        "Working…",
        "Using {tool}",
        "Getting things done",
    ],
}

# Human label of a category, used as small_text tooltip.
LABELS: dict[str, str] = {
    "starting": "Starting",
    "thinking": "Thinking",
    "editing": "Editing",
    "reading": "Reading",
    "searching": "Searching",
    "running": "Terminal",
    "web": "Web",
    "subagents": "Subagents",
    "mcp": "MCP",
    "planning": "Planning",
    "question": "Question",
    "approval": "Approval",
    "idle": "Idle",
    "working": "Working",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "something"


def pool_for(category: str, cfg: dict[str, Any] | None = None) -> list[str]:
    overrides = (cfg or {}).get("phrases", {})
    custom = overrides.get(category)
    if isinstance(custom, list) and custom and all(isinstance(x, str) for x in custom):
        return custom
    return DEFAULT_POOLS.get(category) or DEFAULT_POOLS["working"]


def pick_index(pool_size: int, last: int | None, rng: random.Random | None = None) -> int:
    """Random index that never repeats the previous one (when the pool allows it)."""
    rng = rng or random
    if pool_size <= 1:
        return 0
    choices = [i for i in range(pool_size) if i != last]
    return rng.choice(choices)


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render(template: str, variables: dict[str, str]) -> str:
    try:
        text = template.format_map(_SafeDict(variables))
    except (ValueError, IndexError):
        text = template  # malformed user template - show it as is
    return truncate(text, MAX_LEN)


def choose(
    category: str,
    variables: dict[str, str],
    last_idx: dict[str, int],
    cfg: dict[str, Any] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Pick a non-repeating template for `category`, update `last_idx` in place, render it."""
    pool = pool_for(category, cfg)
    idx = pick_index(len(pool), last_idx.get(category), rng)
    last_idx[category] = idx
    return render(pool[idx], variables)
