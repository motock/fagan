"""Tests for dispatch honouring a pending rebase-conflict handoff.

Background
----------
The merge gate (``pipeline/advance_merge.py``) and overlord triage
(``pipeline/triage_rebase.py``) both hand a rebase conflict to the implementer
by writing a REBASE CONFLICT brief and setting the story back to a resumable
state. Both briefs tell the agent to run the rebase itself. But the resume
block in ``dispatch.py`` runs ``_rebase_onto_master`` BEFORE the agent starts,
and on a conflict it parked the story, so the agent never ran.

Both handoff sites now set ``story["conflict_handoff"] = True``. The resume
block honours that explicit marker: it notifies, lets the agent run on the
worktree as it is, and clears the marker once the agent is actually spawned.
Without the marker a resumed conflict still parks.

What is graded here
-------------------
These tests drive the REAL ``_dispatch_story_impl`` through
``pipeline.server.dispatch_story`` against a REAL git origin/repo/worktree.
``_rebase_onto_master`` is monkeypatched to report a conflict so the branch is
reached deterministically, and the agent backend's ``claude`` subprocess is a
fake that records the launch.
"""

import json
import subprocess

import pytest

from app import backend
from pipeline import dispatch as pdisp
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt


# ---------- Fixtures (copied from test_dispatch_park_event.py) ----------
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


# ---------- Real-git harness (copied from test_dispatch_park_event.py) ----------
def _write_manifest(plan_dir, plan_name, stories, repo_root=None):
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = str(repo_root)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _set_story_fields(plan_dir, plan_name, story_key, **fields):
    path = plan_dir / f"{plan_name}.manifest.json"
    manifest = json.loads(path.read_text())
    manifest["stories"][story_key].update(fields)
    path.write_text(json.dumps(manifest))


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


def _push_conflicting_commit_to_origin(tmp_path, origin, branch):
    """Push a commit to `origin` that rewrites README.md, so origin's default
    branch has moved ahead of the worktree base and the staleness check
    reports the worktree as behind."""
    other = tmp_path / "other-clone-conflict"
    _run(["git", "clone", "-q", str(origin), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / "README.md").write_text("origin side\n")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "origin edit"], other)
    _run(["git", "push", "-q", "origin", branch], other)


def _make_resumed_worktree(tmp_path, worktree_root, repo, branch, story_key="S1"):
    """Create a REAL git worktree at worktree_root/<story_key> on an
    `agent/<story_key>` branch off repo's current HEAD."""
    worktree_path = worktree_root / story_key
    agent_branch = f"agent/{story_key.lower()}"
    _run(
        ["git", "worktree", "add", "-b", agent_branch, str(worktree_path), "HEAD"],
        repo,
    )
    return worktree_path


_CONFLICT_ERROR = "CONFLICT (content): merge conflict in README.md"


def _dispatch(
    plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
    repo, branch, *, status, marker=None, notify_calls=None, launched=None,
    keep_manifest=False,
):
    """Dispatch a story against a REAL repo and worktree.

    ``_rebase_onto_master`` is monkeypatched to report a conflict, so the
    conflict branch is reached deterministically. ``claude`` launches are
    recorded in ``launched``. ``_notify_user`` is captured on both
    ``pipeline.server`` and ``pipeline.dispatch``.
    """
    worktree_path = worktree_root / story_key
    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": status, "dependencies": [],
        "worktree": str(worktree_path),
    }
    if marker is not None:
        story["conflict_handoff"] = marker
    if not keep_manifest:
        _write_manifest(plan_dir, plan_name, {story_key: story}, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_rebase_onto_master",
        lambda wt, br: {"ok": False, "conflict": True, "error": _CONFLICT_ERROR})

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            if launched is not None:
                launched.append(cmd)
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)

    if notify_calls is not None:
        def fake_notify(*args, **kwargs):
            notify_calls.append({"args": args, "kwargs": kwargs})
        monkeypatch.setattr(p, "_notify_user", fake_notify)
        monkeypatch.setattr(pdisp, "_notify_user", fake_notify)

    return p.dispatch_story(plan_name, story_key)


def _events(notify_calls, event):
    return [c for c in notify_calls if c["kwargs"].get("event") == event]


def _setup_behind_origin(tmp_path, worktree_root, repo_branch_pair):
    origin, repo, branch = repo_branch_pair
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_conflicting_commit_to_origin(tmp_path, origin, branch)
    return repo, branch


# ---------------------------------------------------------------------------
# (1) POSITIVE: a pending handoff dispatches the agent instead of parking.
# ---------------------------------------------------------------------------


def test_should_dispatch_the_agent_when_a_merge_gate_conflict_handoff_is_pending(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    repo_pair = _make_origin_and_repo(tmp_path)
    repo, branch = _setup_behind_origin(tmp_path, worktree_root, repo_pair)
    notify_calls, launched = [], []

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "hoff1", "S1", repo, branch,
        status="changes_requested", marker=True,
        notify_calls=notify_calls, launched=launched,
    )

    assert len(launched) == 1, "the agent backend must be launched"
    assert _read_story(plan_dir, "hoff1", "S1")["status"] != "parked"
    handed = _events(notify_calls, "story_conflict_handed_to_agent")
    assert len(handed) == 1, f"expected one handoff notice, got: {notify_calls}"
    assert handed[0]["kwargs"]["story_key"] == "S1"
    message = handed[0]["args"][1]
    assert "S1" in message and branch in message


def test_should_dispatch_the_agent_when_an_overlord_conflict_handoff_is_pending(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    repo_pair = _make_origin_and_repo(tmp_path)
    repo, branch = _setup_behind_origin(tmp_path, worktree_root, repo_pair)
    notify_calls, launched = [], []

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "hoff2", "S1", repo, branch,
        status="todo", marker=True,
        notify_calls=notify_calls, launched=launched,
    )

    assert len(launched) == 1, "the agent backend must be launched"
    assert _read_story(plan_dir, "hoff2", "S1")["status"] != "parked"
    assert _events(notify_calls, "story_conflict_handed_to_agent")


def test_should_clear_the_handoff_marker_once_the_agent_is_dispatched(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    repo_pair = _make_origin_and_repo(tmp_path)
    repo, branch = _setup_behind_origin(tmp_path, worktree_root, repo_pair)
    launched = []

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "hoff3", "S1", repo, branch,
        status="changes_requested", marker=True, launched=launched,
    )

    assert len(launched) == 1
    assert "conflict_handoff" not in _read_story(plan_dir, "hoff3", "S1")


def test_should_park_the_next_resume_after_the_handoff_was_consumed(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    repo_pair = _make_origin_and_repo(tmp_path)
    repo, branch = _setup_behind_origin(tmp_path, worktree_root, repo_pair)
    launched = []
    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "hoff4", "S1", repo, branch,
        status="changes_requested", marker=True, launched=launched,
    )
    assert len(launched) == 1

    _set_story_fields(plan_dir, "hoff4", "S1", status="interrupted")
    notify_calls = []
    result = _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "hoff4", "S1", repo, branch,
        status="interrupted", notify_calls=notify_calls, launched=launched,
        keep_manifest=True,
    )

    assert result["status"] == "parked"
    assert len(launched) == 1, "the second resume must not launch the agent"
    assert _events(notify_calls, "story_parked")


# ---------------------------------------------------------------------------
# (2) NEGATIVE: without a genuine handoff marker, a conflict still parks.
# ---------------------------------------------------------------------------


def test_should_still_park_a_resumed_conflict_without_a_handoff(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    repo_pair = _make_origin_and_repo(tmp_path)
    repo, branch = _setup_behind_origin(tmp_path, worktree_root, repo_pair)
    notify_calls, launched = [], []

    result = _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park_nohand", "S1", repo, branch,
        status="interrupted", notify_calls=notify_calls, launched=launched,
    )

    assert result["status"] == "parked"
    assert _read_story(plan_dir, "park_nohand", "S1")["status"] == "parked"
    assert _events(notify_calls, "story_parked")
    assert launched == []


@pytest.mark.parametrize("marker", ["yes", 1])
def test_should_still_park_when_the_marker_is_not_exactly_true(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path, marker,
):
    repo_pair = _make_origin_and_repo(tmp_path)
    repo, branch = _setup_behind_origin(tmp_path, worktree_root, repo_pair)
    launched = []

    result = _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park_marker", "S1", repo, branch,
        status="changes_requested", marker=marker, launched=launched,
    )

    assert result["status"] == "parked"
    assert launched == []
