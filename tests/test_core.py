import random
import tomllib
from pathlib import Path

import pytest

from claude_discord_rpc import config, phrases, presence, state
from claude_discord_rpc.events import (
    HIDDEN_CMD,
    HIDDEN_FILE,
    classify,
    command_name,
    pretty_model,
    resolve_project,
)

REPO = Path(__file__).resolve().parent.parent


def tool(name, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input}


# events -------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload, category",
    [
        ({"hook_event_name": "SessionStart"}, "starting"),
        ({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}, "thinking"),
        ({"hook_event_name": "Stop"}, "idle"),
        ({"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash"}, "approval"),
        ({"hook_event_name": "Notification", "message": "Claude is waiting for your input"}, "idle"),
        (tool("Edit", file_path="C:/x/main.py"), "editing"),
        (tool("Read", file_path="/x/a.md"), "reading"),
        (tool("Grep", pattern="foo"), "searching"),
        (tool("Bash", command="git status"), "running"),
        (tool("WebSearch", query="q"), "web"),
        (tool("Agent", prompt="p"), "subagents"),
        (tool("TodoWrite"), "planning"),
        (tool("AskUserQuestion"), "question"),
        (tool("mcp__claude_ai_Gmail__search"), "mcp"),
        (tool("SomethingNew"), "working"),
    ],
)
def test_classify_categories(payload, category):
    assert classify(payload).category == category


def test_classify_file_and_privacy():
    payload = tool("Edit", file_path=r"C:\proj\src\main.py")
    assert classify(payload).variables == {"file": "main.py"}
    assert classify(payload, show_files=False).variables == {"file": HIDDEN_FILE}
    assert classify(tool("Bash", command="npm test"), show_commands=False).variables == {"cmd": HIDDEN_CMD}


def test_long_file_name_keeps_extension():
    name = classify(tool("Edit", file_path="/x/" + "a" * 100 + ".py")).variables["file"]
    assert len(name) <= 48 and name.endswith(".py")


@pytest.mark.parametrize(
    "command, expected",
    [
        ("git commit -m 'secret token abc'", "git"),
        ("cd /repo && npm run build", "npm"),
        ("Set-Location C:\\x; uv run pytest", "uv"),
        ("FOO=1 BAR=2 python script.py", "python"),
        ('"C:/Program Files/nodejs/node.exe" app.js', "node"),
        ("& 'C:\\Program Files\\Git\\bin\\git.exe' log", "git"),
        ("FOO=1", HIDDEN_CMD),
        ('$d="$env:LOCALAPPDATA\\claude-discord-rpc"; Get-ChildItem $d', "Get-ChildItem"),
        ("$env:FOO = 'x'; npm test", "npm"),
        ("if (Test-Path x) { rm x }", HIDDEN_CMD),
        ("echo \"secret\" > /tmp/x", "echo"),
        ("C:\\tools\\rg.exe foo", "rg"),
        ("", HIDDEN_CMD),
    ],
)
def test_command_name(command, expected):
    assert command_name(command) == expected


def test_resolve_project(tmp_path):
    repo = tmp_path / "fpsm"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    assert resolve_project(str(repo / "src"), [], home=tmp_path).name == "fpsm"
    assert resolve_project(str(tmp_path), [], home=tmp_path).kind == "home"
    assert resolve_project(str(repo), ["FP*"], home=tmp_path).kind == "secret"
    assert resolve_project(str(repo), ["*/fpsm"], home=tmp_path).kind == "secret"


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
        ("claude-opus-4-1[1m]", "Opus 4.1"),
        ({"id": "x", "display_name": "Opus 5.5"}, "Opus 5.5"),
        ("<synthetic>", None),
        (None, None),
    ],
)
def test_pretty_model(model, expected):
    assert pretty_model(model) == expected


# phrases ------------------------------------------------------------------

def test_every_default_template_renders():
    variables = {"project": "p", "file": "f.py", "cmd": "git", "server": "s", "tool": "t"}
    for category, pool in phrases.DEFAULT_POOLS.items():
        assert pool, category
        for template in pool:
            text = phrases.render(template, variables)
            assert 2 <= len(text) <= phrases.MAX_LEN
            assert "{" not in text


def test_pick_never_repeats():
    rng = random.Random(0)
    last = {}
    previous = None
    for _ in range(200):
        phrases.choose("thinking", {}, last, rng=rng)
        assert last["thinking"] != previous
        previous = last["thinking"]


def test_config_overrides_pool_and_survives_bad_template():
    cfg = {"phrases": {"thinking": ["Only {nope} {"]}}
    assert phrases.choose("thinking", {}, {}, cfg) == "Only {nope} {"
    cfg = {"phrases": {"thinking": ["Hmm {file}"]}}
    assert phrases.choose("thinking", {}, {}, cfg) == "Hmm something"


def test_render_truncates():
    assert len(phrases.render("x" * 500, {})) == phrases.MAX_LEN


# presence -----------------------------------------------------------------

def session(sid, updated, **extra):
    return {"session_id": sid, "started_at": 100, "updated_at": updated, "details_text": "Working on a",
            "state_text": "Thinking…", "category": "thinking", **extra}


def test_build_defaults_and_session_count():
    cfg = config.load(Path("does-not-exist.toml"))
    act = presence.build([session("a", 1), session("b", 5, details_text="Deep in b", model="Opus 5.5")], cfg)
    assert act["details"] == "Deep in b"
    assert act["timestamps"] == {"start": 100}
    assert act["assets"] == {
        "large_image": "logo",
        "large_text": "claude.ai",
        "small_image": "claude-code",
        "small_text": "Clawd · Opus 5.5 · 2 sessions",
    }
    # unknown model falls back to "Claude"
    assert presence.build([session("a", 1)], cfg)["assets"]["small_text"] == "Clawd · Claude"
    assert presence.build([], cfg) is None


def test_build_templates_state_icons_and_buttons():
    cfg = config.load(Path("does-not-exist.toml"))
    cfg["images"].update(large_text="{model}", small_text="{state}", small_states={"thinking": "brain"})
    cfg["buttons"] = [{"label": "GitHub", "url": "https://github.com/x"}, {"label": "bad", "url": "ftp://x"}]
    act = presence.build([session("a", 1, model="Opus 5.5")], cfg)
    assert act["assets"]["small_image"] == "brain"
    assert act["assets"]["small_text"] == "Thinking"
    assert act["assets"]["large_text"] == "Opus 5.5"
    assert act["buttons"] == [{"label": "GitHub", "url": "https://github.com/x"}]


def test_build_without_small_icon_counts_on_large():
    cfg = config.load(Path("does-not-exist.toml"))
    cfg["images"]["small"] = ""
    act = presence.build([session("a", 1), session("b", 2)], cfg)
    assert "small_image" not in act["assets"]
    assert act["assets"]["large_text"] == "claude.ai · 2 sessions"


# state / hook -------------------------------------------------------------

def test_stale_sessions_hidden_then_purged(tmp_path):
    state.write_session({"session_id": "ancient", "updated_at": 0}, tmp_path)
    state.write_session({"session_id": "idle", "updated_at": 900}, tmp_path)
    state.write_session({"session_id": "new", "updated_at": 1000}, tmp_path)
    live = state.load_sessions(60, tmp_path, now=1010, purge_seconds=500)
    assert [s["session_id"] for s in live] == ["new"]
    assert (tmp_path / "idle.json").exists()  # hidden, kept for when the user returns
    assert not (tmp_path / "ancient.json").exists()


def test_hook_flow(tmp_path, monkeypatch):
    from claude_discord_rpc import hook, paths

    monkeypatch.setattr(paths, "SESSIONS_DIR", tmp_path)
    repo = tmp_path / "proj"
    repo.mkdir()
    cfg = config.load(Path("does-not-exist.toml"))
    base = {"session_id": "s1", "cwd": str(repo)}

    s = hook.update_session({**base, "hook_event_name": "SessionStart"}, cfg, now=10)
    details = s["details_text"]
    assert "proj" in details and s["category"] == "starting"

    s = hook.update_session({**base, **tool("Edit", file_path="a.py")}, cfg, now=11)
    first = s["state_text"]
    assert "a.py" in first and s["details_text"] == details  # details stable within a session

    s = hook.update_session({**base, **tool("Edit", file_path="a.py")}, cfg, now=12)
    assert s["state_text"] == first  # same action -> same phrase, no flicker
    assert s["started_at"] == 10 and s["updated_at"] == 12

    assert hook.update_session({**base, "hook_event_name": "SessionEnd"}, cfg) is None
    assert state.read_session("s1") is None


def test_example_config_parses():
    with open(REPO / "config.example.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert "privacy" in data and "phrases" in data


def test_empty_client_id_falls_back_to_default(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('client_id = ""\n', encoding="utf-8")
    assert config.load(cfg_file)["client_id"] == config.DEFAULT_CLIENT_ID
