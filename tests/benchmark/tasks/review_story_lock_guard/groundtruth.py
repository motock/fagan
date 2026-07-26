"""Independent ground-truth oracle for the review_story_lock_guard benchmark task.

Investigator-authored (never the dispatched model's own tests). Run by the
real-repo driver against the merged/surviving code to judge whether the fix
is actually correct, regardless of what the model's own acceptance suite
checked.

Calling convention (mirrors the other ``tests/benchmark/tasks/*/groundtruth.py``
files, which run via pytest inside the repo root so the module-under-test is
importable): ``import pipeline_mcp_server as p`` is expected to resolve here.
The assertions below assume that alias is already set up by the caller.
"""
import fcntl
import json
import os

import pytest

import pipeline_mcp_server as p

# ---------------------------------------------------------------------------
# Helpers (replicated standalone - do not import across test files).
# ---------------------------------------------------------------------------

def _write_manifest_with_story(plan_dir, plan_name, story_key, story):
    """Write a manifest containing a single story with arbitrary fields."""
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {story_key: story},
    }))


def _make_story(plan_dir, status="tests_passed", worktree=None, **extra):
    story = {
        "key": "S1",
        "summary": "x",
        "agent_instructions": "x",
        "persona": "software-engineer",
        "model": "sonnet",
        "risk": "low",
        "dependencies": [],
        "acceptance": [],
        "status": status,
        "worktree": str(plan_dir / "wt") if worktree is None else worktree,
    }
    story.update(extra)
    return story


# ---------------------------------------------------------------------------
# 1. The lock guard must reach the REGISTERED MCP tool, not dead code.
#    (rename-and-delegate registration-path failure shape from CLAUDE.md.)
# ---------------------------------------------------------------------------

def test_registered_mcp_tool_is_the_real_review_story():
    """The FastMCP-registered ``review_story`` tool must delegate to the real
    ``p.review_story`` function. A common failure is to rename the original
    and register a dead-code wrapper that never acquires the lock."""
    tools = p.mcp._tool_manager._tools
    assert "review_story" in tools, "review_story must be registered as an MCP tool"
    assert tools["review_story"].fn is p.review_story, (
        "the registered MCP tool's fn must BE p.review_story, not a stale "
        "wrapper that bypasses the lock guard"
    )


def test_advance_pipeline_still_registered():
    """No regression to the sibling advance_pipeline tool - the exact bug the
    real model introduced on its second attempt (clobbering the registry)."""
    assert "advance_pipeline" in p.mcp._tool_manager._tools, (
        "advance_pipeline must still be registered as an MCP tool"
    )


def test_review_story_docstring_survived():
    """The docstring must survive onto the real wrapper - a wrapper that
    drops the docstring is a sign of a non-delegating rename."""
    assert p.review_story.__doc__, (
        "review_story must retain a non-empty docstring on the real wrapper"
    )


# ---------------------------------------------------------------------------
# 2. Validate-before-lock ordering: an invalid/path-traversal plan_name must
#    raise ValueError AND must not create a stray .lock file outside PLAN_DIR.
#    (path-traversal finding from the real story's first review round.)
# ---------------------------------------------------------------------------

def test_invalid_plan_name_raises_and_creates_no_stray_lock(tmp_path, monkeypatch):
    """A path-traversal plan_name must raise ValueError before any lock
    acquisition, and must not leave a .lock file anywhere outside PLAN_DIR."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    # Snapshot the filesystem under tmp_path before the call so we can detect
    # any stray .lock file created as a side effect.
    locks_before = {
        str(f.relative_to(tmp_path))
        for f in tmp_path.rglob("*.lock")
    }

    with pytest.raises(ValueError):
        p.review_story("../../../../tmp/x", "S1")

    locks_after = {
        str(f.relative_to(tmp_path))
        for f in tmp_path.rglob("*.lock")
    }
    new_locks = locks_after - locks_before
    assert not new_locks, (
        f"an invalid plan_name must not create any stray .lock file; "
        f"new locks: {new_locks!r}"
    )


# ---------------------------------------------------------------------------
# 3. The actual Mode 29 race: hold the plan's lock externally, then call
#    review_story - it must skip as 'locked' and never invoke the reviewer.
# ---------------------------------------------------------------------------

def test_review_story_skips_when_lock_held(tmp_path, monkeypatch):
    """The real concurrency guard: with the plan's lock file held externally
    via fcntl.flock, review_story must acquire _plan_lock, find it
    unavailable, and skip immediately - never invoking the reviewer. This is
    the actual Mode 29 race this whole story exists to close."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(p, "AGENTS_DIR", agents_dir)

    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)

    reviewer_calls = []
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE",
    )

    # Hold the per-plan flock externally, mirroring the lock-held test.
    lock_path = plan_dir / "rv.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.review_story("rv", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.get("skipped") == "locked", (
        f"expected skipped=='locked' when the lock is held, got {result!r}"
    )
    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked when the lock is held; "
        f"calls={reviewer_calls!r}"
    )