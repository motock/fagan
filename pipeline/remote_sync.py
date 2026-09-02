"""Remote git sync primitives for dispatching agent runs off-box.

Two concerns live here:

* ``ensure_remote_worktree`` prepares the REMOTE side of a dispatch: it
  creates the bare transport mirror if missing, pushes the story branch
  into it, and materializes (or resets) a remote worktree at the pushed
  tip so the harness has a checkout to run in.
* ``sync_back_commits`` brings the LOCAL story worktree back to the
  remote's tip after the run: fast-forward when the remote advanced,
  refuse loudly on divergence.

The bare remote repo is a scratch transport mirror for THIS dispatch, not
history of record: every dispatch force-pushes the local story tip over
whatever the mirror held. History of record stays on the local story
worktree, which ``sync_back_commits`` advances only by fast-forward.
"""

from __future__ import annotations

import functools
import os
import subprocess
from collections.abc import Callable
from pathlib import Path


def _ssh_run(host: str, shell_cmd: str) -> None:
    """Run ``shell_cmd`` on ``host`` over batch-mode ssh; raise on failure."""
    subprocess.run(["ssh", "-o", "BatchMode=yes", host, shell_cmd], check=True)


def ensure_remote_worktree(
    worktree: Path,
    branch: str,
    remote_url: str,
    *,
    remote_cwd: Path,
    run_remote: Callable[[str], None] | None = None,
    host: str | None = None,
) -> Path:
    """Make the remote side ready to run the harness, and return remote_cwd.

    The remote bare repo is a scratch transport mirror for THIS dispatch,
    not history of record, so the push below is intentionally a force
    push: each dispatch overwrites the mirror with the local story tip,
    and the remote worktree is reset to that tip on re-dispatch.

    Runner resolution: a caller-supplied ``run_remote`` wins (even for
    ``file://`` remotes); else ``host`` selects ssh; else a ``file://``
    remote runs its shell commands locally (offline-test mode); anything
    else fails closed - ssh needs an explicit runner.
    """
    if run_remote is not None:
        runner = run_remote
    elif host is not None:
        runner = functools.partial(_ssh_run, host)
    elif remote_url.startswith("file://"):
        runner = functools.partial(subprocess.run, shell=True, check=True)
    else:
        raise ValueError(
            "run_remote is required for ssh remotes: pass run_remote or "
            f"host (got run_remote=None, host={host!r}) for {remote_url}"
        )

    if remote_url.startswith("file://"):
        bare = remote_url.removeprefix("file://")
    else:
        after_scheme = remote_url.split("://", 1)[-1]
        bare = "/" + after_scheme.split("/", 1)[1]

    # The mirror holds a linked worktree that checks out ``branch``, so git
    # refuses the re-dispatch push ("branch is currently checked out"). The
    # mirror is scratch, not history of record, so let the force-push land.
    runner(
        f"mkdir -p {os.path.dirname(bare)} && git init --bare -q {bare} "
        f"&& git -C {bare} config receive.denyCurrentBranch ignore"
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "--force", remote_url, branch],
        check=True,
    )
    # First dispatch materializes the worktree; on re-dispatch ``add``
    # refuses (path already registered) and the ``||`` arm resets the
    # existing worktree to the freshly pushed tip. git has no
    # ``worktree reset`` subcommand, so the reset runs in the worktree.
    runner(
        f"cd {remote_cwd.parent} && git -C {bare} worktree add {remote_cwd} "
        f"{branch} || git -C {remote_cwd} reset --hard {branch}"
    )
    return remote_cwd


def sync_back_commits(worktree: Path, branch: str, remote_url: str) -> str:
    """Fetch the remote tip and fast-forward the local story worktree to it.

    Returns ``"unchanged"`` when both tips already match and
    ``"fast_forwarded"`` when the local branch moves up to the remote
    tip. Divergence raises RuntimeError naming both SHAs: someone wrote
    to the local worktree while the remote ran, and silently losing
    either side is exactly the failure this refusal exists to prevent.
    The local branch is never forced or reset.
    """
    subprocess.run(
        ["git", "-C", str(worktree), "fetch", "-q", remote_url, branch],
        check=True,
    )
    local_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    remote_sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "FETCH_HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if local_sha == remote_sha:
        return "unchanged"
    is_ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "merge-base",
            "--is-ancestor",
            local_sha,
            remote_sha,
        ],
        check=False,
    ).returncode
    if is_ancestor != 0:
        raise RuntimeError(
            f"refusing to sync back {branch}: local tip {local_sha} and remote "
            f"tip {remote_sha} on {remote_url} have diverged; resolve manually, "
            "the local branch is never forced"
        )
    subprocess.run(
        ["git", "-C", str(worktree), "merge", "--ff-only", "FETCH_HEAD"],
        check=True,
    )
    return "fast_forwarded"