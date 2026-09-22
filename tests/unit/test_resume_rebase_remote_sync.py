"""Tests for the resume-time remote sync after a rebase (story OA2-01).

`pipeline/dispatch.py`'s resumed-dispatch path rebases a story worktree onto
origin/<default> and never pushes, while every other rebase site in the harness
pushes immediately after (pipeline/merge.py, pipeline/pr.py). The rebase
rewrites the branch's SHAs but origin/agent/<story> keeps the pre-rebase
history, so the resumed agent's own push is rejected as non-fast-forward and the
agent recovers by merging the stale remote back in (commit 4c0ea9b on
agent/oa2-01). Fix: force-push the branch after a successful resume rebase so
the agent's own push is a fast-forward.

Harnesses are copied per this repo's convention (there is no shared conftest.py
for these): the dispatch-path harness mirrors `_resume_rebase_harness` in
tests/unit/test_pipeline_mcp_server_fresh_rework.py; the real-git harness
mirrors the bare-origin setup in tests/unit/test_dispatch_staleness.py.
"""
import subprocess
from pathlib import Path

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import rebase as prebase
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline.rebase import _sync_branch_remote
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_PY = REPO_ROOT / "pipeline" / "dispatch.py"


def _norm(path):
    """Whitespace-normalized source (quote characters preserved)."""
    return " ".join(path.read_text().split())


def _norm_unquoted(path):
    """Whitespace-normalized source with quote characters removed, so a string
    literal split across source lines can be matched as one phrase."""
    return " ".join(path.read_text().replace('"', " ").split())


# ---------- Local copy of the plan_dir / worktree_root / agents_dir fixtures
# (tests/unit/_pipeline_mcp_server_test_helpers.py). Built by a helper rather
# than imported as same-named pytest fixtures so the harness parameters below
# do not shadow module-level names (ruff F811).
def _dispatch_env(tmp_path, monkeypatch):
    """Point PLAN_DIR / WORKTREE_ROOT / AGENTS_DIR at tmp dirs. Returns
    (plan_dir, worktree_root, agents_dir)."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(pcon, "PLAN_DIR", plans)

    wts = tmp_path / "worktrees"
    wts.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", wts)

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (agents / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (agents / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (agents / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", agents)
    monkeypatch.setattr(pper, "AGENTS_DIR", agents)
    return plans, wts, agents


# ---------- Dispatch-path harness (copied from
# tests/unit/test_pipeline_mcp_server_fresh_rework.py) ----------
def _resume_rebase_harness(monkeypatch, plans, wts, behind,
                           rebase_result, plan_name="rrb", story_key="S1"):
    """Set up a resumed dispatch whose worktree base is `behind` commits behind
    origin/<default>. Mocks at the true external boundary: subprocess.run (to
    control the `git rev-list --count` behind-count) and `_rebase_onto_master`
    (to return canned dicts). Returns (worktree_path, notes, rebase_calls)."""
    _write_manifest(plans, plan_name, {
        story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                    "status": "interrupted", "dependencies": []},
    })
    wt = wts / story_key
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /fake\n")

    notes = []
    rebase_calls = []

    def _fake_run(cmd, cwd=None, **kwargs):
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, str(behind), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        p, "_rebase_onto_master",
        lambda wt_arg, br: rebase_calls.append((wt_arg, br)) or rebase_result,
    )
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(777))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: notes.append(msg))
    return wt, notes, rebase_calls


def _record_sync(monkeypatch, sync_result):
    """Patch p._sync_branch_remote (observed through dispatch's _ServerRef) with
    a recorder. Returns the list of (worktree, branch) call args."""
    calls = []

    def _recorder(wt_arg, br):
        calls.append((wt_arg, br))
        return sync_result

    monkeypatch.setattr(p, "_sync_branch_remote", _recorder)
    return calls


_REBASE_OK = {"ok": True, "conflict": False, "error": "", "auto_resolved": False}


# ---------- Behavior 1: WIRING ----------
def test_resume_rebase_ok_syncs_branch_remote_once(tmp_path, monkeypatch):
    """WIRING: rebase ok=True -> dispatch_story calls _sync_branch_remote exactly
    once with (worktree_path, branch) == (wt, "agent/s1"), and dispatch proceeds
    (pid present, story IN_PROGRESS)."""
    plans, wts, _agents = _dispatch_env(tmp_path, monkeypatch)
    wt, _notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plans, wts, behind=3, rebase_result=_REBASE_OK,
    )
    sync_calls = _record_sync(monkeypatch, {"ok": True, "error": ""})

    result = p.dispatch_story("rrb", "S1")

    assert rebase_calls == [(wt, "agent/s1")]
    assert sync_calls == [(wt, "agent/s1")]
    assert result["pid"] == 777
    story = _read_manifest(plans, "rrb")["stories"]["S1"]
    assert story["status"] == "in_progress"


# ---------- Behavior 2: NEGATIVE CONTROL ----------
def test_resume_rebase_conflict_parks_without_sync(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: a rebase conflict parks the story and
    _sync_branch_remote is NOT called (the park returns first)."""
    plans, wts, _agents = _dispatch_env(tmp_path, monkeypatch)
    _wt, _notes, _rebase_calls = _resume_rebase_harness(
        monkeypatch, plans, wts, behind=3,
        rebase_result={"ok": False, "conflict": True,
                       "error": "non-additive conflict in src/app.py",
                       "auto_resolved": False},
    )
    sync_calls = _record_sync(monkeypatch, {"ok": True, "error": ""})

    result = p.dispatch_story("rrb", "S1")

    story = _read_manifest(plans, "rrb")["stories"]["S1"]
    assert story["status"] == "parked"
    assert sync_calls == []
    assert "pid" not in result


# ---------- Behavior 3: FAIL-OPEN ----------
def test_resume_rebase_sync_failure_fails_open(tmp_path, monkeypatch):
    """FAIL-OPEN: rebase ok=True but _sync_branch_remote returns ok=False ->
    dispatch still proceeds (pid present, IN_PROGRESS). The sync must never gate
    dispatch."""
    plans, wts, _agents = _dispatch_env(tmp_path, monkeypatch)
    wt, _notes, _rebase_calls = _resume_rebase_harness(
        monkeypatch, plans, wts, behind=3, rebase_result=_REBASE_OK,
    )
    sync_calls = _record_sync(monkeypatch, {"ok": False, "error": "boom"})

    result = p.dispatch_story("rrb", "S1")

    assert sync_calls == [(wt, "agent/s1")]
    assert result["pid"] == 777
    story = _read_manifest(plans, "rrb")["stories"]["S1"]
    assert story["status"] == "in_progress"


# ---------- "Called only after a successful rebase" boundaries ----------
def test_resume_behind_zero_does_not_sync(tmp_path, monkeypatch):
    """behind == 0: no rebase happens, so no remote sync either."""
    plans, wts, _agents = _dispatch_env(tmp_path, monkeypatch)
    _wt, _notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plans, wts, behind=0, rebase_result=_REBASE_OK,
    )
    sync_calls = _record_sync(monkeypatch, {"ok": True, "error": ""})

    result = p.dispatch_story("rrb", "S1")

    assert rebase_calls == []
    assert sync_calls == []
    assert result["pid"] == 777


def test_resume_rebase_other_failure_does_not_sync(tmp_path, monkeypatch):
    """rebase ok=False, conflict=False (other git/infra failure): fail open and
    proceed, but the rebase did NOT succeed so no remote sync is attempted."""
    plans, wts, _agents = _dispatch_env(tmp_path, monkeypatch)
    _wt, _notes, _rebase_calls = _resume_rebase_harness(
        monkeypatch, plans, wts, behind=3,
        rebase_result={"ok": False, "conflict": False, "error": "fetch failed",
                       "auto_resolved": False},
    )
    sync_calls = _record_sync(monkeypatch, {"ok": True, "error": ""})

    result = p.dispatch_story("rrb", "S1")

    assert sync_calls == []
    assert result["pid"] == 777


# ---------- Real-git harness (copied from tests/unit/test_dispatch_staleness.py) ----------
def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_origin_and_repo(tmp_path, branch="main"):
    """Bare `origin` + a real local clone `repo`, both on `branch`, one commit
    deep. Returns (origin, repo, branch)."""
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


def _make_story_worktree(tmp_path, repo, story_key="S1"):
    """A REAL git worktree on agent/<story_key> off repo's current HEAD.
    Returns (worktree_path, branch)."""
    wt = tmp_path / "worktrees" / story_key
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = f"agent/{story_key.lower()}"
    _run(["git", "worktree", "add", "-q", "-b", branch, str(wt)], repo)
    return wt, branch


def _commit_in(wt, name, msg):
    (wt / name).write_text("x\n")
    _run(["git", "add", "-A"], wt)
    _run(["git", "commit", "-qm", msg], wt)


def _move_origin_master(tmp_path, origin, repo, branch="main"):
    """Push a new commit to origin/<branch> from a THIRD clone and fetch it into
    repo, so repo's origin/<branch> moves past the worktree's base."""
    other = tmp_path / "other-clone"
    _run(["git", "clone", "-q", str(origin), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / "moved.txt").write_text("moved\n")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "moved"], other)
    _run(["git", "push", "-q", "origin", branch], other)
    _run(["git", "fetch", "-q", "origin"], repo)


def _remote_sha(wt, branch):
    out = _run(["git", "ls-remote", "origin", branch], wt).stdout.split()
    return out[0] if out else ""


# ---------- Behavior 4: REAL GIT, the actual bug ----------
def test_sync_branch_remote_force_pushes_rewritten_history(tmp_path):
    """REAL GIT, the actual bug: the branch is already on origin with the
    pre-rebase SHAs; after the local branch is rebased onto a moved master its
    SHAs differ, and _sync_branch_remote must make origin/<branch> equal the
    worktree HEAD (so the resumed agent's own push is a fast-forward)."""
    origin, repo, default = _make_origin_and_repo(tmp_path)
    wt, branch = _make_story_worktree(tmp_path, repo)
    _commit_in(wt, "story.txt", "story work")
    _run(["git", "push", "-q", "-u", "origin", branch], wt)
    remote_before = _remote_sha(wt, branch)

    _move_origin_master(tmp_path, origin, repo, default)
    _run(["git", "rebase", "-q", f"origin/{default}"], wt)
    head = _run(["git", "rev-parse", "HEAD"], wt).stdout.strip()
    assert head != remote_before  # the rebase rewrote the branch's SHAs

    result = _sync_branch_remote(str(wt), branch)

    assert result["ok"] is True
    assert _remote_sha(wt, branch) == head


# ---------- Behavior 5: REAL GIT, absent remote branch ----------
def test_sync_branch_remote_creates_absent_remote_branch(tmp_path):
    """REAL GIT: the branch was never pushed -> the call returns ok=True and the
    remote branch now exists at the local HEAD."""
    _origin, repo, _default = _make_origin_and_repo(tmp_path)
    wt, branch = _make_story_worktree(tmp_path, repo)
    _commit_in(wt, "story.txt", "story work")
    assert _remote_sha(wt, branch) == ""  # never pushed
    head = _run(["git", "rev-parse", "HEAD"], wt).stdout.strip()

    result = _sync_branch_remote(str(wt), branch)

    assert result["ok"] is True
    assert _remote_sha(wt, branch) == head


# ---------- Helper contract: never raises, {"ok": bool, "error": str} ----------
def test_sync_branch_remote_returns_ok_and_error_keys(tmp_path):
    """The return contract is exactly {"ok": bool, "error": str}."""
    _origin, repo, _default = _make_origin_and_repo(tmp_path)
    wt, branch = _make_story_worktree(tmp_path, repo)
    _commit_in(wt, "story.txt", "story work")

    result = _sync_branch_remote(str(wt), branch)

    assert set(result) == {"ok", "error"}
    assert isinstance(result["ok"], bool)
    assert isinstance(result["error"], str)


def test_sync_branch_remote_reports_push_failure_without_raising(tmp_path):
    """A push that cannot succeed returns ok=False with a non-empty error string
    and never raises (the caller fail-opens on it)."""
    _origin, repo, _default = _make_origin_and_repo(tmp_path)
    wt, branch = _make_story_worktree(tmp_path, repo)
    _commit_in(wt, "story.txt", "story work")
    _run(["git", "remote", "set-url", "origin", str(tmp_path / "nope.git")], wt)

    result = _sync_branch_remote(str(wt), branch)

    assert result["ok"] is False
    assert isinstance(result["error"], str)
    assert result["error"]


def test_sync_branch_remote_returns_error_on_oserror(tmp_path, monkeypatch):
    """An OSError out of subprocess.run (git missing) is caught and reported as
    {"ok": False, "error": <message>} - the helper never raises."""
    def _boom(*a, **kw):
        raise OSError("git not found")

    monkeypatch.setattr(prebase.subprocess, "run", _boom)

    result = _sync_branch_remote(str(tmp_path), "agent/s1")

    assert result["ok"] is False
    assert "git not found" in result["error"]


# ---------- Structural wiring requirements ----------
def test_rebase_all_exports_sync_branch_remote_after_rebase_onto_master():
    """pipeline/rebase.py must list "_sync_branch_remote" in __all__ immediately
    after the "_rebase_onto_master" line (ordering relative to a fixed anchor;
    later stories may append more names)."""
    assert "_sync_branch_remote" in prebase.__all__
    assert (prebase.__all__.index("_sync_branch_remote")
            == prebase.__all__.index("_rebase_onto_master") + 1)


def test_server_reexports_sync_branch_remote():
    """pipeline/server.py must re-export the helper so p.<name> patching lands."""
    assert p._sync_branch_remote is prebase._sync_branch_remote


def test_dispatch_binds_sync_branch_remote_via_server_ref():
    """pipeline/dispatch.py must bind the helper through a _ServerRef."""
    assert ('_sync_branch_remote = _ServerRef("_sync_branch_remote")'
            in _norm(DISPATCH_PY))


def test_dispatch_call_site_is_inside_rebase_ok_branch_after_info_log():
    """The sync call must sit inside the `if result["ok"]:` branch, immediately
    after the rebase info log and before the `elif result["conflict"]:` park."""
    src = _norm(DISPATCH_PY)
    ok_idx = src.index('if result["ok"]:')
    conflict_idx = src.index('elif result["conflict"]:')
    info_idx = src.index("story %s worktree base predated")
    call_idx = src.index("_sync_branch_remote(worktree_path, branch)")

    assert ok_idx < info_idx < call_idx < conflict_idx


def test_dispatch_warns_fail_open_when_sync_fails():
    """A failed sync logs a fail-open warning naming the story and the error."""
    src = _norm_unquoted(DISPATCH_PY)
    assert ("remote sync for resumed story %s failed; dispatching anyway "
            "(fail open): %s") in src
