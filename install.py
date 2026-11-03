"""Register claude-discord-rpc hooks in the global Claude Code settings.

    python install.py [--client-id ID]   install / update hooks, create config if missing
    python install.py --uninstall        remove hooks and stop the daemon
"""

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

from claude_discord_rpc import lock, paths

SETTINGS = Path.home() / ".claude" / "settings.json"
MARKER = "claude_discord_rpc"
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop", "SessionEnd"]
TOOL_EVENTS = {"PreToolUse"}


def hook_command() -> str:
    python = Path(sys.executable).as_posix()
    script = (paths.PACKAGE_DIR / "hook.py").as_posix()
    return f'"{python}" "{script}"'


def _strip_ours(hooks: dict) -> None:
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if MARKER not in str(h.get("command", ""))]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def update_settings(install: bool) -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        backup = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print(f"backup: {backup}")

    hooks = settings.setdefault("hooks", {})
    _strip_ours(hooks)
    if install:
        entry = {"type": "command", "command": hook_command(), "timeout": 5}
        for event in EVENTS:
            group = {"matcher": "*", "hooks": [entry]} if event in TOOL_EVENTS else {"hooks": [entry]}
            hooks.setdefault(event, []).append(group)
    if not hooks:
        settings.pop("hooks")

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{'installed' if install else 'removed'} hooks in {SETTINGS}")


def ensure_config(client_id: str | None) -> None:
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not paths.CONFIG_FILE.exists():
        shutil.copy2(paths.REPO_DIR / "config.example.toml", paths.CONFIG_FILE)
        print(f"created config: {paths.CONFIG_FILE}")
    if client_id:
        text = paths.CONFIG_FILE.read_text(encoding="utf-8")
        text, count = re.subn(r'(?m)^client_id\s*=.*$', f'client_id = "{client_id}"', text, count=1)
        if not count:
            text = f'client_id = "{client_id}"\n' + text
        paths.CONFIG_FILE.write_text(text, encoding="utf-8")
        print("client_id saved")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", help="Discord Application ID")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    if args.client_id and not args.client_id.isdigit():
        parser.error("--client-id must be the numeric Application ID")

    if args.uninstall:
        update_settings(install=False)
        if lock.is_held(paths.LOCK_FILE):
            paths.ensure_dirs()
            paths.STOP_FLAG.touch()
            print("daemon stop requested")
        return

    update_settings(install=True)
    ensure_config(args.client_id)
    print("done. Restart Claude Code sessions to pick up the hooks.")


if __name__ == "__main__":
    main()
