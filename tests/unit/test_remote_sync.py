"""Unit tests for pipeline/remote_sync.py.

The remote-sync contract has two directions:

* ``ensure_remote_worktree`` makes the REMOTE side ready to run the harness:
  create the bare transport mirror if missing, force-push the story branch
  into it, and materialize (or reset) a remote worktree at the pushed tip.
* ``sync_back_commits`` brings the LOCAL story worktree back to the remote's
  tip: fast-forward when the remote advanced, refuse loudly on divergence.

Git is exercised for real via subprocess against tmp_path repos - git is
never mocked. The only injected double is ``run_remote``, and the fake used
here executes the shell commands for real (locally) while recording them, so
command-routing assertions still run actual git.

Helpers live at module level so each test body is straight-line: no loops,
no conditionals, one behavioral outcome per test.
"""

import inspect
import re
import subprocess
from pathlib import Path

import pytest

from pipeline import remote_sync

BRANCH = "agent/remote-sync"
GIT_EMAIL = "remote-sync-tests@example.invalid"
GIT_NAME = "remote-sync-tests"
_GIT_IDENTITY = ("-c", f"user.email={GIT_EMAIL}", "-c", f"user.name={GIT_NAME}")


def _git(cwd: Path, *args: str) -> str:
    """Run a real git command in ``cwd``; return stripped stdout."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, (
        f"git -C {cwd} {' '.join(args)} failed: {proc.stderr.strip()}"
    )
    return proc.stdout.strip()


def _make_story_worktree(root: Path) -> Path:
    """Local story worktree with exactly one commit on BRANCH."""
    worktree = root / "story-worktree"
    worktree.mkdir()
    _git(worktree, "init", "-q", "-b", BRANCH)
    (worktree / "story.md").write_text("story body\n")
    _git(worktree, "add", ".")
    _git(worktree, *_GIT_IDENTITY, "commit", "-q", "-m", "initial story commit")
    return worktree


def _remote_paths(root: Path) -> tuple[Path, Path, str]:
    """(bare repo path, remote worktree path, file:// remote_url)."""
    bare = root / "remote-mirror.git"
    remote_cwd = root / "remote-worktree"
    return bare, remote_cwd, f"file://{bare}"


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    """Commit one file in ``repo`` (works in linked worktrees too)."""
    (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, *_GIT_IDENTITY, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _executing_recorder(calls: list[str]):
    """run_remote double: records each shell command, then really runs it."""

    def _run_remote(shell_cmd: str) -> None:
        calls.append(shell_cmd)
        subprocess.run(shell_cmd, shell=True, check=True, capture_output=True)

    return _run_remote


def _commands_containing(calls: list[str], fragment: str) -> list[str]:
    return [cmd for cmd in calls if fragment in cmd]


# ---------- ensure_remote_worktree ----------


def test_file_remote_needs_no_runner_and_lands_branch_on_remote(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    bare, remote_cwd, remote_url = _remote_paths(tmp_path)

    result = remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )

    assert result == remote_cwd
    assert _git(bare, "rev-parse", "--is-bare-repository") == "true"
    assert _git(remote_cwd, "rev-parse", "HEAD") == _git(worktree, "rev-parse", "HEAD")


def test_explicit_run_remote_drives_bare_init_and_worktree_add(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    bare, remote_cwd, remote_url = _remote_paths(tmp_path)
    calls: list[str] = []

    remote_sync.ensure_remote_worktree(
        worktree,
        BRANCH,
        remote_url,
        remote_cwd=remote_cwd,
        run_remote=_executing_recorder(calls),
    )

    init_commands = _commands_containing(calls, "git init --bare")
    assert len(init_commands) == 1
    assert "mkdir -p" in init_commands[0]
    assert str(bare) in init_commands[0]
    add_commands = _commands_containing(calls, "worktree add")
    assert len(add_commands) >= 1
    assert str(remote_cwd) in add_commands[0]
    assert str(bare) in add_commands[0]
    assert _git(remote_cwd, "rev-parse", "HEAD") == _git(worktree, "rev-parse", "HEAD")


def test_host_supplied_routes_commands_through_ssh_runner(tmp_path, monkeypatch):
    worktree = _make_story_worktree(tmp_path)
    _, remote_cwd, remote_url = _remote_paths(tmp_path)
    hosts: list[str] = []
    commands: list[str] = []

    def _fake_ssh_run(host: str, shell_cmd: str) -> None:
        hosts.append(host)
        commands.append(shell_cmd)
        subprocess.run(shell_cmd, shell=True, check=True, capture_output=True)

    monkeypatch.setattr(remote_sync, "_ssh_run", _fake_ssh_run)
    remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd, host="build-host"
    )

    assert set(hosts) == {"build-host"}
    assert len(_commands_containing(commands, "git init --bare")) == 1
    assert _git(remote_cwd, "rev-parse", "HEAD") == _git(worktree, "rev-parse", "HEAD")


def test_ssh_remote_without_runner_or_host_fails_closed(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    _, remote_cwd, _ = _remote_paths(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        remote_sync.ensure_remote_worktree(
            worktree,
            BRANCH,
            "ssh://git@example.invalid/srv/remote-mirror.git",
            remote_cwd=remote_cwd,
        )

    message = str(excinfo.value)
    assert "run_remote is required for ssh remotes" in message
    assert "host" in message


def test_redispatch_resets_remote_worktree_to_new_local_tip(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    _, remote_cwd, remote_url = _remote_paths(tmp_path)
    remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )

    new_tip = _commit_file(worktree, "second.txt", "second change\n", "second commit")
    remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )

    assert _git(remote_cwd, "rev-parse", "HEAD") == new_tip


def test_ensure_docstring_documents_force_push_as_intentional():
    doc = inspect.getdoc(remote_sync.ensure_remote_worktree) or ""

    assert "force" in doc.lower()


# ---------- sync_back_commits ----------


def test_sync_back_returns_unchanged_when_tips_match(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    _, remote_cwd, remote_url = _remote_paths(tmp_path)
    remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )

    outcome = remote_sync.sync_back_commits(worktree, BRANCH, remote_url)

    assert outcome == "unchanged"


def test_sync_back_fast_forwards_local_worktree_to_remote_tip(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    _, remote_cwd, remote_url = _remote_paths(tmp_path)
    remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )
    _commit_file(
        remote_cwd, "remote-note.txt", "written on the remote\n", "remote-side note"
    )

    outcome = remote_sync.sync_back_commits(worktree, BRANCH, remote_url)

    assert outcome == "fast_forwarded"
    assert (worktree / "remote-note.txt").read_text() == "written on the remote\n"


def test_sync_back_refuses_diverged_histories_and_never_forces_local(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    _, remote_cwd, remote_url = _remote_paths(tmp_path)
    remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )
    remote_sha = _commit_file(
        remote_cwd, "remote-note.txt", "from remote\n", "remote-side commit"
    )
    local_sha = _commit_file(
        worktree, "local-note.txt", "from local\n", "local-side commit"
    )

    with pytest.raises(RuntimeError) as excinfo:
        remote_sync.sync_back_commits(worktree, BRANCH, remote_url)

    message = str(excinfo.value)
    assert local_sha in message
    assert remote_sha in message
    assert BRANCH in message
    assert remote_url in message
    assert _git(worktree, "rev-parse", "HEAD") == local_sha


# ---------- module shape and the production ssh runner ----------


def test_module_defines_exactly_the_three_contract_functions():
    source = Path(inspect.getfile(remote_sync)).read_text()

    assert len(re.findall(r"(?m)^def ", source)) == 3
    assert callable(remote_sync.ensure_remote_worktree)
    assert callable(remote_sync.sync_back_commits)
    assert callable(remote_sync._ssh_run)


def test_ssh_run_uses_batch_mode_ssh():
    source = inspect.getsource(remote_sync._ssh_run)

    assert "ssh" in source
    assert "BatchMode=yes" in source


def test_ssh_run_raises_called_process_error_for_unreachable_host():
    with pytest.raises(subprocess.CalledProcessError):
        remote_sync._ssh_run("remote-sync-no-such-host.invalid", "true")


# ---------- regression: shell-context quoting of interpolated values ----------
#
# Review Blocking #1: ``bare``, ``remote_cwd``, ``remote_cwd.parent`` and
# ``branch`` are interpolated raw into shell command strings executed with
# ``shell=True`` locally and shipped to remote shells via ``_ssh_run``.
# Git refs legally allow ``;``, ``$``, ``|``, ``&`` and backticks (only
# ``~ ^ : ? * [ ] \\`` and space are forbidden), and paths may contain
# spaces, so both tests below exercise real git with values the unquoted
# f-strings split into extra shell words.


def _make_story_worktree_on_branch(root: Path, branch: str) -> Path:
    """Local story worktree with exactly one commit on ``branch``."""
    worktree = root / "story-worktree"
    worktree.mkdir()
    _git(worktree, "init", "-q", "-b", branch)
    (worktree / "story.md").write_text("story body\n")
    _git(worktree, "add", ".")
    _git(worktree, *_GIT_IDENTITY, "commit", "-q", "-m", "initial story commit")
    return worktree


def test_file_remote_path_with_space_syncs_end_to_end(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    bare = tmp_path / "my mirrors" / "remote.git"
    remote_cwd = tmp_path / "my mirrors" / "remote worktree"
    remote_url = f"file://{bare}"

    result = remote_sync.ensure_remote_worktree(
        worktree, BRANCH, remote_url, remote_cwd=remote_cwd
    )

    assert result == remote_cwd
    assert _git(bare, "rev-parse", "--is-bare-repository") == "true"
    assert _git(remote_cwd, "rev-parse", "HEAD") == _git(worktree, "rev-parse", "HEAD")


def test_branch_with_semicolon_pushes_and_fetches_that_exact_ref(tmp_path):
    branch = "feat;x"  # legal git ref: ``;`` is not in git's forbidden set
    worktree = _make_story_worktree_on_branch(tmp_path, branch)
    bare, remote_cwd, remote_url = _remote_paths(tmp_path)

    remote_sync.ensure_remote_worktree(
        worktree, branch, remote_url, remote_cwd=remote_cwd
    )

    local_tip = _git(worktree, "rev-parse", "HEAD")
    assert _git(bare, "rev-parse", branch) == local_tip
    assert _git(remote_cwd, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert _git(remote_cwd, "rev-parse", "HEAD") == local_tip

    outcome = remote_sync.sync_back_commits(worktree, branch, remote_url)

    assert outcome == "unchanged"


# ---------- regression: remote_url validated before any remote command ----------
#
# Review Blocking #2: ``git@host:srv/remote-mirror.git`` (scp-like, no
# ``://``) used to mis-parse to ``/remote-mirror.git``, so the remote ran
# ``mkdir -p / && git init --bare /remote-mirror.git`` - a stray bare repo
# at the remote filesystem root - before the push failed against the
# unrelated ``srv/remote-mirror.git`` path. These tests pin the
# entry-point contract: unsupported URL forms raise ValueError BEFORE the
# runner is invoked at all (zero recorded runner calls, so no remote side
# effect can survive the failed call), and the supported forms parse to
# the intended absolute path.
#
# ssh:// remotes have no real endpoint in unit tests, so the recording
# runner below stops the run after the first command instead of executing
# it; reaching the runner at all proves URL parsing happened, and the
# recorded string shows exactly which path was parsed from the URL.


class _StopAfterFirstCommand(Exception):
    """Sentinel raised by the recording runner to end the harness run."""


def _recording_stop_runner(calls: list[str]):
    """run_remote double: records the first shell command, then stops."""

    def _run_remote(shell_cmd: str) -> None:
        calls.append(shell_cmd)
        raise _StopAfterFirstCommand(shell_cmd)

    return _run_remote


def test_scp_like_remote_url_is_rejected_before_any_remote_command(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    remote_cwd = tmp_path / "remote-worktree"
    calls: list[str] = []
    remote_url = "git@host:srv/remote-mirror.git"

    with pytest.raises(ValueError) as excinfo:
        remote_sync.ensure_remote_worktree(
            worktree,
            BRANCH,
            remote_url,
            remote_cwd=remote_cwd,
            run_remote=_recording_stop_runner(calls),
        )

    assert calls == []
    assert remote_url in str(excinfo.value)


def test_relative_ssh_remote_url_is_rejected_before_any_remote_command(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    remote_cwd = tmp_path / "remote-worktree"
    calls: list[str] = []
    remote_url = "ssh://host/relative/x.git"

    with pytest.raises(ValueError) as excinfo:
        remote_sync.ensure_remote_worktree(
            worktree,
            BRANCH,
            remote_url,
            remote_cwd=remote_cwd,
            run_remote=_recording_stop_runner(calls),
        )

    assert calls == []
    assert remote_url in str(excinfo.value)


def test_host_supplied_scp_like_url_is_rejected_before_any_ssh_command(
    tmp_path, monkeypatch
):
    worktree = _make_story_worktree(tmp_path)
    remote_cwd = tmp_path / "remote-worktree"
    calls: list[str] = []
    remote_url = "git@host:srv/remote-mirror.git"

    def _fake_ssh_run(host: str, shell_cmd: str) -> None:
        calls.append(shell_cmd)
        raise _StopAfterFirstCommand(shell_cmd)

    monkeypatch.setattr(remote_sync, "_ssh_run", _fake_ssh_run)

    with pytest.raises(ValueError) as excinfo:
        remote_sync.ensure_remote_worktree(
            worktree, BRANCH, remote_url, remote_cwd=remote_cwd, host="build-host"
        )

    assert calls == []
    assert remote_url in str(excinfo.value)


def test_file_url_with_localhost_authority_parses_to_absolute_path(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    bare = tmp_path / "x.git"
    remote_cwd = tmp_path / "remote-worktree"
    calls: list[str] = []

    with pytest.raises(_StopAfterFirstCommand):
        remote_sync.ensure_remote_worktree(
            worktree,
            BRANCH,
            f"file://localhost{bare}",
            remote_cwd=remote_cwd,
            run_remote=_recording_stop_runner(calls),
        )

    assert len(calls) == 1
    assert str(bare) in calls[0]
    assert f"localhost{bare}" not in calls[0]


def test_ssh_url_with_absolute_path_parses_bare_repo_path_from_url(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    remote_cwd = tmp_path / "remote-worktree"
    calls: list[str] = []

    with pytest.raises(_StopAfterFirstCommand):
        remote_sync.ensure_remote_worktree(
            worktree,
            BRANCH,
            "ssh://host/srv/remote-mirror.git",
            remote_cwd=remote_cwd,
            run_remote=_recording_stop_runner(calls),
        )

    assert len(calls) == 1
    assert "/srv/remote-mirror.git" in calls[0]


def test_plain_file_url_parses_to_absolute_path(tmp_path):
    worktree = _make_story_worktree(tmp_path)
    bare = tmp_path / "x.git"
    remote_cwd = tmp_path / "remote-worktree"
    calls: list[str] = []

    with pytest.raises(_StopAfterFirstCommand):
        remote_sync.ensure_remote_worktree(
            worktree,
            BRANCH,
            f"file://{bare}",
            remote_cwd=remote_cwd,
            run_remote=_recording_stop_runner(calls),
        )

    assert len(calls) == 1
    assert str(bare) in calls[0]
