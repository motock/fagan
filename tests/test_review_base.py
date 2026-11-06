"""Tests for pipeline.review_base.resolve_review_base.

First-review diffs are computed as `<base>..HEAD`, so the base must be the
closest candidate trunk a story branch actually diverged from. A stale
`origin/main` (a clone that was never fetched again) makes the naive
"first candidate that resolves" answer return a commit several stories old,
inflating the diff with unrelated history. These tests pin the fix: fetch
once, then pick the candidate with the fewest commits between it and HEAD.

Every fixture repo gets its own bare "remote" so nothing here touches the
network, and commits carry explicit identity so a CI box without a global
git identity still builds the repro.
"""

import logging
import os
import shutil
import subprocess

import pytest

from pipeline.review import _first_review_base
from pipeline.review_base import resolve_review_base


def _git(repo, *args):
    """Run git in `repo` with a fixed identity; return stdout."""
    return subprocess.run(
        [
            "git",
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test",
            *args,
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _sha(repo, rev):
    return _git(repo, "rev-parse", rev).strip()


def _count(repo, rev):
    """`git rev-list --count <rev>..HEAD` - commits HEAD has and `rev` lacks."""
    return int(_git(repo, "rev-list", "--count", f"{rev}..HEAD").strip())


def _commit(repo, message):
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _sha(repo, "HEAD")


class Repro:
    """A clone whose remote main is m0 <- m1 <- m2 while origin/main is m0.

    `story` is cut from m2 and carries one story commit, so a first review
    must diff from m2 (one commit) rather than m0 (three).
    """

    def __init__(self, bare, clone, m0, m1, m2, last):
        self.bare = bare
        self.clone = clone
        self.m0 = m0
        self.m1 = m1
        self.m2 = m2
        self.last = last  # s1 on the story branch, or x in the tie fixture


def _make_remote(tmp_path, history=("m0", "m1", "m2")):
    """A bare repo on `main` holding the given empty commits, plus a seed clone."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "checkout", "-b", "main")
    shas = [_commit(seed, msg) for msg in history]
    _git(seed, "push", "-u", "origin", "main")
    return bare, seed, shas


@pytest.fixture
def repro(tmp_path):
    """Bare remote at m2; a clone whose origin/main was reset back to m0."""
    bare, _seed, shas = _make_remote(tmp_path)
    m0, m1, m2 = shas
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "update-ref", "refs/remotes/origin/main", m0)
    _git(clone, "checkout", "-b", "story", m2)
    s1 = _commit(clone, "s: the story commit")
    return Repro(bare, clone, m0, m1, m2, s1)


def test_resolve_review_base_fetches_and_prefers_fresh_remote(repro):
    """(a1) A stale origin/main is refreshed, so the base becomes m2."""
    assert _count(repro.clone, repro.m0) == 3  # before fetch: m1, m2, s1

    base = resolve_review_base(str(repro.clone))

    assert _sha(repro.clone, "origin/main") == repro.m2  # fetch happened
    assert _count(repro.clone, repro.m2) == 1  # after fetch: just s1
    assert base == repro.m2


def test_first_review_base_fetches_and_prefers_fresh_remote(repro):
    """(a2) The caller every review path funnels through sees the same base."""
    assert _first_review_base(str(repro.clone)) == repro.m2


@pytest.fixture
def local_main_ahead(tmp_path):
    """Local main is at m2 while the remote (and so origin/main) lags at m0."""
    bare, _seed, shas = _make_remote(tmp_path, history=("m0",))
    m0 = shas[0]
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    m1 = _commit(clone, "m1")
    m2 = _commit(clone, "m2")
    _git(clone, "checkout", "-b", "story", m2)
    s1 = _commit(clone, "s: the story commit")
    return Repro(bare, clone, m0, m1, m2, s1)


def test_local_main_beats_stale_origin_even_though_origin_comes_first(
    local_main_ahead,
):
    """(b) Minimum count wins: local main (1) beats origin/main (3)."""
    clone = local_main_ahead.clone
    assert _count(clone, "origin/main") == 3
    assert _count(clone, "main") == 1

    assert resolve_review_base(str(clone)) == local_main_ahead.m2


def test_unreachable_remote_falls_back_to_local_refs(repro):
    """(c) A fetch that cannot reach the remote must not raise."""
    _git(
        repro.clone,
        "remote", "set-url", "origin", "file:///nonexistent/repo.git",
    )

    assert resolve_review_base(str(repro.clone)) == repro.m2


def test_no_candidate_ref_resolves_to_none(tmp_path):
    """(d) A repo with no trunk candidate at all yields None, not an error."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-b", "story")
    _commit(repo, "s: the only commit")

    assert resolve_review_base(str(repo)) is None


@pytest.fixture
def tie(tmp_path):
    """origin/main = m2 and local main = x, a sibling of s1 on top of m2.

    Both candidates are one commit away from HEAD, so only candidate order
    can break the tie - and origin/main comes first.
    """
    bare, _seed, shas = _make_remote(tmp_path)
    m0, m1, m2 = shas
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    x = _commit(clone, "x: a side commit never merged")
    _git(clone, "checkout", "-b", "story", m2)
    _commit(clone, "s: the story commit")
    return Repro(bare, clone, m0, m1, m2, x)


def test_ties_keep_candidate_order(tie):
    """(e) Equal counts -> the earlier candidate (origin/main = m2) wins."""
    clone = tie.clone
    assert _count(clone, "origin/main") == 1
    assert _count(clone, "main") == 1

    base = resolve_review_base(str(clone))

    assert base == tie.m2
    assert base != tie.last  # not the side commit x


def test_fetch_runs_at_most_once(repro, tmp_path, monkeypatch):
    """(f) One fetch per call - not one per candidate, and never skipped."""
    real_git = shutil.which("git")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "git.log"
    shim = bindir / "git"
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{real_git}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    resolve_review_base(str(repro.clone))

    invocations = [
        line.split() for line in log.read_text().splitlines() if line.strip()
    ]
    fetches = [args for args in invocations if args and args[0] == "fetch"]
    assert len(fetches) == 1
    # The shim really was on the path for the read-only calls too.
    assert any(args and args[0] in {"rev-parse", "rev-list"} for args in invocations)


def test_every_call_re_fetches_instead_of_caching_a_failed_one(repro):
    """A failed fetch must not be remembered for the process's lifetime.

    Call 1 cannot reach the remote and both origin/main and local main are
    stale at m0, so the honest answer is m0 (three commits back). Call 2 in
    the same process finds the network restored and must fetch again: a
    module-level "already fetched" flag would leave it answering m0 forever.
    """
    _git(repro.clone, "branch", "-f", "main", repro.m0)
    _git(
        repro.clone,
        "remote", "set-url", "origin", "file:///nonexistent/repo.git",
    )
    assert resolve_review_base(str(repro.clone)) == repro.m0

    _git(repro.clone, "remote", "set-url", "origin", str(repro.bare))

    assert resolve_review_base(str(repro.clone)) == repro.m2


def test_warning_never_leaks_the_remote_url(repro, caplog):
    """(g) The credentialed URL lives in git's stderr: it must not be logged."""
    _git(
        repro.clone,
        "remote", "set-url", "origin",
        "https://x-access-token:SECRET@127.0.0.1:1/repo.git",
    )

    with caplog.at_level(logging.WARNING):
        base = resolve_review_base(str(repro.clone))

    assert base == repro.m2  # local refs still resolve the base
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
    assert "SECRET" not in caplog.text
    assert "https://" not in caplog.text


@pytest.fixture
def trunk_moved_on(tmp_path):
    """origin/main advanced to m3 *after* the story forked from m2.

    This is the ordinary case once the fetch succeeds: the trunk tip is ahead
    of the fork point, and the extra commit belongs to another story. The
    story branch was cut from m2 and carries `story.py`.
    """
    bare, seed, shas = _make_remote(tmp_path)
    m0, m1, m2 = shas
    (seed / "other_story.py").write_text("landed by another story\n")
    _git(seed, "add", "other_story.py")
    m3 = _commit(seed, "other: land another story")
    _git(seed, "push", "origin", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "checkout", "-b", "story", m2)
    (clone / "story.py").write_text("this story's change\n")
    _git(clone, "add", "story.py")
    s1 = _commit(clone, "s: the story commit")
    return Repro(bare, clone, m0, m1, m2, s1), m3


def test_base_is_the_fork_point_when_trunk_advanced_past_it(trunk_moved_on):
    """The winner's *merge-base*, not its tip, is the first-review base.

    origin/main is one commit from HEAD either way, so it still wins on
    count; but it carries another story's commit on top of the fork point.
    Returning the tip makes `git diff <base>..HEAD` report that file as a
    deletion and the scope gate flag it as outside the story's files.
    """
    repro, m3 = trunk_moved_on
    clone = repro.clone
    assert _sha(clone, "origin/main") == m3  # clone saw the landed commit
    assert _count(clone, "origin/main") == 1  # only s1 after the fork
    assert _count(clone, repro.m2) == 1  # tie on count

    base = resolve_review_base(str(clone))

    assert base == repro.m2
    assert base != m3  # the tip is not an ancestor of HEAD
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"],
        check=False, cwd=str(clone), capture_output=True,
    ).returncode == 0
    changed = _git(clone, "diff", "--name-only", f"{base}..HEAD").split()
    assert changed == ["story.py"]
    assert "other_story.py" not in changed

