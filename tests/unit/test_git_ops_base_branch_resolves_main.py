"""``pipeline.git_ops.resolve_base`` — make the dispatch worktree resolve ``main``.

Story ``ST-01-repo-issue``. Live failure: a dispatched worktree had no local
``main`` ref (its repo only ever knew ``master``, while ``_default_branch()``
answered ``main``), so the new-commits verification step could compare nothing
and rendered::

    BASE BRANCH main UNRESOLVED in this worktree; new-commits check INCONCLUSIVE

Triage then could not corroborate the branch's commits, ST-01 was held parked as
unmerged, and its green 18162-test/ruff-clean implementation was never merged.
The defect is environmental: the base branch is resolvable from ``origin/main``
but nothing materialised it locally, and nothing told the caller "unresolved"
explicitly enough to distinguish "no idea" from "no commits".

Contract pinned here (real throwaway repos under ``tmp_path``; no existing test
file is edited — these are additive, and they are written FIRST):

(a) local ``main`` absent but ``origin/main`` present -> the origin sha is
    returned (never ``HEAD``) and a local ``main`` ref is created at that sha,
    so a later ``git rev-parse main`` in the worktree works;
(b) the materialisation happens ONCE: after three commits land on the agent
    branch the base must NOT have moved, so the new-commit count is 3, not 0.
    ``git branch -f main HEAD`` / ``git checkout -B main`` breaks this: the base
    silently becomes the tip, the diff is empty, and the story is re-failed as
    "no new commits";
(c) neither local ``main`` nor ``origin/main`` resolves -> the explicit
    unresolved signal ``None`` (the caller reports INCONCLUSIVE), and never a
    ``HEAD`` fallback. Nothing is cached: once ``git fetch`` brings
    ``origin/main``, a second call resolves it and the count is 3. A module-level
    memo of the ``HEAD`` fallback breaks this — the retry looks right but the
    stale sha makes the follow-up count wrong.
"""
import subprocess

from pipeline import git_ops
from pipeline.branch_names import story_branch

STORY_KEY = "st-1"
BASE_BRANCH = "main"


def _git(repo, *args):
    """Run git in ``repo``; raise on failure (fixture setup and inspection)."""
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path):
    """A throwaway repo whose only branch is ``feature`` — deliberately no ``main``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/feature")
    _git(repo, "config", "user.email", "git-ops-test@example.test")
    _git(repo, "config", "user.name", "Git Ops Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _rev_parse(repo, "HEAD")


def _rev_parse(repo, rev):
    return _git(repo, "rev-parse", "--verify", rev).stdout.strip()


def _resolves(repo, rev):
    return git_ops._rev_parses(repo, rev)


def _add_origin_main(repo, sha):
    """Model what ``git fetch origin main`` leaves behind."""
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)


def _agent_commits(repo, count):
    """Land ``count`` commits on the story's agent branch and leave HEAD there."""
    branch = story_branch(STORY_KEY)
    _git(repo, "checkout", "-q", "-B", branch)
    for i in range(count):
        (repo / f"work{i}.txt").write_text(f"work {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"work {i}")


def _count(repo, base, tip="HEAD"):
    return int(_git(repo, "rev-list", "--count", f"{base}..{tip}").stdout.strip())


def _resolve_base(repo, name):
    """Call the contract seam; a plain AssertionError when it is still missing."""
    assert hasattr(git_ops, "resolve_base"), (
        "pipeline.git_ops.resolve_base is missing: the dispatch worktree can never "
        "resolve a base branch that exists only as origin/<base>"
    )
    return git_ops.resolve_base(repo, name)


# (a) origin/main is materialised as a local main ref, and the sha is returned.
def test_origin_main_is_returned_and_materialised_as_local_main(tmp_path):
    repo, base_sha = _init_repo(tmp_path)
    _add_origin_main(repo, base_sha)
    _agent_commits(repo, 1)

    assert not _resolves(repo, BASE_BRANCH), "fixture must start without a local main"

    resolved = _resolve_base(repo, BASE_BRANCH)

    assert resolved == base_sha
    assert resolved != _rev_parse(repo, "HEAD"), "resolve_base must never fall back to HEAD"
    assert _rev_parse(repo, BASE_BRANCH) == base_sha, (
        "the worktree must afterwards resolve the base branch by name"
    )


# (b) materialised once: three commits later the base has not moved, count is 3.
def test_base_is_materialised_once_and_does_not_follow_head(tmp_path):
    repo, base_sha = _init_repo(tmp_path)
    _add_origin_main(repo, base_sha)
    assert _resolve_base(repo, BASE_BRANCH) == base_sha

    _agent_commits(repo, 3)
    head_sha = _rev_parse(repo, "HEAD")
    assert head_sha != base_sha

    assert _resolve_base(repo, BASE_BRANCH) == base_sha, (
        "a second call must return the same base; branch -f main HEAD would move it"
    )
    assert _rev_parse(repo, BASE_BRANCH) == base_sha
    assert _rev_parse(repo, "HEAD") == head_sha
    assert _count(repo, base_sha) == 3
    assert git_ops._worktree_new_commits(repo, STORY_KEY, BASE_BRANCH) == ("yes", BASE_BRANCH)


# (c) unresolved -> None (not HEAD, not cached), then resolvable after a fetch.
def test_unresolved_returns_none_without_caching_a_head_fallback(tmp_path):
    repo, base_sha = _init_repo(tmp_path)
    _agent_commits(repo, 3)

    assert _resolve_base(repo, BASE_BRANCH) is None, (
        "with neither main nor origin/main, the caller needs the explicit "
        "unresolved signal so it reports INCONCLUSIVE instead of guessing"
    )
    assert not _resolves(repo, BASE_BRANCH), "nothing may be invented when unresolvable"
    assert git_ops._worktree_new_commits(repo, STORY_KEY, BASE_BRANCH) == ("unknown", "")

    # `git fetch origin main` now provides the missing ref.
    _add_origin_main(repo, base_sha)

    assert _resolve_base(repo, BASE_BRANCH) == base_sha, (
        "the earlier None must not be cached as a sticky fallback"
    )
    assert _count(repo, base_sha) == 3
    assert git_ops._worktree_new_commits(repo, STORY_KEY, BASE_BRANCH) == ("yes", BASE_BRANCH)


# Negative/boundary inputs: empty and blank base names are unresolved, not HEAD.
def test_empty_base_name_is_unresolved(tmp_path):
    repo, _ = _init_repo(tmp_path)
    _agent_commits(repo, 1)

    assert _resolve_base(repo, "") is None
    assert _resolve_base(repo, "   ") is None
    assert _rev_parse(repo, "HEAD") != ""
