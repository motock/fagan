"""A local agent's own working files never reach a team repository's commits.

The local driver writes `.agent_transcript.json` into the worktree on every
dispatch, and `pipeline/watchers.py` renames `.agent_done` to
`.agent_done.consumed`. A team repository has no `.gitignore` line for them
(core's own does), so the story's `git add -A` swept them into the branch and
the scope gate parked the story after three review cycles.

These tests use a real temporary git repository and assert the invariant the
pipeline holds on its own: `_exclude_worktree_logs_from_tracking` keeps the
runtime files out of `git add -A`, and `_commit_wip` untracks a copy that an
earlier commit already tracked.
"""
import shutil
import subprocess
from pathlib import Path

from pipeline import paths
from pipeline.git_ops import _commit_wip

_RUNTIME_FILES = (".agent_transcript.json", ".agent_done", ".agent_done.consumed")


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "master", ".", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "src").mkdir()
    (path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git("add", "src/calc.py", cwd=path)
    _git("commit", "-q", "-m", "seed", cwd=path)
    return path


def _write_agent_runtime_files(worktree: Path) -> None:
    (worktree / ".agent_transcript.json").write_text("[]")
    (worktree / ".agent_done").write_text("")
    (worktree / ".agent_done.consumed").write_text("")


def _stage_everything_and_list(worktree: Path) -> set[str]:
    _git("add", "-A", cwd=worktree)
    out = _git("diff", "--cached", "--name-only", cwd=worktree).stdout
    return set(out.split())


def _exclude_lines(repo: Path) -> list[str]:
    return (repo / ".git" / "info" / "exclude").read_text().splitlines()


def test_exclude_lists_each_agent_runtime_file(tmp_path):
    repo = _init_repo(tmp_path / "team")

    paths._exclude_worktree_logs_from_tracking(repo)

    lines = _exclude_lines(repo)
    for name in _RUNTIME_FILES:
        assert name in lines


def test_git_add_all_skips_agent_runtime_files_in_repo_without_gitignore(tmp_path):
    repo = _init_repo(tmp_path / "team")
    assert not (repo / ".gitignore").exists()
    paths._exclude_worktree_logs_from_tracking(repo)

    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    _write_agent_runtime_files(repo)

    staged = _stage_everything_and_list(repo)

    assert staged == {"src/calc.py"}


def test_git_add_all_skips_agent_runtime_files_in_linked_worktree(tmp_path):
    repo = _init_repo(tmp_path / "team")
    paths._exclude_worktree_logs_from_tracking(repo)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "agent/otp-x", str(linked), cwd=repo)

    (linked / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    _write_agent_runtime_files(linked)

    staged = _stage_everything_and_list(linked)

    assert staged == {"src/calc.py"}


def test_repeated_exclude_call_does_not_duplicate_runtime_file_lines(tmp_path):
    repo = _init_repo(tmp_path / "team")

    paths._exclude_worktree_logs_from_tracking(repo)
    paths._exclude_worktree_logs_from_tracking(repo)

    lines = _exclude_lines(repo)
    for name in _RUNTIME_FILES:
        assert lines.count(name) == 1


def test_exclude_creates_file_when_git_info_directory_is_missing(tmp_path):
    repo = _init_repo(tmp_path / "team")
    shutil.rmtree(repo / ".git" / "info")

    paths._exclude_worktree_logs_from_tracking(repo)

    lines = _exclude_lines(repo)
    for name in _RUNTIME_FILES:
        assert name in lines


def test_commit_wip_untracks_agent_runtime_file_an_earlier_commit_tracked(tmp_path):
    repo = _init_repo(tmp_path / "team")
    paths._exclude_worktree_logs_from_tracking(repo)
    _write_agent_runtime_files(repo)
    # An earlier commit (e.g. the agent's own `git add -f`) already tracks it,
    # so the exclude alone cannot stop it from riding into the next commit.
    _git("add", "-f", ".agent_transcript.json", cwd=repo)
    _git("commit", "-q", "-m", "earlier", cwd=repo)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")

    _commit_wip(str(repo), "otp-x", "checkpoint")

    tracked = set(_git("ls-files", cwd=repo).stdout.split())
    assert ".agent_transcript.json" not in tracked
    assert (repo / ".agent_transcript.json").exists()
