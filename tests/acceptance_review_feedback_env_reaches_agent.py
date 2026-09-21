"""Acceptance: a reviewer-feedback rework round must reach the agent
subprocess with its OWN signal, distinct from the shared rework_full_suite one.

`dispatch.py` raises the agent's done-bar for ANY rework redispatch - a
CI-triggered one (`story["ci_rework"]`) or a reviewer REQUEST_CHANGES one
(`story["review_feedback"]`) - through the single `rework_full_suite` kwarg,
which `app/backend_ollama.py` translates to LOCAL_AGENT_REWORK_FULL_SUITE.
Those two rounds are not the same bar, and the oracle harness cannot tell them
apart: on a CI-fail rework the agent's own committed test is the defect (a
green suite settles it), but on a reviewer-feedback rework the acceptance
oracle is usually ALREADY green when the round starts - that first dispatch is
exactly what the reviewer read - so oracle-green is no evidence the findings
were addressed.

This fixture grades the DELIVERY half only: that the distinct signal reaches
the agent environment, that the shared signal is unchanged, and that a fresh
or CI-fail dispatch carries neither. What the harness does with it is the
dependent story's oracle.

The whole chain is driven through the real entrypoint - `dispatch_story` over
a manifest on disk, capturing the env the driver hands its subprocess - so a
kwarg that is declared but never wired is graded as the failure it is.
"""
import inspect
import json
import subprocess

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage

_PLAN = "rfwenv"
_ACCEPTANCE = [{"path": "tests/acceptance_fixture.py", "source": "def test_x(): pass"}]


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _dispatch(tmp_path, monkeypatch, story):
    """Dispatch one story through the real entrypoint; return the agent env."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(pcon, "PLAN_DIR", plans)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktrees)
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", tmp_path / "usage_state.json")

    # Hermetic seams: no live Ollama round-trip, no Plane, no real git.
    monkeypatch.setattr(backend.OllamaDriver, "complete", lambda self, prompt, **kw: "")
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: set())
    monkeypatch.setattr(backend, "_ollama_serving_parallelism", lambda: None)
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    # The story carries an acceptance block, so the pre-dispatch oracle gate
    # reads .returncode/.stdout - a no-op None would be misread as a broken
    # oracle rather than an ordinary not-yet-implemented failure.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    captured: dict = {}
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4321),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    (plans / f"{_PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}}, indent=2),
        encoding="utf-8",
    )
    result = p.dispatch_story(_PLAN, "S1")
    assert result["ok"] is True, result
    assert "env" in captured, "dispatch spawned no agent subprocess"
    return captured["env"]


def test_review_feedback_rework_carries_its_own_signal(tmp_path, monkeypatch):
    env = _dispatch(tmp_path, monkeypatch, {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "changes_requested", "dependencies": [],
        "review_feedback": "REQUEST_CHANGES: the helper is defined twice.",
        "acceptance": _ACCEPTANCE,
    })
    assert env["LOCAL_AGENT_REVIEW_FEEDBACK_REWORK"] == "1"
    # The shared signal must be unchanged: this round still requires a green
    # full suite before the agent may call done.
    assert env["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_ci_fail_rework_carries_only_the_shared_signal(tmp_path, monkeypatch):
    env = _dispatch(tmp_path, monkeypatch, {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "todo", "dependencies": [], "ci_rework": True,
        "acceptance": _ACCEPTANCE,
    })
    assert env["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"
    assert "LOCAL_AGENT_REVIEW_FEEDBACK_REWORK" not in env


def test_fresh_dispatch_carries_neither_signal(tmp_path, monkeypatch):
    env = _dispatch(tmp_path, monkeypatch, {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "todo", "dependencies": [],
        "acceptance": _ACCEPTANCE,
    })
    assert "LOCAL_AGENT_REWORK_FULL_SUITE" not in env
    assert "LOCAL_AGENT_REVIEW_FEEDBACK_REWORK" not in env


def test_backend_protocol_and_local_driver_declare_the_kwarg():
    """The kwarg is part of the backend dispatch contract, not just the ollama
    driver's local signature: a driver that does not declare it fails loudly
    on an unexpected keyword rather than silently dropping the signal."""
    for cls in (backend.Backend, backend.OllamaDriver):
        params = inspect.signature(cls.dispatch).parameters
        assert "review_feedback_rework" in params, f"{cls.__name__}.dispatch"
        assert params["review_feedback_rework"].default is False, f"{cls.__name__}"
