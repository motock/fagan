"""Shared fixtures/helpers for the always-on guided-decomposition planner test
suite, split across test_always_on_planner_*.py files (originally one
1,162-line test_always_on_planner.py) to keep each file under the project's
line-count target.
"""
import json

import pytest

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt


class _FakePlannerBackend:
    """Stand-in for whatever backend.get_backend(...) returns, capturing the
    exact kwargs _run_planner passed to complete()."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    from pipeline import persona as pper
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
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    pt._state_cache.clear()
    pt._label_cache.clear()
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


def _stub_dispatch_externals(monkeypatch):
    """Stub the external boundaries dispatch_story touches so it can run to
    completion without git/gh/Plane/subprocess."""
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakeProc(9500))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


__all__ = [
    "_FakePlannerBackend",
    "_FakeProc",
    "_clear_caches",
    "_plane_configured",
    "_stub_dispatch_externals",
    "_write_manifest",
    "agents_dir",
    "plan_dir",
    "worktree_root",
]
