"""The pipeline must keep its own acceptance oracle out of story commits.

``dispatch.py`` materializes a story's ``acceptance`` fixtures into the
worktree as plain untracked files, and nothing excluded them from git. The
next ``git add -A`` - the executor's own commit, a WIP commit, or the merge
step - therefore swept the read-only grading oracle into the branch, and the
reviewer's file-inventory criterion ("an added file nobody asked for") then
blocked the story or not depending on where the commit happened to land.

Observed live 2026-09-27 on the ``qwen38_iq3s_e`` bench sweep. The oracle
commit subjects (``chore: track pipeline-materialized acceptance oracle`` at
c05c25a, ``Track pre-existing acceptance oracle file`` at 8fdd80b) appear
nowhere in this repo's source, so a *dispatched model* wrote them: it found
the file it had been told was digest-pinned and read-only, saw it untracked,
and committed it. ``ratelimiter_bugfix`` parked REQUEST_CHANGES on exactly
that file while ``groundtruth_passed`` was true - a correct cell scored as a
failure - while ``lru_cache``, ``interval_merge``, ``retry_backoff`` and
``cron_field`` all merged with the same file tracked.

The oracle is a grading fixture, materialized fresh on every dispatch, so it
never needs to be tracked. These tests pin both halves: the exclusion
mechanism honours caller-supplied paths (behaviourally, through real git),
and the dispatch path actually supplies the story's acceptance paths.
"""
import inspect
import subprocess
from pathlib import Path

from pipeline import dispatch, paths

_ORACLE = "tests/unit/test_oracle_fixture.py"


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


def _materialize(repo: Path) -> None:
    """Mirror what dispatch does: write the oracle, plus one real file."""
    (repo / _ORACLE).parent.mkdir(parents=True, exist_ok=True)
    (repo / _ORACLE).write_text("def test_placeholder():\n    assert True\n")
    (repo / "real_work.py").write_text("x = 1\n")


def _staged(repo: Path) -> list[str]:
    return _git("diff", "--cached", "--name-only", cwd=repo).stdout.split()


def test_extra_paths_are_excluded_from_git_add_all(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _materialize(repo)

    paths._exclude_worktree_logs_from_tracking(repo, [_ORACLE])
    _git("add", "-A", cwd=repo)
    staged = _staged(repo)

    assert "real_work.py" in staged, "the exclusion must not hide real work"
    assert _ORACLE not in staged, (
        "the story's acceptance oracle was swept into the commit - the "
        "materialized fixture must be excluded from git, or the reviewer "
        "blocks a correct story on 'an added file nobody asked for'"
    )


def test_without_extra_the_oracle_is_still_staged(tmp_path):
    """Negative control: the new parameter is load-bearing, not a no-op."""
    repo = _init_repo(tmp_path / "repo")
    _materialize(repo)

    paths._exclude_worktree_logs_from_tracking(repo)
    _git("add", "-A", cwd=repo)

    assert _ORACLE in _staged(repo), (
        "an oracle left untracked with no extra paths must still be staged - "
        "if it is not, this test is not exercising the parameter"
    )


def test_static_excludes_survive_a_call_with_extra(tmp_path):
    """The runtime markers must not be lost when extra paths are supplied."""
    repo = _init_repo(tmp_path / "repo")

    paths._exclude_worktree_logs_from_tracking(repo, [_ORACLE])
    written = (repo / ".git" / "info" / "exclude").read_text()

    for name in paths._WORKTREE_LOG_EXCLUDES:
        assert name in written
    assert _ORACLE in written


def test_dispatch_supplies_the_storys_acceptance_paths():
    """The wiring, not just the mechanism.

    A fixture that only called the helper would pass with the change
    half-done (helper extended, call site never updated) and the story would
    ship dead code - see the "grade the integration, not just the unit" rule
    in .claude/rules/pipeline-story-schema.md. Driving a real
    ``_dispatch_story_impl`` needs an origin remote, a branch and a backend
    launch, so this asserts the wiring landed against the production source
    instead, which is that rule's sanctioned alternative.
    """
    src = inspect.getsource(dispatch._dispatch_story_impl)

    assert "_acceptance_rel_paths(story)" in src, (
        "dispatch must derive the story's acceptance paths"
    )
    assert "_exclude_worktree_logs_from_tracking" in src, (
        "_dispatch_story_impl never calls the exclusion helper - the "
        "materialized oracle stays untracked-and-unignored"
    )
