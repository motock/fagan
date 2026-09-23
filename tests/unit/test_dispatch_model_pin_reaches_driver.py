"""Behavioural regression tests for LDC-7: a story's bare registry model
NAME pin must reach the driver as its concrete tag.

Reviewer's blocking finding (this file exists to reproduce it)
-------------------------------------------------------------
``_registry_tag_for`` (``pipeline/dispatch_routing.py``) translates a bare
registry name like ``deepseek-v4.1-flash`` to its concrete tag
``deepseek-v4.1-flash:cloud``, and ``_resolve_dispatch_target`` routes the
story pin through it - but the translated value only lands in
``dispatch_model``, which ``pipeline/dispatch.py`` applies to ``spec["model"]``
ONLY when the story has NO model (``if not story.get("model") and
dispatch_model:``). For exactly the stories this change targets - ones that DO
have a pin - ``spec["model"]`` keeps the raw story value (built in
``pipeline/persona.py`` as ``story.get("model") or persona default or
DEFAULT_MODEL``) and that is what the driver receives
(``local_model=spec["model"]``), so ``_resolve_local_model`` - which only
knows the tiers ``opus|sonnet|haiku`` - still falls back to
``PIPELINE_LOCAL_MODEL_DEFAULT``.

These tests drive the REAL ``_dispatch_story_impl`` (through
``pipeline.server.dispatch_story``) against a REAL git origin/repo/worktree,
with a synthetic registry installed via ``PIPELINE_MODEL_REGISTRY_PATH``, and
assert on the model the driver is actually launched with and on
``story["dispatched_model"]``.

Per ``.claude/rules/testing-config-gates.md`` every test stubs the registry
with a synthetic payload; nothing asserts against this machine's real
``model_registry``.

Expected state: RED until the translated tag is applied to the value the
driver receives (``spec["model"]``) for stories that carry a pin.
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
# roles.dispatch resolves provider "ollama" with a concrete tag; the STORY
# pins the bare NAME "deepseek-v4.1-flash", which the registry maps to the
# tag "deepseek-v4.1-flash:cloud" under that same provider.
_BARE_NAME = "deepseek-v4.1-flash"
_CONCRETE_TAG = "deepseek-v4.1-flash:cloud"
_REGISTRY_PROVIDERS = {
    "ollama": {"models": {_BARE_NAME: {"tag": _CONCRETE_TAG}}},
}
_REGISTRY_DISPATCH = {"provider": "ollama", "model": _CONCRETE_TAG}
# The driver's own env default - the value the bug makes every agent boot on.
_DRIVER_ENV_DEFAULT = "llama3:8b"
# The spec default (DEFAULT_MODEL) - the other wrong value the bug degrades to.
_SPEC_DEFAULT = "sonnet"


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
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


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
        self._sink.append(kwargs)
        return self._inner.dispatch(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _no_plane(*args, **kwargs):
    raise RuntimeError("no plane")


def _arm_dispatch(
    monkeypatch, tmp_path, plan_dir, repo, branch, plan_name, story_key, story,
):
    """Install the synthetic registry + manifest and monkeypatch the infra
    seams, returning the list the driver's launch kwargs are recorded into.

    The manifest is written exactly ONCE, so a test may call
    ``p.dispatch_story`` repeatedly against the same story dict."""
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
# Test A (BLOCKING): the translated tag reaches the driver
# ---------------------------------------------------------------------------
def test_bare_registry_pin_reaches_the_driver_as_its_tag(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A story pinned to the bare registry NAME ``deepseek-v4.1-flash`` must
    be launched with that entry's concrete tag ``deepseek-v4.1-flash:cloud``
    - not the raw bare name, not ``DEFAULT_MODEL`` ("sonnet"), and not the
    driver's env default.

    RED today: ``spec["model"]`` keeps the raw story value (the
    ``dispatch_model`` override is gated on ``not story.get("model")``, which
    is False for exactly the pinned stories), so the driver is handed the
    bare name and ``_resolve_local_model`` degrades it to
    ``PIPELINE_LOCAL_MODEL_DEFAULT``.
    """
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")

    captured = _arm_dispatch(
        monkeypatch, tmp_path, plan_dir, repo, branch, "pin1", "S1",
        _story(model=_BARE_NAME),
    )

    result = p.dispatch_story("pin1", "S1")
    assert result["ok"] is True, f"dispatch did not reach the launch: {result}"

    assert captured, "the dispatch driver was never invoked"
    model = captured[0]["model"]

    assert model == _CONCRETE_TAG, (
        f"the story pins the bare registry name {_BARE_NAME!r}, so the driver "
        f"must be launched with its concrete tag {_CONCRETE_TAG!r}; got "
        f"{model!r}. The translated tag never reaches the driver: spec["
        '"model"] keeps the raw story value because the dispatch_model '
        'override is gated on `not story.get("model")` - always False for '
        "exactly the pinned stories - so _resolve_local_model degrades the "
        "bare name to the driver's default."
    )
    assert model != _BARE_NAME, (
        "the driver was launched with the raw bare name - _resolve_local_model "
        "does not know it and would run its default model instead"
    )
    assert model != _SPEC_DEFAULT, (
        f"the driver was launched with the spec default {_SPEC_DEFAULT!r} - "
        "the pin never reached the driver"
    )
    assert model != os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT"), (
        "the driver was launched with PIPELINE_LOCAL_MODEL_DEFAULT - the "
        "measured bug (every agent booting on the driver's env default)"
    )

    story = _read_story(plan_dir, "pin1", "S1")
    assert story["dispatched_model"] == _CONCRETE_TAG, (
        "story['dispatched_model'] records the model the agent actually boots "
        f"with; got {story['dispatched_model']!r}, expected the concrete tag "
        f"{_CONCRETE_TAG!r}"
    )
    assert story["backend"] == "ollama"


# ---------------------------------------------------------------------------
# Test B: a concrete tag pin is passed through unchanged
# ---------------------------------------------------------------------------
def test_concrete_tag_pin_reaches_the_driver_verbatim(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A story pinned to an already-concrete tag must reach the driver
    unchanged - the translation must never mangle a tag that is not a bare
    registry name."""
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")

    captured = _arm_dispatch(
        monkeypatch, tmp_path, plan_dir, repo, branch, "pin2", "S1",
        _story(model="glm-5.3-flash:cloud"),
    )

    result = p.dispatch_story("pin2", "S1")
    assert result["ok"] is True, f"dispatch did not reach the launch: {result}"

    assert captured, "the dispatch driver was never invoked"
    model = captured[0]["model"]

    assert model == "glm-5.3-flash:cloud", (
        f"an already-concrete tag pin must reach the driver verbatim; got "
        f"{model!r}"
    )