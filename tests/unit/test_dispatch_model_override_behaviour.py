"""Behavioural regression tests for REG-1: a registry-pinned
``roles.dispatch`` model must actually reach the driver.

Reviewer's blocking finding (this file exists to reproduce it)
-------------------------------------------------------------
``pipeline/dispatch.py`` gained an override meant to plumb the
registry-resolved dispatch model into the spec the driver is launched with::

    if not spec.get("model") and dispatch_model:
        spec["model"] = dispatch_model

That guard can never fire. ``_build_dispatch_command``
(``pipeline/persona.py``) always returns a truthy ``spec["model"]``::

    model = story.get("model") or persona_default or DEFAULT_MODEL

and ``DEFAULT_MODEL = os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")``
(``pipeline/config.py``) is never empty. So ``not spec.get("model")`` is
always ``False``, ``spec["model"]`` stays ``"sonnet"``, and the value handed
to the driver (``dispatch_kwargs["model"] = spec["model"]``) and recorded as
``story["dispatched_model"]`` is the driver's env default - never the
registry tag. The measured bug (every agent booting on
``PIPELINE_LOCAL_MODEL_DEFAULT``) therefore persists.

The pre-existing test ``test_resolved_model_is_plumbed_into_the_dispatch_spec``
is a source-level regex over ``dispatch.py``; it passes on the dead code. These
tests are behavioural instead: they drive the REAL ``_dispatch_story_impl``
(through ``pipeline.server.dispatch_story``) against a REAL git
origin/repo/worktree, with a synthetic registry installed via
``PIPELINE_MODEL_REGISTRY_PATH``, and assert on the model the driver is
actually launched with and on ``story["dispatched_model"]``.

Per ``.claude/rules/testing-config-gates.md`` every test stubs the registry
with a synthetic payload; nothing asserts against this machine's real
``model_registry``.

Expected state: RED until the guard is gated on the story's own pin
(``if not story.get("model") and dispatch_model:``) rather than on the
always-populated spec default.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from app import backend, role_registry
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt

# ---------------------------------------------------------------------------
# Synthetic registry (never this machine's real model_registry.json)
# ---------------------------------------------------------------------------
# The registry's roles.dispatch entry names a *friendly* model name that is
# resolved against providers.<provider>.models to a concrete tag. Both are
# "glm-5.3-flash:cloud" here so the resolved tag is unambiguous.
_REGISTRY_MODEL_NAME = "glm-5.3-flash:cloud"
_REGISTRY_MODEL_TAG = "glm-5.3-flash:cloud"
_REGISTRY_PROVIDERS = {
    "claude": {"models": {_REGISTRY_MODEL_NAME: {"tag": _REGISTRY_MODEL_TAG}}},
}
_REGISTRY_DISPATCH = {"provider": "claude", "model": _REGISTRY_MODEL_NAME}

# The driver's own env default - the model every agent actually booted on
# (the measured bug). Never a registry tag.
_DRIVER_ENV_DEFAULT = "qwen3-coder:30b"

# What _build_dispatch_command falls back to when the story pins no model and
# the persona declares none: DEFAULT_MODEL, which is never empty.
_SPEC_DEFAULT = "sonnet"


# ---------- Fixtures (copied from test_dispatch_staleness.py) ----------
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
    (d / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: sonnet\n---\n\nSecurity body.\n'
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
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


# ---------- Real-git harness (copied from test_dispatch_staleness.py) ----------
class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.args = []
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _write_manifest(plan_dir, plan_name, stories, repo_root=None):
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = str(repo_root)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True,
                          text=True)


def _make_origin_and_repo(tmp_path, branch="main"):
    """Bare `origin` + a real local clone `repo`, both on `branch`, one
    commit deep. Returns (origin, repo, branch)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], tmp_path)

    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", branch, str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", branch], repo)
    return origin, repo, branch


def _make_resumed_worktree(tmp_path, worktree_root, repo, branch, story_key="S1"):
    """Create a REAL git worktree at worktree_root/<story_key> on an
    `agent/<story_key>` branch off repo's current HEAD, so the dispatch is a
    RESUMED one (skips worktree creation / venv provisioning) while still
    running the real spec-building and launch path."""
    worktree_path = worktree_root / story_key
    agent_branch = f"agent/{story_key.lower()}"
    _run(
        ["git", "worktree", "add", "-b", agent_branch, str(worktree_path), "HEAD"],
        repo,
    )
    return worktree_path


# ---------- Registry + driver capture ----------
def _install_registry(monkeypatch, tmp_path, roles):
    """Point the registry at a synthetic payload and drop the memo."""
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps({"providers": _REGISTRY_PROVIDERS, "roles": roles}))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    role_registry.reset_registry_cache()
    return path


class _RecordingBackend:
    """Transparent proxy around the real driver that records the kwargs
    ``_dispatch_story_impl`` hands it - i.e. the exact ``spec`` it built."""

    def __init__(self, inner, sink):
        self._inner = inner
        self._sink = sink

    def dispatch(self, **kwargs):
        self._sink.append(dict(kwargs))
        return self._inner.dispatch(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _no_plane(*a, **k):
    raise RuntimeError("no plane")


def _arm_dispatch(
    monkeypatch, tmp_path, plan_dir, repo, branch, plan_name, story_key, story,
):
    """Install the synthetic registry + manifest and monkeypatch the infra
    seams, returning the list the driver's launch kwargs are recorded into.

    The manifest is written exactly ONCE, so a test may call
    ``p.dispatch_story`` repeatedly against the same story dict (Test C)."""
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("PIPELINE_AGENT_HARNESS", raising=False)
    # The driver's own env default - the value the bug makes every agent boot
    # on. Set explicitly so the assertion is meaningful on any host.
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _DRIVER_ENV_DEFAULT)
    # Pin the spec default deterministically, whatever this host's env says.
    monkeypatch.setattr(pper, "DEFAULT_MODEL", _SPEC_DEFAULT)

    _write_manifest(plan_dir, plan_name, {story_key: story}, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request", _no_plane)

    captured: list[dict] = []
    real_get_backend = backend.get_backend

    def _recording_get_backend(role=None, *, name=None):
        return _RecordingBackend(real_get_backend(role, name=name), captured)

    monkeypatch.setattr(backend, "get_backend", _recording_get_backend)

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    return captured


def _story(story_key="S1", **extra):
    story = {
        "summary": "Do thing",
        "agent_instructions": "Build it.",
        "status": "interrupted",
        "dependencies": [],
    }
    story.update(extra)
    return story


# ---------------------------------------------------------------------------
# Test A (BLOCKING): the registry-resolved dispatch model reaches the driver
# ---------------------------------------------------------------------------
def test_registry_dispatch_model_reaches_the_driver(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A story with NO ``model`` pin, dispatched on a registry-pinned
    ``roles.dispatch``, must be launched with the registry tag - not
    ``DEFAULT_MODEL`` ("sonnet") and not the driver's env default.

    RED today: the guard tests ``not spec.get("model")``, which is always
    False because ``_build_dispatch_command`` always returns a truthy model,
    so the driver is handed "sonnet" and ``story["dispatched_model"]`` is
    "sonnet".
    """
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")

    captured = _arm_dispatch(
        monkeypatch, tmp_path, plan_dir, repo, branch, "reg1", "S1",
        _story(persona="security-engineer"),
    )

    result = p.dispatch_story("reg1", "S1")
    assert result["ok"] is True, f"dispatch did not reach the launch: {result}"

    assert captured, "the dispatch driver was never invoked"
    model = captured[0]["model"]

    assert model == _REGISTRY_MODEL_TAG, (
        f"the registry pins roles.dispatch to {_REGISTRY_MODEL_TAG!r}, so that "
        f"is the model the driver must be launched with; got {model!r}. The "
        "override guard in pipeline/dispatch.py is gated on the always-truthy "
        "spec default instead of the story's own (absent) pin, so it is dead "
        "code and the registry model never reaches the driver."
    )
    assert model != _SPEC_DEFAULT, (
        f"the driver was launched with the spec default {_SPEC_DEFAULT!r} - the "
        "exact dead-code symptom the reviewer reported"
    )
    assert model != os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT"), (
        "the driver was launched with PIPELINE_LOCAL_MODEL_DEFAULT - the "
        "measured bug (every agent booting on the driver's env default)"
    )

    story = _read_story(plan_dir, "reg1", "S1")
    assert story["dispatched_model"] == _REGISTRY_MODEL_TAG, (
        "story['dispatched_model'] records the model the agent actually boots "
        f"with; got {story['dispatched_model']!r}, expected the registry tag "
        f"{_REGISTRY_MODEL_TAG!r}"
    )
    assert story["backend"] == "claude"


# ---------------------------------------------------------------------------
# Test B: an explicit story pin still wins over the registry
# ---------------------------------------------------------------------------
def test_story_model_pin_wins_over_registry_dispatch_model(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """``story["model"]`` is the most specific pin (an escalation flip writes
    a concrete tag there) and must NOT be overridden by the registry-resolved
    dispatch model."""
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")

    captured = _arm_dispatch(
        monkeypatch, tmp_path, plan_dir, repo, branch, "reg2", "S1",
        _story(persona="security-engineer", model="my-pinned-tag"),
    )

    result = p.dispatch_story("reg2", "S1")
    assert result["ok"] is True, f"dispatch did not reach the launch: {result}"
    assert captured, "the dispatch driver was never invoked"

    assert captured[0]["model"] == "my-pinned-tag", (
        "an explicit story['model'] pin must win over the registry-resolved "
        f"dispatch model; got {captured[0]['model']!r}"
    )
    story = _read_story(plan_dir, "reg2", "S1")
    assert story["dispatched_model"] == "my-pinned-tag"


# ---------------------------------------------------------------------------
# Test C: state left behind by call 1 must not drift call 2
# ---------------------------------------------------------------------------
def test_second_dispatch_of_same_story_keeps_the_registry_model(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Dispatching the SAME story dict twice must yield the registry tag both
    times. After call 1 the story carries ``dispatched_model`` but still has
    no ``model`` key, so the override must fire again - and the resolved tag
    must never be written back into ``story["model"]`` (that would freeze the
    story to the first resolution even if the registry changes)."""
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")

    captured = _arm_dispatch(
        monkeypatch, tmp_path, plan_dir, repo, branch, "reg3", "S1",
        _story(persona="security-engineer"),
    )

    first = p.dispatch_story("reg3", "S1")
    assert first["ok"] is True, f"first dispatch failed: {first}"
    second = p.dispatch_story("reg3", "S1")
    assert second["ok"] is True, f"second dispatch failed: {second}"

    assert len(captured) == 2, (
        f"expected two driver launches, recorded {len(captured)}"
    )
    assert captured[0]["model"] == _REGISTRY_MODEL_TAG, (
        f"call 1 launched with {captured[0]['model']!r}, expected the registry "
        f"tag {_REGISTRY_MODEL_TAG!r}"
    )
    assert captured[1]["model"] == _REGISTRY_MODEL_TAG, (
        f"call 2 launched with {captured[1]['model']!r}, expected the registry "
        f"tag {_REGISTRY_MODEL_TAG!r} - the override must fire again on the "
        "second dispatch, not just the first"
    )

    story = _read_story(plan_dir, "reg3", "S1")
    assert story["dispatched_model"] == _REGISTRY_MODEL_TAG, (
        f"after two dispatches story['dispatched_model'] is "
        f"{story['dispatched_model']!r}, expected {_REGISTRY_MODEL_TAG!r}"
    )
    assert "model" not in story, (
        "the resolved registry tag must NOT be written back into "
        "story['model'] - only story['dispatched_model'] records it, so a "
        "later registry change is still observed"
    )
