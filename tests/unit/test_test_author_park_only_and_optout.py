"""TDD-split enforcement: reject park-only test-author output and honor the
``[no-new-tests]`` opt-out sentinel (pipeline/planner.py + git_ops.py).

Two harness gaps surfaced live on W1a-10 (2026-08-10, Mode 51):

1. ``_run_test_author_phase`` accepted a park/checkpoint-only commit as
   success. The test-author phase drifted (Mode 31 off-task drift) and
   parked mid-write, leaving only a ``wip(<key>): parked on ...`` checkpoint
   commit (made by the harness's own ``_commit_wip``). The phase's success
   check ``_worktree_has_new_commits`` answered "did ANY commit land" -> True
   -- and handed a half-authored, unsatisfiable test file to the executor as
   a fixed, must-pass oracle. The executor never wrote a test (git forensics
   confirmed); the broken oracle came from the test-author phase itself.

2. ``_test_author_prompt`` unconditionally instructs "Write ONLY the test
   file(s)..." with no escape hatch. A pure behavior-preserving move (W1a's
   "move a tool body onto PipelineService; existing tests already cover it")
   has no new test to write, so forcing the phase to invent one produced the
   redundant structural-assertion oracle that then parked.

Fixes:
  - ``_worktree_has_non_wip_commits`` (git_ops): True iff the branch has a
    commit whose subject is NOT a ``wip(<key>):`` checkpoint. The test-author
    phases now require a REAL finished commit, not a park marker.
  - ``[no-new-tests]`` sentinel in ``agent_instructions``: opts a story out of
    the test-author phase entirely (fall open to monolithic dispatch, the
    existing fail-open contract -- the executor runs against the existing
    suite, the correct grade for a behavior-preserving move).

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q test_test_author_park_only_and_optout.py
"""

import subprocess
from pathlib import Path

import pytest
from test_pipeline_mcp_server import (
    _already_reaped_pid,
    _FakeTestAuthorBackend,
    _make_worktree_repo,
)

from app import backend
from pipeline import planner as pplanner
from pipeline import server as p
from pipeline.git_ops import _commit_wip, _worktree_has_non_wip_commits


@pytest.fixture(autouse=True)
def _quiet_notify(monkeypatch):
    monkeypatch.setattr(pplanner, "_notify_user", lambda *a, **k: None)


# ======================= _worktree_has_non_wip_commits =======================

def test_non_wip_commits_empty_branch_is_false(tmp_path: Path):
    """No commits on the branch at all -> False (subsumes the old
    _worktree_has_new_commits empty-branch guard)."""
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    assert _worktree_has_non_wip_commits(wt, "S1", "main") is False


def test_non_wip_commits_wip_only_is_false(tmp_path: Path):
    """The motivating case: the ONLY commit on the branch is a harness-made
    WIP checkpoint (``wip(S1): parked on off-task drift``). This records
    parked/interrupted state, not finished test-author work -> must be False
    so the phase falls open instead of handing a half-authored oracle to the
    executor."""
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_partial.py").write_text("def test_x(): assert False\n")
    _commit_wip(str(wt), "S1", "parked on off-task drift")
    assert _worktree_has_non_wip_commits(wt, "S1", "main") is False


def test_non_wip_commits_real_commit_is_true(tmp_path: Path):
    """A normal descriptive commit (what a successful test-author agent
    produces) -> True."""
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "Add tests for foo"],
                   cwd=wt, capture_output=True, text=True, check=True)
    assert _worktree_has_non_wip_commits(wt, "S1", "main") is True


def test_non_wip_commits_mixed_wip_and_real_is_true(tmp_path: Path):
    """A real test commit followed by a harness WIP checkpoint on top still
    counts as success -- the real finished commit is present, the WIP just
    records later interrupted state. Any non-WIP commit -> True."""
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "Add tests for foo"],
                   cwd=wt, capture_output=True, text=True, check=True)
    (wt / "test_foo.py").write_text("def test_x(): assert False\n")
    _commit_wip(str(wt), "S1", "interrupted")
    assert _worktree_has_non_wip_commits(wt, "S1", "main") is True


def test_non_wip_commits_git_error_is_false(tmp_path: Path):
    """A non-git cwd -> git fails -> fail open to False (mirrors
    _worktree_has_new_commits)."""
    assert _worktree_has_non_wip_commits(tmp_path, "S1", "main") is False


def test_non_wip_commits_wip_prefix_case_insensitive(tmp_path: Path):
    """The branch name lower-cases the story key but the WIP commit message
    uses the raw key; the prefix match must not depend on casing (a UUID key
    like ``F027e711-...`` could land either way)."""
    _repo, wt = _make_worktree_repo(tmp_path, "agent/f027e711-ab")
    (wt / "test_partial.py").write_text("def test_x(): assert False\n")
    # Use a mixed-case key in the WIP message, distinct from the lower branch.
    _commit_wip(str(wt), "F027E711-AB", "parked on repetition")
    assert _worktree_has_non_wip_commits(wt, "F027E711-AB", "main") is False


# ======================= Fix 2 wired into _run_test_author_phase =======================

def _wire_phase(monkeypatch, tmp_path):
    """Resolve test_author to a distinct backend, reap the dispatch pid
    immediately, fix the base branch -- leaves a real git worktree ready for
    the commit-shape under test."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    return wt, fake


def test_phase_returns_false_on_wip_only_commit(monkeypatch, tmp_path):
    """The phase dispatched, the agent exited, but the only commit on the
    branch is a WIP park checkpoint -> must fail open (return False), NOT
    hand the half-authored oracle to the executor as a fixed test suite."""
    wt, fake = _wire_phase(monkeypatch, tmp_path)
    (wt / "test_partial.py").write_text("def test_x(): assert False\n")
    _commit_wip(str(wt), "S1", "parked on off-task drift")

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan", plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert len(fake.calls) == 1, "the phase MUST dispatch before parking"
    assert result is False


def test_phase_returns_true_on_real_commit(monkeypatch, tmp_path):
    """Regression guard: a normal descriptive commit still reports success --
    the new non-WIP check must not over-reject genuine finished work."""
    wt, fake = _wire_phase(monkeypatch, tmp_path)
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "Add tests for foo"],
                   cwd=wt, capture_output=True, text=True, check=True)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan", plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert len(fake.calls) == 1, "the phase MUST dispatch"
    assert result is True


# ======================= Fix 1: [no-new-tests] opt-out sentinel =======================

def test_phase_opts_out_via_no_new_tests_sentinel(monkeypatch, tmp_path):
    """A story whose agent_instructions contains ``[no-new-tests]`` skips the
    test-author phase entirely -- no dispatch is attempted, the phase returns
    False (fall open to monolithic dispatch against the existing suite). This
    is the escape hatch for behavior-preserving refactors with no new test to
    write."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")

    def _boom(*a, **k):
        raise AssertionError(
            "backend.get_backend must not be called for a [no-new-tests] story")

    monkeypatch.setattr(backend, "get_backend", _boom)

    def _boom_resolve(*a, **k):
        raise AssertionError(
            "_resolve_test_author_backend must not be called for a "
            "[no-new-tests] story")

    monkeypatch.setattr(pplanner, "_resolve_test_author_backend", _boom_resolve)

    result = p._run_test_author_phase(
        {"agent_instructions": "Move decompose_plan body onto PipelineService; "
         "existing test_pipeline_mcp_server.py covers it. [no-new-tests]"},
        story_key="S1", worktree_path=tmp_path,
        dispatch_backend="ollama", local_model="gpt-oss:20b", plan_name="plan",
    )
    assert result is False


def test_phase_runs_when_sentinel_absent(monkeypatch, tmp_path):
    """Boundary: without the sentinel the phase proceeds normally -- the
    opt-out is opt-in, never a default. The dispatch is attempted (we wire a
    reaped pid so the phase reaches the commit check on a real empty worktree
    and returns False for 'no commit', proving dispatch was reached)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it. Write a test first."},
        story_key="S1", worktree_path=wt,
        dispatch_backend="ollama", local_model="gpt-oss:20b", plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    # Dispatch was reached (fake recorded a call) and no commit -> False.
    assert len(fake.calls) == 1, "phase MUST dispatch when sentinel is absent"
    assert result is False


def test_phase_opts_out_only_for_exact_sentinel(monkeypatch, tmp_path):
    """The sentinel is the literal token ``[no-new-tests]``; a nearby phrase
    like 'no new tests' (without the brackets) must NOT opt out -- free-text
    mentions of tests are common in real briefs and must not silently skip
    the phase."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it. There are no new tests needed here."},
        story_key="S1", worktree_path=wt,
        dispatch_backend="ollama", local_model="gpt-oss:20b", plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert len(fake.calls) == 1, "free-text 'no new tests' must NOT opt out"
    assert result is False


# ======================= Fix 2 wired into _run_rework_test_author_phase =======================

def test_rework_phase_returns_false_on_wip_only_commit(monkeypatch, tmp_path):
    """The rework test-author phase has the identical gap: a park-only WIP
    commit must fail open, not hand a half-authored regression test to the
    rework executor as a fixed oracle."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_partial.py").write_text("def test_x(): assert False\n")
    _commit_wip(str(wt), "S1", "parked on repetition")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._run_rework_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        review_feedback="Add a regression test for the crash.",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False