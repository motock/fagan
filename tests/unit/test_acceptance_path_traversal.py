"""SEC-1: acceptance fixture paths that escape the worktree must be rejected.

``pipeline/dispatch.py`` materialized each acceptance fixture with
``target = worktree_path / entry["path"]`` and
``target.parent.mkdir(parents=True, exist_ok=True)`` without validating the
path. An absolute path or a ``..`` component therefore escaped the worktree
and wrote arbitrary files as the operator.

Two layers now guard the same plan-authored data:

* ``pipeline.ingest._acceptance_field_error`` rejects such a manifest at
  ingest time (``ok=False``), mirroring the existing ``files`` validation.
* ``pipeline.dispatch`` skips (and notifies) any entry that still reaches it,
  mirroring the guard ``pipeline/build_detect.py`` already applies.

The dispatch tests drive the REAL ``dispatch_story`` entrypoint end to end,
following the harness of ``tests/unit/test_dispatch_acceptance_overwrite.py``
(a real git origin + clone, a stubbed ``_default_branch``/``plane_request`` and
a discriminating ``backend.subprocess.Popen``). The ingest tests follow
``tests/unit/test_ingest_story_files_field.py``. Fixtures are copied locally
per this repo's convention - no shared conftest.
"""

import json
import subprocess

import pytest

from app import backend
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt

_PREFLIGHT = "Preflight: test fixture - not a real plan"


# ---------- Fixtures (copied from test_dispatch_acceptance_overwrite.py) ----------
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


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_origin(tmp_path, branch="main"):
    """Bare ``origin`` + local clone ``repo``, both on ``branch``."""
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


def _write_manifest(plan_dir, plan_name, story_key, story, repo_root):
    manifest = {"repo_root": str(repo_root), "epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _dispatch(
    plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
    repo_root, branch, story, notifications=None,
):
    """Write the manifest and drive the REAL ``dispatch_story``."""
    _write_manifest(plan_dir, plan_name, story_key, story, repo_root)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    if notifications is not None:
        monkeypatch.setattr(
            p, "_notify_user", lambda plan, msg: notifications.append(msg)
        )

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    return p.dispatch_story(plan_name, story_key)


def _story(**extra):
    story = {
        "summary": "Do thing",
        "agent_instructions": "Build it.",
        "status": "todo",
        "dependencies": [],
    }
    story.update(extra)
    return story


# ---------------------------------------------------------------------------
# 1. dispatch: a `..` path is skipped and nothing escapes the worktree.
# ---------------------------------------------------------------------------
def test_dispatch_skips_parent_traversal_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin(tmp_path)
    story = _story(acceptance=[{"path": "../escape.txt", "source": "PWNED"}])

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch, "escape", "S1",
        repo, branch, story,
    )

    assert not (tmp_path / "escape.txt").exists(), (
        "a `..` acceptance path escaped the worktree and wrote a file outside it"
    )
    assert not (worktree_root / "escape.txt").exists()
    assert not (worktree_root / "S1" / "escape.txt").exists()


# ---------------------------------------------------------------------------
# 2. dispatch: an absolute path is skipped and nothing is written.
# ---------------------------------------------------------------------------
def test_dispatch_skips_absolute_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin(tmp_path)
    # Absolute, but under tmp_path so a broken guard cannot write to the real
    # /tmp.
    absolute = str(tmp_path / "escape_abs.txt")
    story = _story(acceptance=[{"path": absolute, "source": "PWNED"}])

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch, "abs", "S1",
        repo, branch, story,
    )

    assert not (tmp_path / "escape_abs.txt").exists(), (
        "an absolute acceptance path escaped the worktree and wrote a file"
    )


# ---------------------------------------------------------------------------
# 3. dispatch: a valid relative path is still written with its source.
# ---------------------------------------------------------------------------
def test_dispatch_writes_valid_relative_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin(tmp_path)
    story = _story(
        acceptance=[{"path": "tests/x.py", "source": "print(1)"}],
    )

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch, "valid", "S1",
        repo, branch, story,
    )

    written = worktree_root / "S1" / "tests" / "x.py"
    assert written.read_text() == "print(1)", (
        "a valid relative acceptance fixture was not materialized"
    )


# ---------------------------------------------------------------------------
# 4. dispatch: the rejection notifies the operator, naming the path.
# ---------------------------------------------------------------------------
def test_dispatch_rejection_notifies_with_offending_path(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin(tmp_path)
    story = _story(acceptance=[{"path": "../escape.txt", "source": "PWNED"}])
    notifications = []

    _dispatch(
        plan_dir, worktree_root, agents_dir, monkeypatch, "notify", "S1",
        repo, branch, story, notifications=notifications,
    )

    naming = [m for m in notifications if "../escape.txt" in str(m)]
    assert len(naming) == 1, (
        f"expected exactly one notification naming the rejected path, got "
        f"{notifications!r}"
    )
    assert "rejected" in str(naming[0]) or "outside the worktree" in str(naming[0])


# ---------------------------------------------------------------------------
# 5. ingest: an absolute acceptance path is rejected.
# ---------------------------------------------------------------------------
@pytest.fixture
def ingest_plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


def _ingest_story(**extra):
    story = {
        "summary": "Do the thing",
        "key": "S1",
        "agent_instructions": _PREFLIGHT + "\nImplement it.",
    }
    story.update(extra)
    return story


def _write_plan(plan_dir, plan_name, story, tmp_path):
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [story]}],
    }
    (plan_dir / f"{plan_name}.json").write_text(json.dumps(plan))


def test_ingest_rejects_absolute_acceptance_path(
    ingest_plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(
        ingest_plan_dir, "abs",
        _ingest_story(acceptance=[{"path": "/tmp/x.py", "source": "s"}]),
        tmp_path,
    )

    result = p.ingest_plan("abs")

    assert result["ok"] is False
    assert "acceptance" in result["error"]


def test_ingest_rejects_parent_traversal_acceptance_path(
    ingest_plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(
        ingest_plan_dir, "dotdot",
        _ingest_story(acceptance=[{"path": "a/../b.py", "source": "s"}]),
        tmp_path,
    )

    result = p.ingest_plan("dotdot")

    assert result["ok"] is False
    assert "acceptance" in result["error"]


def test_ingest_accepts_valid_acceptance_path(
    ingest_plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(
        ingest_plan_dir, "valid",
        _ingest_story(
            acceptance=[
                {"path": "tests/x.py", "source": "def test_x():\n    assert True\n"}
            ]
        ),
        tmp_path,
    )

    result = p.ingest_plan("valid")

    assert result["ok"] is True, result
