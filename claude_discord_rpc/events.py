"""Map raw Claude Code hook payloads to presence categories and template variables."""

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SEARCH_TOOLS = {"Grep", "Glob", "LS", "ToolSearch"}
SHELL_TOOLS = {"Bash", "PowerShell", "BashOutput"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
AGENT_TOOLS = {"Agent", "Task", "Workflow", "SendMessage"}
PLAN_TOOLS = {"TodoWrite", "EnterPlanMode", "ExitPlanMode", "TaskCreate", "TaskUpdate"}
QUESTION_TOOLS = {"AskUserQuestion"}

HIDDEN_FILE = "a file"
HIDDEN_CMD = "a command"
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_QUOTED = re.compile(r"""^(["'])(.+?)\1""")
_PS_ASSIGN = re.compile(r"^\$[\w:]+(\.\w+)*\s*[-+*/]?=")  # $x = ..., $env:FOO = ...
_PROGRAM = re.compile(r"^[\w.+-]+$")
_SKIP_WORDS = {
    "cd", "set-location", "pushd", "popd", "sl",
    "if", "elif", "else", "then", "fi", "for", "foreach", "while", "do", "done", "try", "catch", "function",
}


@dataclass(frozen=True)
class Event:
    category: str
    variables: dict[str, str]

    @property
    def key(self) -> str:
        """Identity of a state: same key -> keep the already picked phrase."""
        return self.category + "|" + json.dumps(self.variables, sort_keys=True)


@dataclass(frozen=True)
class Project:
    kind: str  # "project" | "home" | "secret"
    name: str

    @property
    def pool(self) -> str:
        return {"project": "details", "home": "home", "secret": "secret"}[self.kind]


def _basename(path: str, limit: int = 48) -> str:
    name = re.split(r"[\\/]", path.rstrip("\\/"))[-1] if path else ""
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and len(ext) <= 8:
            name = stem[: limit - len(ext) - 2] + "…." + ext  # keep the extension visible
        else:
            name = name[: limit - 1] + "…"
    return name or HIDDEN_FILE


def command_name(command: str) -> str:
    """First meaningful program name of a shell command, without arguments."""
    for segment in _SEGMENT_SPLIT.split(command or ""):
        rest = segment.strip().lstrip("&( ").strip()
        while _ENV_ASSIGN.match(rest):  # skip leading VAR=value assignments
            parts = rest.split(None, 1)
            rest = parts[1] if len(parts) > 1 else ""
        if not rest or rest.startswith("#") or _PS_ASSIGN.match(rest):
            continue
        quoted = _QUOTED.match(rest)
        prog = quoted.group(2) if quoted else rest.split()[0]
        prog = prog.strip("\"'()")
        prog = re.split(r"[\\/]", prog)[-1]
        prog = re.sub(r"\.(exe|cmd|bat|ps1)$", "", prog, flags=re.IGNORECASE)
        # Only plain program names: never leak paths, strings or expressions.
        if not _PROGRAM.match(prog) or prog.lower() in _SKIP_WORDS:
            continue
        return prog[:32]
    return HIDDEN_CMD


def _mcp_server(tool_name: str) -> str:
    parts = tool_name.split("__")
    server = parts[1] if len(parts) > 1 else tool_name
    server = re.sub(r"^claude_ai_", "", server)
    return server.replace("_", " ").strip() or "MCP"


def classify(payload: dict[str, Any], show_files: bool = True, show_commands: bool = True) -> Event:
    event = payload.get("hook_event_name", "")
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    def file_var() -> dict[str, str]:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return {"file": _basename(str(path)) if show_files and path else HIDDEN_FILE}

    if event == "SessionStart":
        return Event("starting", {})
    if event == "UserPromptSubmit":
        return Event("thinking", {})
    if event in ("Stop", "SubagentStop"):
        return Event("idle", {})
    if event == "Notification":
        text = f"{payload.get('notification_type', '')} {payload.get('message', '')}".lower()
        return Event("approval" if "permission" in text or "approv" in text else "idle", {})

    # PreToolUse / PostToolUse
    if tool in EDIT_TOOLS:
        return Event("editing", file_var())
    if tool in READ_TOOLS:
        return Event("reading", file_var())
    if tool in SEARCH_TOOLS:
        return Event("searching", {})
    if tool in SHELL_TOOLS:
        cmd = command_name(str(tool_input.get("command", ""))) if show_commands else HIDDEN_CMD
        return Event("running", {"cmd": cmd})
    if tool in WEB_TOOLS:
        return Event("web", {})
    if tool in AGENT_TOOLS:
        return Event("subagents", {})
    if tool in PLAN_TOOLS:
        return Event("planning", {})
    if tool in QUESTION_TOOLS:
        return Event("question", {})
    if tool.startswith("mcp__"):
        return Event("mcp", {"server": _mcp_server(tool)})
    return Event("working", {"tool": tool or "tools"})


def _find_repo_root(cwd: Path) -> Path | None:
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def resolve_project(cwd: str, blacklist: list[str], home: Path | None = None) -> Project:
    home = (home or Path.home()).resolve()
    try:
        path = Path(cwd).resolve()
    except (OSError, ValueError):
        path = Path(cwd)
    root = _find_repo_root(path) or path
    name = root.name or str(root)

    candidates = [name.lower(), str(root).replace("\\", "/").lower()]
    for pattern in blacklist:
        pat = str(pattern).replace("\\", "/").lower()
        if any(fnmatch.fnmatch(c, pat) for c in candidates):
            return Project("secret", "")

    if os.path.normcase(str(root)) == os.path.normcase(str(home)):
        return Project("home", "")
    return Project("project", name)


def pretty_model(model: Any) -> str | None:
    """'claude-opus-5-5[1m]' -> 'Opus 5.5'; dicts with display_name are used as is."""
    if isinstance(model, dict):
        display = model.get("display_name")
        if display:
            return str(display)
        model = model.get("id")
    if not isinstance(model, str) or not model or model.startswith("<"):
        return None
    ident = re.sub(r"\[.*?\]$", "", model.strip())
    parts = [p for p in ident.split("-") if p and p != "claude" and not re.fullmatch(r"\d{8}", p)]
    if not parts:
        return None
    family = next((p for p in parts if not p.isdigit()), None)
    version = ".".join(p for p in parts if p.isdigit())
    if not family:
        return None
    return f"{family.capitalize()} {version}".strip()


def model_from_transcript(transcript_path: str, tail_bytes: int = 256 * 1024) -> str | None:
    """Last model id used in a session, read from the tail of the JSONL transcript."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read()
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if b'"model"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if isinstance(message, dict):
            pretty = pretty_model(message.get("model"))
            if pretty:
                return pretty
    return None
