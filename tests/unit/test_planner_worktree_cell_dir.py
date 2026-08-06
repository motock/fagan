"""Tests for wiring the planner/rework-planner roles' token-cost sidecar
(``cell_dir``) into ``pipeline/planner.py`` and ``pipeline/server.py``.

CONTEXT (docs/plans/TOKEN_CONTEXT_OPTIMIZATION_PLAN.md Step 0):
``pipeline/review.py``'s ``_run_reviewer``/``_run_security_reviewer`` already
compute a ``cell_dir`` from the worktree path (when
``Path(worktree).parent.name == "worktrees"``) and pass it -- together with a
``role`` kwarg -- into the backend's ``complete()`` call, which triggers the
per-call JSONL token-cost sidecar. The planner role makes the same kind of
``complete()`` call but currently NEVER passes ``cell_dir``/``role``, so there
is zero recorded cache-hit data for the planner role even though the same
underlying caching mechanism is in play.

This story closes that gap by:

1. Adding a ``worktree: str | None = None`` keyword parameter to BOTH
   ``_run_planner`` and ``_run_rework_planner`` in ``pipeline/planner.py``.
   Inside each, ``cell_dir`` is computed exactly as ``pipeline/review.py``
   does it::

       if worktree is not None and Path(worktree).parent.name == "worktrees":
           cell_dir = str(Path(worktree).resolve().parent)
       else:
           cell_dir = None

   and then ``cell_dir=cell_dir, role="planner"`` (for ``_run_planner``) /
   ``cell_dir=cell_dir, role="rework_planner"`` (for ``_run_rework_planner``)
   are passed into the ``complete()`` call. ``_run_decompose`` is untouched
   (it runs before any worktree exists).

2. At the two call sites in ``pipeline/server.py`` (the ``_run_planner`` call
   near the initial-dispatch checklist, and the ``_run_rework_planner`` call
   in the rework path), adding ``worktree=str(worktree_path)`` to the call's
   keyword arguments.

The ``worktree`` parameter defaults to ``None`` so any other caller (tests,
benchmark harness) that doesn't pass it keeps working exactly as before --
``cell_dir`` stays ``None``, no sidecar write, unchanged behavior.

These tests must FAIL before the implementation exists (the ``worktree``
parameter and the ``cell_dir``/``role`` wiring are absent) and PASS after.
"""

import json

import pytest
from test_pipeline_mcp_server import _FakeProc, _write_manifest

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt

# --------------------------------------------------------------------------- #
# Unit-level helpers (mirror test_always_on_planner.py's _FakePlannerBackend)
# --------------------------------------------------------------------------- #

class _FakePlannerBackend:
    """Stand-in for whatever ``backend.get_backend(...)`` returns, capturing
    the exact kwargs ``_run_planner``/``_run_rework_planner`` passed to
    ``complete()``."""

    def __init__(self, response="1. Step one"):
        self._response = response
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self._response


def _install_fake_planner_backend(monkeypatch):
    """Install a fake backend whose ``get_backend`` returns a recording
    driver, and clear the planner env overrides so resolution is stable."""
    fake = _FakePlannerBackend()
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    return fake


def _worktree_under_worktrees(tmp_path, story="STORY-1"):
    """Build a worktree path whose parent dir is named ``worktrees`` so the
    ``cell_dir`` branch is exercised (mirrors production layout under
    ``WORKTREE_ROOT=~/.claude/worktrees``)."""
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    wt = worktrees / story
    wt.mkdir()
    return str(wt)


# --------------------------------------------------------------------------- #
# _run_planner: cell_dir / role wiring
# --------------------------------------------------------------------------- #

def test_run_planner_passes_cell_dir_and_role_when_worktree_under_worktrees(
    monkeypatch, tmp_path,
):
    """Positive: ``_run_planner(..., worktree=<under worktrees/>)`` must call
    ``complete()`` with ``cell_dir`` set to the resolved parent of the
    worktree and ``role="planner"``."""
    fake = _install_fake_planner_backend(monkeypatch)
    wt = _worktree_under_worktrees(tmp_path, story="STORY-1")

    p._run_planner(
        "Build the thing.",
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        worktree=wt,
    )

    assert len(fake.calls) == 1, "complete() was never called"
    call = fake.calls[0]
    assert call["role"] == "planner"
    # cell_dir is the resolved parent of the worktree (the worktrees/ dir).
    from pathlib import Path

    expected_cell_dir = str(Path(wt).resolve().parent)
    assert call["cell_dir"] == expected_cell_dir


def test_run_planner_cell_dir_none_when_worktree_omitted(monkeypatch, tmp_path):
    """Negative: ``_run_planner`` called WITHOUT ``worktree`` must call
    ``complete()`` with ``cell_dir=None`` (no change from current behavior --
    no sidecar write)."""
    fake = _install_fake_planner_backend(monkeypatch)

    p._run_planner(
        "Build the thing.",
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
    )

    assert len(fake.calls) == 1, "complete() was never called"
    call = fake.calls[0]
    assert call.get("cell_dir") is None
    # role must still be passed (the story wires role="planner" unconditionally
    # alongside cell_dir); but cell_dir must be None so no sidecar is written.
    assert call["role"] == "planner"


def test_run_planner_cell_dir_none_when_worktree_parent_not_worktrees(
    monkeypatch, tmp_path,
):
    """Boundary: when ``worktree`` is supplied but its parent dir is NOT named
    ``worktrees`` (e.g. a live review whose worktree lives somewhere we
    shouldn't scribble into), ``cell_dir`` must be ``None`` -- mirroring
    ``pipeline/review.py``'s guard exactly."""
    fake = _install_fake_planner_backend(monkeypatch)
    # A worktree whose parent is NOT "worktrees".
    elsewhere = tmp_path / "scratch" / "STORY-1"
    elsewhere.parent.mkdir()
    elsewhere.mkdir()

    p._run_planner(
        "Build the thing.",
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        worktree=str(elsewhere),
    )

    assert len(fake.calls) == 1, "complete() was never called"
    call = fake.calls[0]
    assert call.get("cell_dir") is None
    assert call["role"] == "planner"


def test_run_planner_accepts_worktree_keyword(monkeypatch, tmp_path):
    """The ``worktree`` keyword parameter must exist on ``_run_planner`` (the
    implementation adds it). Calling with it must not raise ``TypeError``."""
    _install_fake_planner_backend(monkeypatch)
    wt = _worktree_under_worktrees(tmp_path)
    # Must not raise TypeError: worktree is a recognized keyword.
    p._run_planner(
        "Build it.", dispatch_backend="ollama", local_model="gpt-oss:20b",
        worktree=wt,
    )


# --------------------------------------------------------------------------- #
# _run_rework_planner: cell_dir / role wiring
# --------------------------------------------------------------------------- #

def test_run_rework_planner_passes_cell_dir_and_role_when_worktree_under_worktrees(
    monkeypatch, tmp_path,
):
    """Positive: ``_run_rework_planner(..., worktree=<under worktrees/>)`` must
    call ``complete()`` with ``cell_dir`` set to the resolved parent of the
    worktree and ``role="rework_planner"``."""
    fake = _install_fake_planner_backend(monkeypatch)
    wt = _worktree_under_worktrees(tmp_path, story="STORY-1")

    p._run_rework_planner(
        "allow() double-counts refill.",
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        worktree=wt,
    )

    assert len(fake.calls) == 1, "complete() was never called"
    call = fake.calls[0]
    assert call["role"] == "rework_planner"
    from pathlib import Path

    expected_cell_dir = str(Path(wt).resolve().parent)
    assert call["cell_dir"] == expected_cell_dir


def test_run_rework_planner_cell_dir_none_when_worktree_omitted(monkeypatch, tmp_path):
    """Negative: ``_run_rework_planner`` called WITHOUT ``worktree`` must call
    ``complete()`` with ``cell_dir=None`` (no change from current behavior)."""
    fake = _install_fake_planner_backend(monkeypatch)

    p._run_rework_planner(
        "allow() double-counts refill.",
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
    )

    assert len(fake.calls) == 1, "complete() was never called"
    call = fake.calls[0]
    assert call.get("cell_dir") is None
    assert call["role"] == "rework_planner"


def test_run_rework_planner_cell_dir_none_when_worktree_parent_not_worktrees(
    monkeypatch, tmp_path,
):
    """Boundary: when ``worktree`` is supplied but its parent dir is NOT named
    ``worktrees``, ``cell_dir`` must be ``None``."""
    fake = _install_fake_planner_backend(monkeypatch)
    elsewhere = tmp_path / "scratch" / "STORY-1"
    elsewhere.parent.mkdir()
    elsewhere.mkdir()

    p._run_rework_planner(
        "allow() double-counts refill.",
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        worktree=str(elsewhere),
    )

    assert len(fake.calls) == 1, "complete() was never called"
    call = fake.calls[0]
    assert call.get("cell_dir") is None
    assert call["role"] == "rework_planner"


def test_run_rework_planner_accepts_worktree_keyword(monkeypatch, tmp_path):
    """The ``worktree`` keyword parameter must exist on ``_run_rework_planner``."""
    _install_fake_planner_backend(monkeypatch)
    wt = _worktree_under_worktrees(tmp_path)
    p._run_rework_planner(
        "Fix it.", dispatch_backend="ollama", local_model="gpt-oss:20b",
        worktree=wt,
    )


# --------------------------------------------------------------------------- #
# _run_decompose is OUT OF SCOPE: it must NOT gain a worktree/cell_dir/role
# wiring (it runs before any worktree exists).
# --------------------------------------------------------------------------- #

def test_run_decompose_does_not_accept_worktree_keyword(monkeypatch):
    """``_run_decompose`` is explicitly out of scope: it must NOT accept a
    ``worktree`` keyword (it runs before any worktree exists). Calling it with
    ``worktree=`` must raise ``TypeError`` -- guarding against an
    over-eager implementation that wires it into all three planner functions."""
    _install_fake_planner_backend(monkeypatch)
    with pytest.raises(TypeError):
        p._run_decompose("Some request.", worktree="/tmp/worktrees/STORY-1")


# --------------------------------------------------------------------------- #
# Call-site wiring in pipeline/server.py (the actual gap this story closes)
# --------------------------------------------------------------------------- #
#
# These exercise the REAL dispatch_story flow (not _run_planner in isolation)
# and assert that ``worktree=str(worktree_path)`` is actually passed through
# at BOTH call sites. They mirror the dispatch-flow tests in
# test_always_on_planner.py / test_rework_test_author_wiring_acceptance.py.

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
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\n'
        "Engineer body.\n"
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\n'
        "Reviewer body.\n"
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\n'
        "Analyst body.\n"
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
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


def test_dispatch_story_passes_worktree_to_run_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Call-site wiring (initial dispatch): ``dispatch_story`` must pass
    ``worktree=str(worktree_path)`` into the ``_run_planner`` call -- this is
    the actual gap this story exists to close, not just the unit-level
    parameter."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    worktree_path = worktree_root / "S1"
    _write_manifest(plan_dir, "wtcell", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append(kwargs)
        return "1. Write a failing test.\n2. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("wtcell", "S1")

    assert result["ok"] is True
    assert len(planner_calls) == 1
    # The call site must pass worktree=str(worktree_path).
    assert planner_calls[0].get("worktree") == str(worktree_path)


def test_dispatch_story_passes_worktree_to_run_rework_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Call-site wiring (rework): ``dispatch_story``'s rework path must pass
    ``worktree=str(worktree_path)`` into the ``_run_rework_planner`` call."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    # A transcript is required for the rework path to resume and reach the
    # _run_rework_planner call site.
    (worktree_path / ".agent_transcript.json").write_text(
        json.dumps(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "task"},
            ]
        )
    )
    _write_manifest(plan_dir, "rwcell", {
        "S1": {
            "summary": "Do thing",
            "agent_instructions": "Build it.",
            "status": "changes_requested",
            "worktree": str(worktree_path),
            "review_feedback": "The `since` comparison crashes on a naive timestamp.",
            "last_reviewed_sha": "deadbeef",
        },
    })

    rework_planner_calls = []

    def _fake_rework_planner(review_feedback, **kwargs):
        rework_planner_calls.append(kwargs)

    monkeypatch.setattr(p, "_run_rework_planner", _fake_rework_planner)
    # The rework test-author phase and new-tests check are not under test here;
    # stub them so the flow reaches/returns from the rework planner call.
    monkeypatch.setattr(p, "_rework_requires_new_tests", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_rework_test_author_phase", lambda *a, **k: True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("rwcell", "S1")

    assert result["ok"] is True
    assert len(rework_planner_calls) == 1, (
        "rework planner was not called -- rework path did not reach the call site"
    )
    assert rework_planner_calls[0].get("worktree") == str(worktree_path)