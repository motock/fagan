"""The merge gate must see the worktree the review approved, not a local edit.

``review_merge_binding.check`` binds an ``APPROVE`` verdict to the sha the
reviewer graded. That binding is only meaningful if the commit that is merged
is the commit that was reviewed - and the review happens on the *remote* tip,
so a commit that exists only in the story worktree (a wall-clock-timeout WIP
commit, an agent's local fix) is invisible to it. The same is true of an
uncommitted edit: it is not in the remote tip either, but the merge gate's
pre-rebase checkpoint would happily commit and publish it.

``worktree_head_state(worktree)`` probes the local checkout
(``git rev-parse --verify HEAD`` plus ``git status --porcelain
--untracked-files=no``); ``_check_with_worktree(story, branch_head_sha,
worktree)`` combines that local evidence with the remote binding check. A
missing worktree falls back to the remote-only check, so a story whose
checkout is gone still merges on its remote binding.

Real git runs here (throwaway repos under ``tmp_path``): the seam that matters
is the real ``git status`` output, not a fake of it.
"""

import json
import os
import subprocess

import pytest

import pipeline.ci as _pipeline_ci
import pipeline.server  # noqa: F401 - pre-existing import cycle, import first
from pipeline import review_merge_binding
from pipeline.review_merge_binding import check

# The real probe, captured at import time: the suite-wide fixture stubs the
# module attribute with a no-op, and these tests need the genuine git calls.
# The new names are reached through the module (not a module-level
# ``from ... import``) so that a bare ``pytest --collect-only`` still collects
# this file while the implementation is missing.
_REAL_WORKTREE_HEAD_STATE = getattr(review_merge_binding, "worktree_head_state", None)


def _check_with_worktree(story, branch_head_sha, worktree):
    """``check_with_worktree`` through the module attribute."""
    return review_merge_binding.check_with_worktree(story, branch_head_sha, worktree)


def _worktree_head_state(worktree):
    """``worktree_head_state`` through the module attribute."""
    return review_merge_binding.worktree_head_state(worktree)

PLAN = "go"
KEY = "P1"


@pytest.fixture(autouse=True)
def _real_local_probe(monkeypatch):
    """Restore the genuine local probe the suite-wide fixture stubs out.

    The suite-wide fixture in ``tests/unit/conftest.py`` replaces the local
    probe with a no-op so the existing merge-path tests (which drive the gate
    over fake worktrees) keep passing. These tests need the REAL probe, so
    the genuine function - captured at import time, before any fixture ran -
    is put back.
    """
    if _REAL_WORKTREE_HEAD_STATE is None:
        # The implementation does not exist yet: let the call site raise the
        # AttributeError rather than the fixture, so the failure names the
        # missing function.
        return
    monkeypatch.setattr(
        review_merge_binding, "worktree_head_state", _REAL_WORKTREE_HEAD_STATE
    )


# ---------------------------------------------------------------------------
# Real-git fixtures: a repo, a reviewed commit, and a worktree checked out
# from it.
# ---------------------------------------------------------------------------
def _git(repo, *args):
    """Run git in ``repo``; raise on failure (fixture setup only).

    The identity and the global/system config are pinned so the throwaway
    repo never depends on (or writes to) the developer's git configuration.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Local Head Test",
            "GIT_AUTHOR_EMAIL": "local-head@example.test",
            "GIT_COMMITTER_NAME": "Local Head Test",
            "GIT_COMMITTER_EMAIL": "local-head@example.test",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def _init_repo(tmp_path, name="repo"):
    """A throwaway repo with one commit on ``master``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/master")
    (repo / "app.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _sha(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _approved(sha, **overrides):
    story = {"review_verdict": "APPROVE", "review_verdict_sha": sha}
    story.update(overrides)
    return story


# ---------------------------------------------------------------------------
# worktree_head_state - the local probe.
# ---------------------------------------------------------------------------
def test_head_state_reports_a_clean_worktree(tmp_path):
    repo = _init_repo(tmp_path)

    head, dirty = _worktree_head_state(str(repo))

    assert head == _sha(repo)
    assert dirty is False


def test_head_state_reports_a_modified_tracked_file_as_dirty(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "app.txt").write_text("edited\n")

    head, dirty = _worktree_head_state(str(repo))

    assert head == _sha(repo)
    assert dirty is True


def test_head_state_ignores_a_modified_agent_log(tmp_path):
    """``agent.log`` is the pipeline's own scratch file, not story content."""
    repo = _init_repo(tmp_path)
    (repo / "agent.log").write_text("noise\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add agent log")
    (repo / "agent.log").write_text("more noise\n")

    head, dirty = _worktree_head_state(str(repo))

    assert head == _sha(repo)
    assert dirty is False


def test_head_state_ignores_an_untracked_file(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "scratch.txt").write_text("untracked\n")

    head, dirty = _worktree_head_state(str(repo))

    assert head == _sha(repo)
    assert dirty is False


def test_head_state_of_a_missing_path_is_no_head_and_clean(tmp_path):
    missing = tmp_path / "not-a-worktree"

    assert _worktree_head_state(str(missing)) == (None, False)


def test_head_state_of_a_non_git_directory_is_no_head_and_clean(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert _worktree_head_state(str(plain)) == (None, False)


def test_head_state_fails_closed_when_git_status_fails(tmp_path, monkeypatch):
    """A status that cannot be read is treated as dirty, never as clean."""
    repo = _init_repo(tmp_path)
    real_git = review_merge_binding._git

    def _status_fails(args, cwd):
        if "status" in list(args):
            return subprocess.CompletedProcess(
                ["git", *args], 1, stdout="", stderr="boom"
            )
        return real_git(args, cwd)

    monkeypatch.setattr(review_merge_binding, "_git", _status_fails)

    head, dirty = _worktree_head_state(str(repo))

    assert head == _sha(repo)
    assert dirty is True


# ---------------------------------------------------------------------------
# check_with_worktree - the local probe folded into the merge gate.
# ---------------------------------------------------------------------------
def test_a_clean_worktree_at_the_reviewed_sha_is_allowed(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _sha(repo)

    result = _check_with_worktree(_approved(sha), sha, str(repo))

    assert result["ok"] is True


def test_a_local_commit_beyond_the_reviewed_sha_is_refused(tmp_path):
    """The review graded the remote tip; a local-only commit was never seen."""
    repo = _init_repo(tmp_path)
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("local only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip: wall clock timeout")
    local = _sha(repo)
    assert local != reviewed

    result = _check_with_worktree(_approved(reviewed), reviewed, str(repo))

    assert result["ok"] is False
    assert reviewed[:12] in result["reason"]
    assert local[:12] in result["reason"]
    assert "re-run review_story" in result["reason"]


def test_a_local_commit_is_refused_even_when_the_worktree_is_also_dirty(tmp_path):
    """The sha mismatch is the stronger evidence, so it names both shas."""
    repo = _init_repo(tmp_path)
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("local only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip: wall clock timeout")
    local = _sha(repo)
    (repo / "other.txt").write_text("uncommitted\n")

    result = _check_with_worktree(_approved(reviewed), reviewed, str(repo))

    assert result["ok"] is False
    assert "local HEAD" in result["reason"]
    assert reviewed[:12] in result["reason"]
    assert local[:12] in result["reason"]
    assert "uncommitted changes" not in result["reason"]


def test_the_probe_caches_nothing_across_calls(tmp_path):
    """A worktree reset back to the reviewed sha is allowed again."""
    repo = _init_repo(tmp_path)
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("local only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip: wall clock timeout")
    assert _check_with_worktree(_approved(reviewed), reviewed, str(repo))["ok"] is False

    _git(repo, "reset", "--hard", reviewed)

    result = _check_with_worktree(_approved(reviewed), reviewed, str(repo))

    assert result["ok"] is True


def test_an_uncommitted_edit_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _sha(repo)
    (repo / "app.txt").write_text("uncommitted\n")

    result = _check_with_worktree(_approved(sha), sha, str(repo))

    assert result["ok"] is False
    assert "uncommitted changes" in result["reason"]
    assert "re-run review_story" in result["reason"]


def test_a_modified_agent_log_alone_is_allowed(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "agent.log").write_text("noise\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add agent log")
    sha = _sha(repo)
    (repo / "agent.log").write_text("more noise\n")

    result = _check_with_worktree(_approved(sha), sha, str(repo))

    assert result["ok"] is True


def test_an_untracked_file_alone_is_allowed(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _sha(repo)
    (repo / "scratch.txt").write_text("untracked\n")

    result = _check_with_worktree(_approved(sha), sha, str(repo))

    assert result["ok"] is True


@pytest.mark.parametrize("worktree", ["", None, 12345])
def test_an_unusable_worktree_falls_back_to_the_remote_check(worktree):
    story = _approved("abc123")

    result = _check_with_worktree(story, "abc123", worktree)

    assert result == check(story, "abc123")
    assert result["ok"] is True


def test_a_missing_worktree_falls_back_to_the_remote_check(tmp_path):
    story = _approved("abc123")
    missing = str(tmp_path / "gone")

    result = _check_with_worktree(story, "abc123", missing)

    assert result == check(story, "abc123")
    assert result["ok"] is True


def test_a_non_git_worktree_falls_back_to_the_remote_check(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    story = _approved("abc123")

    result = _check_with_worktree(story, "abc123", str(plain))

    assert result == check(story, "abc123")
    assert result["ok"] is True


def test_a_remote_tip_the_review_does_not_cover_is_the_plain_denial(tmp_path):
    """The remote binding is checked first: a stale approval is denied as
    before and the local probe never rescues it."""
    repo = _init_repo(tmp_path)
    story = _approved("abc123")

    result = _check_with_worktree(story, "deadbeef", str(repo))

    assert result == check(story, "deadbeef")
    assert result["ok"] is False


def test_a_remote_denial_never_consults_the_local_probe(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    story = _approved("abc123")

    def _explode(worktree):
        raise AssertionError("the local probe must not run after a remote denial")

    monkeypatch.setattr(review_merge_binding, "worktree_head_state", _explode)

    result = _check_with_worktree(story, "deadbeef", str(repo))

    assert result == check(story, "deadbeef")
    assert "local HEAD" not in result["reason"]


def test_a_stale_remote_denial_wins_over_a_clean_local_head(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _sha(repo)
    story = _approved(sha)

    result = _check_with_worktree(story, "deadbeef", str(repo))

    assert result["ok"] is False
    assert "abc123" not in result["reason"]
    assert "deadbeef" in result["reason"]


def test_a_failed_status_is_refused_as_uncommitted(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    sha = _sha(repo)
    real_git = review_merge_binding._git

    def _status_fails(args, cwd):
        if "status" in list(args):
            return subprocess.CompletedProcess(
                ["git", *args], 1, stdout="", stderr="boom"
            )
        return real_git(args, cwd)

    monkeypatch.setattr(review_merge_binding, "_git", _status_fails)

    result = _check_with_worktree(_approved(sha), sha, str(repo))

    assert result["ok"] is False
    assert "uncommitted changes" in result["reason"]
    assert "re-run review_story" in result["reason"]


def test_check_with_worktree_does_not_mutate_the_story(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _sha(repo)
    story = _approved(sha)

    _check_with_worktree(story, sha, str(repo))

    assert story == {"review_verdict": "APPROVE", "review_verdict_sha": sha}


# ---------------------------------------------------------------------------
# The merge gate wiring: approve_merge refuses and never rebases.
# ---------------------------------------------------------------------------
def _write_manifest(plan_dir, story):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {KEY: story}}, indent=2)
    )


def _read_story(plan_dir):
    return json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())["stories"][KEY]


class _Recorder:
    """Callable that records its calls and returns a fixed value."""

    def __init__(self, value=None):
        self.value = value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.value

    @property
    def count(self):
        return len(self.calls)


def _install_approve_merge_seams(monkeypatch, head, pushed="deadbeefcafe99"):
    import contextlib

    import pipeline.authorization as pa
    import pipeline.server as p
    from pipeline import merge

    seams = {
        "rebase": _Recorder({"ok": True, "error": "", "auto_resolved": False}),
        "merge_pr": _Recorder(None),
        "notify": _Recorder(None),
    }
    monkeypatch.setattr(pa, "authorize", lambda *a, **k: None)
    monkeypatch.setattr(p, "_scoped_repo_root", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(p, "_rebase_onto_master", seams["rebase"])
    monkeypatch.setattr(p, "_merge_pr", seams["merge_pr"])
    monkeypatch.setattr(p, "_notify_user", seams["notify"])
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])
    monkeypatch.setattr(merge, "_maybe_record_retro", lambda *a, **k: None)
    monkeypatch.setattr(_pipeline_ci, "_ci_status", lambda *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(
        _pipeline_ci, "_reverify_acceptance", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        _pipeline_ci, "_reverify_build", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        review_merge_binding, "branch_head_sha", lambda branch, **kw: head
    )
    # The merge gate's own push/rev-parse are faked; every other git call
    # (the local worktree probe, the branch resolver) runs for real.
    real_run = subprocess.run

    def _fake_run(cmd, *a, **k):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else []
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{pushed}\n", stderr=""
            )
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(merge.subprocess, "run", _fake_run)
    return seams


def test_approve_merge_refuses_a_local_commit_the_review_never_saw(
    plan_dir, tmp_path, monkeypatch
):
    from pipeline import merge

    repo = _init_repo(tmp_path, "story-worktree")
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("local only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip: wall clock timeout")
    local = _sha(repo)

    _write_manifest(
        plan_dir,
        {
            "summary": "local commit beyond the review",
            "status": "pr_open",
            "review_verdict": "APPROVE",
            "review_verdict_sha": reviewed,
            "worktree": str(repo),
            "pr_url": "https://example.invalid/pr/1",
            "risk": "low",
        },
    )
    seams = _install_approve_merge_seams(monkeypatch, reviewed)

    result = merge._approve_merge_impl(PLAN, KEY)

    assert result["ok"] is False
    assert reviewed[:12] in result["error"]
    assert local[:12] in result["error"]
    assert "review_story" in result["error"]
    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0


def test_approve_merge_refuses_an_uncommitted_worktree_edit(
    plan_dir, tmp_path, monkeypatch
):
    from pipeline import merge

    repo = _init_repo(tmp_path, "story-worktree")
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("uncommitted\n")

    _write_manifest(
        plan_dir,
        {
            "summary": "uncommitted edit",
            "status": "pr_open",
            "review_verdict": "APPROVE",
            "review_verdict_sha": reviewed,
            "worktree": str(repo),
            "pr_url": "https://example.invalid/pr/1",
            "risk": "low",
        },
    )
    seams = _install_approve_merge_seams(monkeypatch, reviewed)

    result = merge._approve_merge_impl(PLAN, KEY)

    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]
    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0


def test_approve_merge_allows_a_clean_worktree_at_the_reviewed_sha(
    plan_dir, tmp_path, monkeypatch
):
    from pipeline import merge

    repo = _init_repo(tmp_path, "story-worktree")
    reviewed = _sha(repo)

    _write_manifest(
        plan_dir,
        {
            "summary": "clean worktree",
            "status": "pr_open",
            "review_verdict": "APPROVE",
            "review_verdict_sha": reviewed,
            "worktree": str(repo),
            "pr_url": "https://example.invalid/pr/1",
            "risk": "low",
        },
    )
    seams = _install_approve_merge_seams(monkeypatch, reviewed)

    result = merge._approve_merge_impl(PLAN, KEY)

    assert result["ok"] is True
    assert seams["rebase"].count == 1
    assert seams["merge_pr"].count == 1, result


# ---------------------------------------------------------------------------
# The scheduler path parks the story with the binding-denial reason.
# ---------------------------------------------------------------------------
def _install_scheduler_seams(monkeypatch, head):
    import pipeline.server as p

    seams = {
        "rebase": _Recorder(("", "deadbeefcafe99")),
        "merge_pr": _Recorder(None),
        "notify": _Recorder(None),
    }
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(
        p, "_rebase_and_push_for_merge", lambda *a, **k: seams["rebase"](*a, **k)
    )
    monkeypatch.setattr(p, "_merge_pr", seams["merge_pr"])
    monkeypatch.setattr(p, "_notify_user", seams["notify"])
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])
    monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_merge_gate_ci_status", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        _pipeline_ci, "_reverify_acceptance", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        _pipeline_ci, "_reverify_build", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p, "_merge_decision", lambda story: {"action": "merge", "reason": ""}
    )
    monkeypatch.setattr(
        review_merge_binding, "branch_head_sha", lambda branch, **kw: head
    )
    return seams


def _summary():
    return {"parked": [], "notify": [], "merged": [], "failed": [], "ci_pending": []}


def test_the_scheduler_parks_a_story_with_a_local_commit_beyond_the_review(
    plan_dir, tmp_path, monkeypatch
):
    from pipeline import advance_merge

    repo = _init_repo(tmp_path, "story-worktree")
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("local only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip: wall clock timeout")
    local = _sha(repo)

    _write_manifest(
        plan_dir,
        {
            "summary": "local commit beyond the review",
            "status": "pr_open",
            "review_verdict": "APPROVE",
            "review_verdict_sha": reviewed,
            "worktree": str(repo),
            "pr_url": "https://example.invalid/pr/1",
            "risk": "low",
        },
    )
    seams = _install_scheduler_seams(monkeypatch, reviewed)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["status"] == "parked"
    assert "local HEAD" in story["parked_reason"]
    assert reviewed[:12] in story["parked_reason"]
    assert local[:12] in story["parked_reason"]
    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0


def test_the_scheduler_parks_a_story_with_an_uncommitted_worktree_edit(
    plan_dir, tmp_path, monkeypatch
):
    from pipeline import advance_merge

    repo = _init_repo(tmp_path, "story-worktree")
    reviewed = _sha(repo)
    (repo / "app.txt").write_text("uncommitted\n")

    _write_manifest(
        plan_dir,
        {
            "summary": "uncommitted edit",
            "status": "pr_open",
            "review_verdict": "APPROVE",
            "review_verdict_sha": reviewed,
            "worktree": str(repo),
            "pr_url": "https://example.invalid/pr/1",
            "risk": "low",
        },
    )
    seams = _install_scheduler_seams(monkeypatch, reviewed)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["status"] == "parked"
    assert "uncommitted changes" in story["parked_reason"]
    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0


# ---------------------------------------------------------------------------
# The gate must not log story content while refusing.
# ---------------------------------------------------------------------------
def test_a_refusal_logs_no_story_content(tmp_path, caplog):
    """Identifiers and outcomes only: no diff, no file body, no token."""
    repo = _init_repo(tmp_path)
    sha = _sha(repo)
    (repo / "app.txt").write_text("secret-body-marker\n")

    with caplog.at_level("DEBUG"):
        result = _check_with_worktree(_approved(sha), sha, str(repo))

    assert result["ok"] is False
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-body-marker" not in blob
    assert "app.txt" not in blob
