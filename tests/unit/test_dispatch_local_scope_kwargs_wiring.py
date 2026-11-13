"""Integration: a scoped local-family dispatch carries test_scope_spec.

Fixtures are copied from tests/unit/test_dispatch_test_scope_wiring.py
(repo convention: no shared conftest). The agent backend is stubbed to
capture the kwargs dispatch_story hands it; the dispatch path runs for real.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import backend
from pipeline import dispatch as dispatch_module
from pipeline import event_wiring
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._line_limit_policy import assert_within_line_limit


class _CiForgeStub:
    def has_ci_config(self, repo_root):
        return True


@pytest.fixture(autouse=True)
def _scoped_survives_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]))
    monkeypatch.setattr(
        "pipeline.test_scope_dispatch.get_forge", lambda root: _CiForgeStub()
    )


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
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


class _FakeHandle:
    pid = 4242


class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class _CaptureBackend:
    def __init__(self, sink):
        self._sink = sink

    def dispatch(self, **kwargs):
        self._sink.append(kwargs)
        return _FakeHandle()


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path, branch="main"):
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], tmp_path)
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", branch, str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    (repo / "pyproject.toml").write_text("[project]\nname = \"scope-fixture\"\nversion = \"0\"\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", branch], repo)
    return repo, branch


@pytest.fixture
def bus(monkeypatch):
    fake = _FakeBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: fake)
    return fake


@pytest.fixture
def captured(monkeypatch):
    """Record the kwargs every local-family agent launch receives."""
    calls = {"ollama": [], "claude": []}
    real_get_backend = backend.get_backend

    def _get_backend(role=None, *, name=None):
        if name in calls:
            return _CaptureBackend(calls[name])
        return real_get_backend(role, name=name)

    monkeypatch.setattr(backend, "get_backend", _get_backend)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    return calls


def _dispatch(plan_dir, repo, branch, monkeypatch, story, plan_extra=None, plan_name="scope"):
    manifest = {
        "repo_root": str(repo),
        "epics": {},
        "stories": {"S1": {
            "summary": "Do thing", "agent_instructions": "Build it.",
            "status": "todo", "dependencies": [],
            **story,
        }},
    }
    manifest.update(plan_extra or {})
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    return p.dispatch_story(plan_name, "S1")


ACCEPTANCE = [{"path": "tests/unit/test_acc_scope.py", "source": "def test_x():\n    assert False\n"}]
FAILED_LINE = "FAILED tests/unit/test_x.py::test_y - assert False"


def test_full_mode_dispatch_passes_no_test_scope_spec(
    plan_dir, worktree_root, agents_dir, captured, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch,
              {"backend": "ollama", "acceptance": ACCEPTANCE})
    kwargs = captured["ollama"][-1]
    assert "test_scope_spec" not in kwargs
    assert set(kwargs) == {
        "prompt", "system", "model", "allowed_tools", "cwd", "log_path", "append",
        "acceptance", "story_summary",
    }


def test_scoped_dispatch_sends_acceptance_paths_in_test_scope_spec(
    plan_dir, worktree_root, agents_dir, captured, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch,
              {"backend": "ollama", "acceptance": ACCEPTANCE},
              plan_extra={"test_scope": "scoped"})
    spec = captured["ollama"][-1]["test_scope_spec"]
    assert spec["acceptance"] == ["tests/unit/test_acc_scope.py"]


def test_scoped_ci_rework_sends_failing_node_id_and_keeps_full_suite_gate(
    plan_dir, worktree_root, agents_dir, captured, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch,
              {"backend": "ollama", "acceptance": ACCEPTANCE,
               "ci_rework": True, "review_feedback": FAILED_LINE},
              plan_extra={"test_scope": "scoped"})
    kwargs = captured["ollama"][-1]
    assert kwargs["test_scope_spec"]["ci_failures"] == ["tests/unit/test_x.py::test_y"]
    assert kwargs["rework_full_suite"] is True


def test_scoped_ci_rework_without_failed_line_sends_no_ci_failures(
    plan_dir, worktree_root, agents_dir, captured, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch,
              {"backend": "ollama", "acceptance": ACCEPTANCE,
               "ci_rework": True, "review_feedback": "build log with no test summary"},
              plan_extra={"test_scope": "scoped"})
    assert captured["ollama"][-1]["test_scope_spec"]["ci_failures"] == []


def test_claude_scoped_dispatch_passes_no_test_scope_spec(
    plan_dir, worktree_root, agents_dir, captured, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch,
              {"backend": "claude", "acceptance": ACCEPTANCE},
              plan_extra={"test_scope": "scoped"})
    assert "test_scope_spec" not in captured["claude"][-1]


def test_dispatch_module_stays_within_line_limit():
    lines = Path(dispatch_module.__file__).read_text().splitlines()
    assert_within_line_limit(len(lines), 999, "pipeline/dispatch.py")


REFERENCE_DOC = Path(__file__).resolve().parents[2] / "REFERENCE.md"


@pytest.mark.parametrize("phrase", [
    "`LOCAL_AGENT_TEST_SCOPE`",
    "a reviewer self-fix is verified on the same scope",
])
def test_reference_documents_local_scope_wiring(phrase):
    assert phrase in REFERENCE_DOC.read_text()
