from pathlib import Path

import pytest

from claude_discord_rpc import config, hook, paths, presence

CFG = config.load(Path("does-not-exist.toml"))


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSIONS_DIR", tmp_path)
    repo = tmp_path / "proj"
    repo.mkdir()

    def send(event, now, **extra):
        payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event, **extra}
        return hook.update_session(payload, CFG, now=now)

    return send


def state_line(session, now):
    return presence.compose([session], CFG, now=now)[0]["state"]


def test_foreground_subagents(run):
    run("UserPromptSubmit", 10)
    s = run("PreToolUse", 11, tool_name="Agent", tool_input={"prompt": "p"})
    assert s["category"] == "subagents"
    assert state_line(s, 11).endswith(" subagents")  # none started yet

    s = run("SubagentStart", 12, agent_id="a1", agent_type="Explore")
    assert state_line(s, 12).endswith("the Explore agent")

    # the subagent's own tool calls don't take over the main thread's state
    s = run("PreToolUse", 13, agent_id="a1", agent_type="Explore", tool_name="Read", tool_input={"file_path": "x.py"})
    assert s["category"] == "subagents" and s["updated_at"] == 13

    s = run("SubagentStart", 14, agent_id="a2", agent_type="Plan")
    assert state_line(s, 14).endswith("2 subagents")

    run("SubagentStop", 15, agent_id="a1", agent_type="Explore")
    s = run("SubagentStop", 16, agent_id="a2", agent_type="Plan")
    assert s["agents"] == {}


def test_background_agents_survive_stop(run):
    run("PreToolUse", 10, tool_name="Agent", tool_input={"run_in_background": True})
    run("SubagentStart", 11, agent_id="bg", agent_type="general-purpose")
    run("SubagentStart", 12, agent_id="fg", agent_type="Explore")  # its SubagentStop got lost
    s = run("Stop", 13, background_tasks=[{"id": "bg", "type": "local_agent", "status": "running"},
                                          {"id": "sh", "type": "local_bash", "status": "running"}])
    assert list(s["agents"]) == ["bg"]
    assert s["category"] == "idle"
    assert state_line(s, 13).endswith(" · 1 subagent")


def test_missed_subagent_start_and_stale_agents(run):
    s = run("PreToolUse", 10, agent_id="x", tool_name="Bash", tool_input={"command": "ls"})
    assert "x" in s["agents"]  # hooks installed mid-run: no SubagentStart seen
    later = 10 + presence.AGENT_STALE_SECONDS + 1
    assert presence.active_agents(s, later) == []
    s = run("UserPromptSubmit", later)
    assert s["agents"] == {}  # pruned on the next write


def sess(sid, updated, category, **extra):
    return {"session_id": sid, "updated_at": updated, "category": category, "state_text": category, **extra}


def test_pick_prefers_working_then_waiting_then_idle():
    working, waiting, idle = sess("w", 1, "editing"), sess("q", 2, "approval"), sess("i", 3, "idle")
    assert presence.pick_session([working, waiting, idle], now=10) is working
    assert presence.pick_session([waiting, idle], now=10) is waiting
    with_agents = sess("a", 0, "idle", agents={"bg": {"updated_at": 9}})
    assert presence.pick_session([idle, with_agents], now=10) is with_agents


def test_pick_is_sticky_within_a_tier():
    a, b = sess("a", 1, "editing"), sess("b", 5, "running")
    assert presence.pick_session([a, b], now=10) is b
    assert presence.pick_session([a, b], previous_id="a", now=10) is a
    # the shown session went idle: switch to the busy one
    assert presence.pick_session([sess("a", 6, "idle"), b], previous_id="a", now=10) is b


def test_cli_correction_replaces_template():
    s = sess("a", 1, "subagents", state_template="Running {agents}")
    activity, shown = presence.compose([s], CFG, {"a": {"status": "idle"}}, snapshot_at=100, now=100)
    assert shown == "a"
    assert "agent" not in activity["state"]  # idle phrase, not the stale subagents template
