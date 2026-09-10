"""Tests for Mode 30 core: dispatch_story's worktree creation must never
mutate the shared main working tree's checked-out files. It fetches into
.git only, then starts the new branch directly from origin/<branch> - so
worktree currency no longer depends on the local default branch being kept
up to date (that used to be `_sync_local_default_branch`'s job, which this
change removes entirely).

Fixtures (`plan_dir`, `worktree_root`, `agents_dir`) are copied locally per
this repo's convention - there is no shared conftest.py.
"""
import inspect
import json
import logging
import subprocess
from pathlib import Path

import pytest

from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
)
from pipeline import concurrency as pcon
from pipeline import dispatch as pdisp
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import service as psvc
from pipeline import ticketing as pt


# ---------- Fixtures (copied from test_pipeline_mcp_server.py) ----------
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


def _write_manifest(plan_dir, plan_name, stories, repo_root=None):
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = str(repo_root)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


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


def _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra"):
    """Push a new commit to `origin` from a THIRD clone, so the `repo` made
    by `_make_origin_and_repo` never sees it locally - simulating another
    dispatch/merge landing on origin while this repo's local branch goes
    stale."""
    other = tmp_path / "other-clone"
    _run(["git", "clone", "-q", str(origin), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / name).write_text("more\n")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "more"], other)
    _run(["git", "push", "-q", "origin", branch], other)
    return _run(["git", "rev-parse", branch], other).stdout.strip()


def _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
              repo_root, branch, popen_calls=None):
    """Dispatch against a REAL repo_root (real git commands run - p.subprocess
    is not mocked). backend.subprocess IS the real, global subprocess module
    (a singleton import, not a copy), so faking its Popen unconditionally
    would also break the real git commands dispatch_story issues - only fake
    the actual `claude` CLI invocation, delegate everything else through."""
    _write_manifest(plan_dir, plan_name, {
        story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                    "status": "todo", "dependencies": []},
    }, repo_root=repo_root)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            if popen_calls is not None:
                popen_calls.append(cmd)
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    return p.dispatch_story(plan_name, story_key)


# ---------- Currency no longer depends on the local default branch ----------
def test_worktree_head_matches_origin_tip_when_local_default_branch_is_stale(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    stale_local_sha = _run(["git", "rev-parse", branch], repo).stdout.strip()
    origin_tip = _push_extra_commit_directly_to_origin(tmp_path, origin, branch)
    assert origin_tip != stale_local_sha

    result = _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch,
                        "stale", "S1", repo, branch)
    assert result["ok"] is True

    worktree_path = worktree_root / "S1"
    worktree_head = _run(["git", "rev-parse", "HEAD"], worktree_path).stdout.strip()
    assert worktree_head == origin_tip
    assert worktree_head != stale_local_sha
    # The local repo's own checked-out branch must be untouched (no pull
    # merged the new commit into it).
    local_head_after = _run(["git", "rev-parse", branch], repo).stdout.strip()
    assert local_head_after == stale_local_sha


def test_worktree_head_matches_origin_tip_when_local_default_branch_diverged(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Local default branch carries a commit origin never saw (e.g. manual
    local work). Dispatch must still start the worktree from origin's tip,
    not the diverged local tip - the dispatch path no longer cares about
    local default branch state at all."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    origin_tip = _run(["git", "rev-parse", branch], origin).stdout.strip()

    (repo / "local_only.txt").write_text("local work\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "local-only work"], repo)
    diverged_local_sha = _run(["git", "rev-parse", branch], repo).stdout.strip()
    assert diverged_local_sha != origin_tip

    result = _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch,
                        "diverged", "S1", repo, branch)
    assert result["ok"] is True

    worktree_head = _run(
        ["git", "rev-parse", "HEAD"], worktree_root / "S1").stdout.strip()
    assert worktree_head == origin_tip
    assert worktree_head != diverged_local_sha


# ---------- Command shape: fetch + worktree-add-from-origin, never pull ----------
def test_dispatch_git_commands_are_fetch_and_worktree_add_from_origin_no_pull(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "cmdshape", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("cmdshape", "S1")

    assert result["ok"] is True
    worktree_path = worktree_root / "S1"
    assert ["git", "fetch", "origin", "main"] in run_calls
    assert ["git", "worktree", "add", "-b", "agent/s1", str(worktree_path),
            "origin/main"] in run_calls
    assert not any(c[:2] == ["git", "pull"] for c in run_calls)


# ---------- _sync_local_default_branch is fully removed ----------
def test_sync_local_default_branch_function_no_longer_exists():
    assert not hasattr(p, "_sync_local_default_branch")


def test_sync_local_default_branch_call_site_removed_from_module_source():
    assert "_sync_local_default_branch" not in inspect.getsource(p)


# ---------- Resuming dispatch: no git ops, and (the prior regression) no raise ----------
def test_resuming_dispatch_with_interrupted_status_does_not_raise_or_touch_git(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "resume1", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "worktree": str(worktree_path)},
    })
    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(2))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("resume1", "S1")

    assert result == {**result, "ok": True}
    assert result["resumed"] is True
    assert not any(c[:2] == ["git", "fetch"] for c in run_calls)
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)


def test_resuming_dispatch_with_changes_requested_status_does_not_raise_or_touch_git(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "resume2", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "fix it"},
    })
    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(3))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("resume2", "S1")

    assert result["ok"] is True
    assert not any(c[:2] == ["git", "fetch"] for c in run_calls)
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)


# ---------- Negative / boundary cases ----------
def test_fetch_failure_returns_ok_false_and_never_creates_worktree(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """origin unreachable -> `git fetch` exits non-zero -> caught and
    surfaced as a structured {"ok": False, "error": ...} result instead of
    an uncaught CalledProcessError (which used to escape all the way to an
    unhandled 500 with an empty body - see pipeline/dispatch.py's
    _dispatch_story_impl). The worktree-add step (and the worktree
    directory) must still never be reached."""
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "f.txt").write_text("x\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", "/nonexistent/path/does/not/exist"], repo)

    _write_manifest(plan_dir, "fetchfail", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    }, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    result = p.dispatch_story("fetchfail", "S1")

    assert result["ok"] is False
    assert "git setup failed" in result["error"]
    assert not (worktree_root / "S1").exists()


def test_origin_branch_missing_returns_ok_false_rather_than_creating_stale_worktree(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """origin exists and is reachable but has no ref for the branch dispatch
    is told to fetch (a misconfigured default branch) -> the fetch for that
    specific ref fails -> returns {"ok": False, ...}, no worktree is
    silently created from whatever stale state happened to be on disk."""
    _origin, repo, _branch = _make_origin_and_repo(tmp_path, branch="main")

    _write_manifest(plan_dir, "badbranch", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    }, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: "does-not-exist-on-origin")
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    result = p.dispatch_story("badbranch", "S1")

    assert result["ok"] is False
    assert not (worktree_root / "S1").exists()


def test_worktree_add_failure_returns_ok_false_not_raise(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """fetch succeeds but `git worktree add` fails (branch name already
    exists) -> also caught by the same try/except as the fetch failure,
    returning {"ok": False, ...} rather than raising."""
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    # Pre-create the branch name dispatch_story will try to use, so
    # `git worktree add -b agent/s1 ...` collides ("branch already exists").
    _run(["git", "branch", "agent/s1", branch], repo)

    _write_manifest(plan_dir, "wtaddfail", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    }, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    result = p.dispatch_story("wtaddfail", "S1")

    assert result["ok"] is False
    assert "git setup failed" in result["error"]
    # The structured error must name the repo_root, the failing command and
    # its exit code, and carry git's stderr - the stderr detail is only
    # present when the subprocess.run calls capture output
    # (capture_output=True, text=True); without it e.stderr is None and the
    # detail is empty.
    assert f"repo_root {str(repo)!r}" in result["error"]
    assert "'git worktree add" in result["error"]
    # git's exit code for a failed `worktree add` varies by version (128 on
    # older gits, 255 on 2.55+); assert a nonzero exit is reported, not a
    # specific one.
    assert "exit 128" in result["error"] or "exit 255" in result["error"]
    assert "already exists" in result["error"]
    assert not (worktree_root / "S1").exists()


def test_advance_pipeline_real_git_fetch_failure_counts_attempt_and_notifies(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Integration: a REAL git fetch failure (unreachable origin, exactly
    the live-reproduced bug) reaching dispatch_story through a real
    advance_pipeline tick - not a mocked-raising dispatch_story - must
    still trigger the tick's existing attempt-counting/notify-user logic.
    Proves the ok:False-to-raise conversion in the tick loop (Change 2)
    actually wires up end to end, not just against a hand-written mock."""
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "f.txt").write_text("x\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", "/nonexistent/path/does/not/exist"], repo)

    _write_manifest(plan_dir, "realfail", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    }, repo_root=repo)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    notes: list[str] = []
    monkeypatch.setattr(p, "_notify_user",
        lambda plan, msg, **kwargs: notes.append(msg))

    result = p.advance_pipeline("realfail")

    manifest = json.loads((plan_dir / "realfail.manifest.json").read_text())
    story = manifest["stories"]["S1"]
    assert story["status"] == "todo"
    assert story["dispatch_attempts"] == 1
    # attempts=1 < DISPATCH_MAX_ATTEMPTS=3, so the tick's RETRY branch runs:
    # it never writes `dispatch_error` (only the give-up branch does), but it
    # does notify the user with str(e) - the re-raised RuntimeError text,
    # which carries dispatch_story's structured "git setup failed ..." error.
    assert "dispatch_error" not in story
    assert any("git setup failed" in m for m in notes), notes
    assert "S1" in result["notify"]
    assert not (worktree_root / "S1").exists()


# ---------- Outer try/except backstop in PipelineService.dispatch_story ----------
#
# DISPATCHGITFAIL-1 already gave _dispatch_story_impl (pipeline/dispatch.py) a
# git-specific handler for subprocess.CalledProcessError during worktree
# setup. This story layers an OUTER backstop in PipelineService.dispatch_story
# (pipeline/service.py) so ANY other exception comes back as a structured
# {"ok": False, "error": ...} instead of escaping as an unhandled 500 - with
# an explicit `except ValueError: raise` carve-out so _validate_key's
# deliberate rejection of malformed plan/story keys keeps propagating.

def test_dispatch_story_returns_ok_false_for_an_unexpected_non_git_exception(
    plan_dir, worktree_root, agents_dir, monkeypatch, caplog,
):
    """A failure unrelated to git setup (e.g. the live-reproduced
    FileNotFoundError from an invalid persona name deep in
    _build_dispatch_command) must also come back as a structured
    {"ok": False, ...} result, not an uncaught exception - this is the
    backstop for any failure DISPATCHGITFAIL-1's git-specific handler
    doesn't cover."""
    monkeypatch.setattr(
        p, "_dispatch_story_impl",
        lambda plan_name, story_key: (_ for _ in ()).throw(
            FileNotFoundError("No persona named backend-engineer at ...")
        ),
    )

    with caplog.at_level(logging.ERROR):
        result = p.dispatch_story("anyplan", "S1")

    assert result["ok"] is False
    assert "FileNotFoundError" in result["error"]
    # Exact documented shape: f"dispatch failed: {type(e).__name__}: {e}"
    assert result["error"] == (
        "dispatch failed: FileNotFoundError: No persona named backend-engineer at ..."
    )
    # The failure must be logged via logger.exception with plan/story context
    # (the "dispatch_story failed for plan=%s story=%s" record), not silently
    # swallowed.
    assert any(
        r.levelno == logging.ERROR
        and "dispatch_story failed" in r.getMessage()
        and "anyplan" in r.getMessage()
        and "S1" in r.getMessage()
        for r in caplog.records
    ), caplog.records


def test_dispatch_story_still_raises_valueerror_for_invalid_keys_after_backstop_added(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The broad Exception catch-all must not swallow _validate_key's
    deliberate ValueError for a malformed key - it should keep propagating
    exactly as it did before this story's change."""
    with pytest.raises(ValueError, match="invalid"):
        p.dispatch_story("../evil", "S1")


def test_dispatch_story_backstop_plumbing_lives_in_service_module():
    """The backstop's plumbing must be added to pipeline/service.py itself:
    an `import logging` alongside the existing stdlib imports, a module-level
    `logger = logging.getLogger(__name__)`, and a dispatch_story body that
    re-raises ValueError before the broad Exception handler. DISPATCHGITFAIL-1's
    git-specific handler must still exist in pipeline/dispatch.py (membership
    check only - later stories may extend that module)."""
    service_src = Path(psvc.__file__).read_text()
    assert "import logging" in service_src
    assert "logger = logging.getLogger(__name__)" in service_src
    assert "except ValueError" in service_src
    assert "except Exception as e" in service_src
    assert "logger.exception(" in service_src
    assert 'f"dispatch failed: {type(e).__name__}: {e}"' in service_src

    dispatch_src = Path(pdisp.__file__).read_text()
    assert "except subprocess.CalledProcessError" in dispatch_src
    assert "git setup failed" in dispatch_src
