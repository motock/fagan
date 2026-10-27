"""The PR title must come from the branch's FIRST conventional commit.

The squash-merge subject on the default branch is the PR title. A story that
went through review rework ends with a fix commit answering the review, and
taking the newest subject made master record e.g.
``fix(mcp): pass configured host to the SSE app`` for a story that actually
introduced the transport. ``_branch_commit_subject`` therefore returns the
branch's OLDEST Conventional Commits subject: the commit that introduced the
story's work.

``git log <base>..<branch> --format=%s`` prints newest-first, so the fix is to
keep scanning after the first conventional match instead of returning it, and
to return the last conventional subject seen.

Written FIRST (TDD): test (a) below is RED against the newest-wins
implementation and GREEN once the loop accumulates. The other four pin
behavior this story must not change (the ``wip(`` skip, the ``_pr_title``
fallback, and single-commit branches where both rules agree).
"""

import subprocess

from pipeline import pr as pr_mod

STORY_KEY = "T-1"
SUMMARY = "T-1: Title a squash merge from the story's first commit"


def _git(repo, *args):
    """Run one git command in the throwaway repo (no assertions here)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo, subject):
    """Commit an empty change with `subject` as its message."""
    _git(repo, "commit", "--allow-empty", "-m", subject)


def _make_repo(tmp_path, subjects):
    """A repo with a `main` default ref and a `feature` branch of `subjects`.

    `main` is the base the helper merge-bases against, and `subjects` are
    committed oldest-first on `feature`, so the newest one is applied last.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test Agent")
    _git(repo, "config", "user.email", "agent@example.test")
    _commit(repo, "base commit")
    _git(repo, "update-ref", "refs/heads/main", "HEAD")
    _git(repo, "checkout", "-b", "feature")
    for subject in subjects:
        _commit(repo, subject)
    return str(repo)


def _title(repo):
    """The PR title `_open_pr` would use, fallback expression included."""
    return pr_mod._branch_commit_subject(repo, "feature") or pr_mod._pr_title(
        STORY_KEY, SUMMARY
    )


def test_rework_commit_does_not_replace_the_story_title(tmp_path):
    repo = _make_repo(tmp_path, ["feat(x): add the thing", "fix(x): answer review"])

    assert pr_mod._branch_commit_subject(repo, "feature") == "feat(x): add the thing"


def test_step_cap_checkpoint_still_falls_back_to_the_earlier_feat(tmp_path):
    repo = _make_repo(tmp_path, ["feat(x): add the thing", "wip(T-1): step cap"])

    assert pr_mod._branch_commit_subject(repo, "feature") == "feat(x): add the thing"


def test_only_wip_commits_fall_back_to_the_generated_title(tmp_path):
    repo = _make_repo(tmp_path, ["wip(T-1): step cap", "wip(T-1): step cap again"])

    assert _title(repo) == pr_mod._pr_title(STORY_KEY, SUMMARY)


def test_non_conventional_commit_falls_back_to_the_generated_title(tmp_path):
    repo = _make_repo(tmp_path, ["plain commit message"])

    assert _title(repo) == pr_mod._pr_title(STORY_KEY, SUMMARY)


def test_single_conventional_commit_is_unchanged(tmp_path):
    repo = _make_repo(tmp_path, ["feat(x): add the thing"])

    assert pr_mod._branch_commit_subject(repo, "feature") == "feat(x): add the thing"
