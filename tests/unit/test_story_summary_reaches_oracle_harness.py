"""The dispatcher passes the story's summary to the local oracle harness.

``OllamaDriver.dispatch`` turns a ``story_summary`` kwarg into the
``LOCAL_AGENT_STORY_SUMMARY`` env var, but only in oracle mode (when the story
carries ``acceptance`` fixtures); ``_dispatch_story_impl`` supplies the kwarg
only for local-family backends with acceptance paths.
"""
import json
import subprocess

import pytest

from app import backend
from app.backend_types import AgentHandle
from pipeline import execution
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt

_SUMMARY = "Abort the grid when its pins cannot resolve"
_ACCEPTANCE = ["tests/acceptance_x.py"]


# ---------- OllamaDriver.dispatch -> spawned env ----------
def _spawned_env(monkeypatch, tmp_path, **kwargs):
    captured = {}

    def _fake_spawn(*args, **kw):
        captured["env"] = kw["env"]
        return AgentHandle(pid=4242)

    monkeypatch.setattr(execution, "spawn_harness", _fake_spawn)
    backend.OllamaDriver().dispatch(
        "fix the bug", system="be careful", model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False, **kwargs,
    )
    return captured["env"]


def test_oracle_dispatch_spawns_harness_with_story_summary(monkeypatch, tmp_path):
    env = _spawned_env(
        monkeypatch, tmp_path, acceptance=_ACCEPTANCE, story_summary=_SUMMARY,
    )

    assert env["LOCAL_AGENT_STORY_SUMMARY"] == _SUMMARY


def test_non_oracle_dispatch_does_not_set_story_summary(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_AGENT_STORY_SUMMARY", raising=False)

    env = _spawned_env(monkeypatch, tmp_path, story_summary=_SUMMARY)

    assert "LOCAL_AGENT_STORY_SUMMARY" not in env


def test_oracle_dispatch_without_story_summary_sets_empty_value(monkeypatch, tmp_path):
    env = _spawned_env(monkeypatch, tmp_path, acceptance=_ACCEPTANCE)

    assert env["LOCAL_AGENT_STORY_SUMMARY"] == ""


# ---------- real dispatch path -> kwargs handed to the backend ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_resumed_worktree(tmp_path, worktree_root):
    """Bare origin + clone + a real worktree, so dispatch takes the RESUMED
    path (no venv provisioning, oracle validation or test-author phase)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)
    _run(["git", "worktree", "add", "-b", "agent/s1", str(worktree_root / "S1"), "HEAD"], repo)
    return repo


class _RecordingBackend:
    def __init__(self, sink):
        self._sink = sink

    def dispatch(self, **kwargs):
        self._sink.append(dict(kwargs))
        return AgentHandle(pid=4242)


def _dispatch_recording_kwargs(
    monkeypatch, tmp_path, plan_dir, worktree_root, backend_name, with_acceptance,
):
    repo = _repo_with_resumed_worktree(tmp_path, worktree_root)
    story = {
        "summary": _SUMMARY, "agent_instructions": "Build it.",
        "status": "interrupted", "dependencies": [], "backend": backend_name,
    }
    if with_acceptance:
        story["acceptance"] = [{"path": _ACCEPTANCE[0], "source": "def test_x():\n    assert False\n"}]
    manifest = {"repo_root": str(repo), "epics": {}, "stories": {"S1": story}}
    (plan_dir / "ss.manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    captured = []
    monkeypatch.setattr(
        backend, "get_backend", lambda role=None, *, name=None: _RecordingBackend(captured),
    )

    result = p.dispatch_story("ss", "S1")

    assert result.get("ok") is True, f"dispatch did not reach the launch: {result}"
    assert captured, "the backend was never invoked"
    return captured[0]


def test_local_dispatch_with_acceptance_hands_story_summary_to_backend(
    monkeypatch, tmp_path, plan_dir, worktree_root, agents_dir,
):
    kwargs = _dispatch_recording_kwargs(
        monkeypatch, tmp_path, plan_dir, worktree_root, "local", with_acceptance=True,
    )

    assert kwargs["story_summary"] == _SUMMARY
    assert kwargs["acceptance"] == _ACCEPTANCE


def test_local_dispatch_without_acceptance_gets_no_story_summary(
    monkeypatch, tmp_path, plan_dir, worktree_root, agents_dir,
):
    kwargs = _dispatch_recording_kwargs(
        monkeypatch, tmp_path, plan_dir, worktree_root, "local", with_acceptance=False,
    )

    assert "story_summary" not in kwargs


def test_claude_dispatch_gets_no_story_summary_kwarg(
    monkeypatch, tmp_path, plan_dir, worktree_root, agents_dir,
):
    kwargs = _dispatch_recording_kwargs(
        monkeypatch, tmp_path, plan_dir, worktree_root, "claude", with_acceptance=True,
    )

    assert "story_summary" not in kwargs
