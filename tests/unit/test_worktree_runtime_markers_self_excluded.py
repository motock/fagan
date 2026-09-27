"""The pipeline must exclude its own runtime markers in ANY host repo.

`spawn_local` opens a *bare* `.log` for every phase it runs - agent.log,
review.log, test_author.log, rework_test_author.log, grading.log - and the
TDD-split dispatcher writes `.tdd_split_test_author_done`. Only `agent.log`
and `review.log` were named in `_WORKTREE_LOG_EXCLUDES`, so in a host repo
that does not itself `.gitignore` the rest, the story's first `_commit_wip`
(`git add -A`) swept them into the feature commit and the review gate then
rejected the story on its file inventory ("an added file nobody asked for").
A *correct* implementation scored as a first-pass-clean failure.

Observed live under `tests/benchmark/`: `token_bucket` parked REQUEST_CHANGES
with `groundtruth_passed: true` in two independent runs, both times on a
stray `test_author.log` / `.tdd_split_test_author_done` in the story commit.

These tests pin the invariant the pipeline must hold on its own, without
depending on the host repo's `.gitignore`: the exclusion list carries a glob
covering every phase log, and `_exclude_worktree_logs_from_tracking` ALONE
keeps both artifacts out of `git add -A`.
"""
import subprocess
from pathlib import Path

import pytest

from pipeline import paths

_PHASE_LOG = "test_author.log"
_SPLIT_MARKER = ".tdd_split_test_author_done"


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "master", ".", cwd=path)
    _git("config", "user.email", "t@t.com", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    return path


def test_exclusion_list_covers_every_phase_log():
    """A glob, not a hand-enumerated list: spawn_local opens a bare .log per
    phase, so naming only the two long-standing ones leaves every other
    phase's log exposed in a host repo that does not ignore *.log."""
    assert "*.log" in paths._WORKTREE_LOG_EXCLUDES, (
        "'*.log' missing from pipeline.paths._WORKTREE_LOG_EXCLUDES - a host "
        "repo that does not itself gitignore *.log will sweep test_author.log "
        "into the story's first WIP commit (Mode 17)"
    )


def test_exclusion_list_carries_the_tdd_split_marker():
    """This repo ignores the marker via .gitignore; no other host repo does."""
    assert _SPLIT_MARKER in paths._WORKTREE_LOG_EXCLUDES, (
        f"{_SPLIT_MARKER} missing from pipeline.paths._WORKTREE_LOG_EXCLUDES - "
        "it is ignored in this repo by .gitignore alone, which no other host "
        "repo has, so dispatched worktrees there will track it"
    )


@pytest.mark.parametrize("name", [_PHASE_LOG, _SPLIT_MARKER])
def test_info_exclude_alone_keeps_artifact_out_of_git_add_all(tmp_path, name):
    """End-to-end, and deliberately with NO `.gitignore` in the repo: the
    pipeline's own `.git/info/exclude` write must be sufficient on its own."""
    repo = _init_repo(tmp_path / "repo")
    paths._exclude_worktree_logs_from_tracking(repo)
    (repo / name).write_text("x\n")
    (repo / "real_work.py").write_text("x = 1\n")

    _git("add", "-A", cwd=repo)
    staged = _git("diff", "--cached", "--name-only", cwd=repo).stdout.split()

    assert "real_work.py" in staged
    assert name not in staged
    assert name not in _git("status", "--porcelain", cwd=repo).stdout
