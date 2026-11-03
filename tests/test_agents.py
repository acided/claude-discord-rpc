import json
import subprocess

import pytest

from claude_discord_rpc import agents, state

ENTRY = {"pid": 1, "sessionId": "a", "kind": "interactive", "status": "busy", "cwd": "C:\\x"}


def listing(*ids):
    return json.dumps([{**ENTRY, "sessionId": sid} for sid in ids])


class FakeCli:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(out, Exception):
            raise out
        return out


def test_parse_skips_junk():
    out = json.dumps([ENTRY, {"pid": 2}, "x", {**ENTRY, "sessionId": "b"}])
    assert set(agents.parse(out)) == {"a", "b"}
    with pytest.raises(ValueError):
        agents.parse('{"not": "a list"}')


def test_poll_is_throttled():
    cli = FakeCli(listing("a"))
    poller = agents.AgentsPoller(cli, interval=10)
    poller.poll(100)
    poller.poll(105)
    assert cli.calls == 1
    poller.poll(110)
    assert cli.calls == 2


def test_is_alive_rules():
    poller = agents.AgentsPoller(FakeCli(listing("a", "b"), listing("a")), interval=10)
    assert poller.is_alive({"session_id": "a"}) is None  # nothing polled yet

    poller.poll(100)
    assert poller.is_alive({"session_id": "a", "updated_at": 1}) is True
    assert poller.is_alive({"session_id": "never-listed", "updated_at": 1}) is None  # e.g. claude -p

    poller.poll(110)  # "b" has exited
    assert poller.is_alive({"session_id": "b", "updated_at": 50}) is False
    # a hook event newer than the snapshot: can't tell yet
    assert poller.is_alive({"session_id": "b", "updated_at": 111}) is None


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("no claude"), subprocess.TimeoutExpired("claude", 15), ValueError("bad json")],
)
def test_cli_failure_means_unknown(error):
    poller = agents.AgentsPoller(FakeCli(listing("a"), error), interval=10)
    poller.poll(100)
    poller.poll(110)
    assert poller.live is None
    assert poller.is_alive({"session_id": "a", "updated_at": 1}) is None


def test_load_sessions_with_liveness(tmp_path):
    for sid in ("running", "gone", "unknown", "unknown-stale"):
        state.write_session({"session_id": sid, "updated_at": 0}, tmp_path)
    state.write_session({"session_id": "unknown", "updated_at": 990}, tmp_path)
    verdicts = {"running": True, "gone": False}

    shown = state.load_sessions(60, tmp_path, now=1000, is_alive=lambda s: verdicts.get(s["session_id"]))

    # running: shown despite 1000 s without events; unknown: timeout rules apply
    assert sorted(s["session_id"] for s in shown) == ["running", "unknown"]
    assert not (tmp_path / "gone.json").exists()
    assert (tmp_path / "unknown-stale.json").exists()  # hidden, not deleted
