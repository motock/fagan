"""Checkpoint file mode for dispatched agents.

When PIPELINE_CHECKPOINT_MODE=file the agent is told to drop a checkpoint
file (.agent_checkpoint) instead of calling the checkpoint tool, and the
local agent's checkpoint tool commits its work and writes that file. With the
mode off, both keep today's behaviour.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from pipeline import checkpoint_drop
from pipeline.persona import _build_dispatch_command

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)

STORY = {"summary": "s", "agent_instructions": "BRIEF-TEXT", "rework_attempts": 0}
TOOL_SENTENCE = "call the checkpoint tool"
DROP_SENTENCE = ".agent_checkpoint"


@pytest.fixture
def file_mode(monkeypatch):
    monkeypatch.setenv(checkpoint_drop.MODE_ENV, "file")


@pytest.fixture
def tool_mode(monkeypatch):
    monkeypatch.delenv(checkpoint_drop.MODE_ENV, raising=False)


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "seed.txt").write_text("seed\n")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(la, "CWD", tmp_path)
    return tmp_path


# ---------- persona prompt ----------

def test_persona_prompt_uses_checkpoint_tool_when_mode_off(tool_mode):
    prompt = _build_dispatch_command(STORY, "K-1", plan_name="P")["prompt"]

    assert TOOL_SENTENCE in prompt
    assert DROP_SENTENCE not in prompt


def test_persona_prompt_asks_for_drop_file_when_mode_on(file_mode):
    prompt = _build_dispatch_command(STORY, "K-1", plan_name="P")["prompt"]

    assert DROP_SENTENCE in prompt
    assert TOOL_SENTENCE not in prompt


@pytest.mark.parametrize("mode_fixture", ["tool_mode", "file_mode"])
def test_persona_prompt_has_no_checkpoint_text_without_plan_name(mode_fixture, request):
    request.getfixturevalue(mode_fixture)
    prompt = _build_dispatch_command(STORY, "K-1")["prompt"]

    assert TOOL_SENTENCE not in prompt
    assert DROP_SENTENCE not in prompt


# ---------- local agent checkpoint tool ----------

def test_checkpoint_tool_commits_and_writes_drop_file_when_mode_on(
    repo, file_mode, monkeypatch,
):
    journal_calls = []
    monkeypatch.setattr(la.p, "_checkpoint_impl", lambda *args: journal_calls.append(args))
    (repo / "work.txt").write_text("done\n")

    res = la.run_tool("checkpoint", {
        "plan_name": "P", "story_key": "K-1", "step": "s1", "summary": "did it",
        "next_hint": "next",
    })

    sha = _git(repo, "rev-parse", "HEAD")
    assert journal_calls == []
    assert res.startswith("checkpoint recorded")
    assert "work.txt" in _git(repo, "show", "--name-only", "--format=", "HEAD")
    assert _git(repo, "status", "--porcelain", "--", "work.txt") == ""
    drop = json.loads((repo / checkpoint_drop.DROP_NAME).read_text())
    assert drop["step"] == "s1"
    assert drop["commit"] == sha


def test_checkpoint_tool_calls_journal_with_five_arguments_when_mode_off(
    tool_mode, monkeypatch,
):
    calls = []

    def _fake_impl(*args):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(la.p, "_checkpoint_impl", _fake_impl)

    res = la.run_tool("checkpoint", {
        "plan_name": "P", "story_key": "K-1", "step": "s1", "summary": "did it",
        "next_hint": "next",
    })

    assert calls == [("P", "K-1", "s1", "did it", "next")]
    assert res.startswith("checkpoint recorded")


def test_checkpoint_tool_reports_error_without_raising_when_drop_write_fails(
    repo, file_mode, monkeypatch,
):
    def _fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(checkpoint_drop, "write", _fail_write)

    res = la.run_tool("checkpoint", {
        "plan_name": "P", "story_key": "K-1", "step": "s1", "summary": "did it",
        "next_hint": "next",
    })

    assert res.startswith("ERROR checkpointing")
