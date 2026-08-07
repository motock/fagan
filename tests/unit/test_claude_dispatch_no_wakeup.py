"""Regression test for a live failure mode observed 2026-08-07: a Claude-
backend dispatch (ClaudeCliDriver.dispatch, headless `claude -p ... --output-
format stream-json`) has no external harness that ever revisits a scheduled
wakeup - it is a single one-shot subprocess. An agent that calls ScheduleWakeup
and defers to "wait for the background task to notify me" ends its turn
(stop_reason: end_turn); the subprocess exits, the background task is
orphaned/killed, and the notification that was supposed to resume the agent
never arrives - so no commit ever lands. This happened identically across 4
rework redispatches on story 4bfcc3b4 (plan rebrief-evidence-error-key-fix)
before being diagnosed and fixed here: block ScheduleWakeup outright via
--disallowedTools, and tell the agent explicitly why in its prompt.
"""
from app import backend as b
from pipeline import persona


class _FakePopenResult:
    def __init__(self, pid):
        self.pid = pid


def test_dispatch_disallows_schedule_wakeup(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: captured.update(cmd=cmd) or _FakePopenResult(1),
    )

    b.ClaudeCliDriver().dispatch(
        "implement the story", system=None, model="sonnet",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    cmd = captured["cmd"]
    assert "--disallowedTools" in cmd
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "ScheduleWakeup" in disallowed


def test_dispatch_prompt_warns_against_backgrounding_and_wakeups(tmp_path):
    story = {"summary": "s", "agent_instructions": "do the thing", "persona": None}
    spec = persona._build_dispatch_command(story, "story-key")
    prompt = spec["prompt"]
    assert "ScheduleWakeup" in prompt
    assert "synchronously" in prompt.lower()
