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

Security invariants: ``remote_url`` is validated at the point of entry
BEFORE any command runs, and every value interpolated into a shell
command is encoded for the shell context with ``shlex.quote``. Git refs
legally contain ``;``, ``$``, ``|``, ``&`` and backticks, and paths
legally contain spaces, so unquoted interpolation is shell injection -
POSIX quoting is correct both for the local ``shell=True`` runner and for
the remote shell that ``_ssh_run`` hands the command to.
"""

from __future__ import annotations

import functools
import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

# First path segments that mark an ``ssh://`` URL path as home-relative
# rather than absolute. git's scp-like syntax spells home-relative paths as
# ``host:relative/x.git``; the same intent written as a URL
# (``ssh://host/relative/x.git``) is ambiguous, so it is rejected instead of
# being silently promoted to the wrong absolute path ``/relative/x.git``.
_RELATIVE_FIRST_SEGMENTS = frozenset({".", "..", "~", "relative"})


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

    Supported ``remote_url`` forms, validated before any command runs:

    * ``file:///abs/path.git`` - bare mirror at ``/abs/path.git`` locally;
    * ``file://localhost/abs/path.git`` - the same, with the loopback
      authority spelled out;
    * ``ssh://[user@]host/abs/path.git`` - bare mirror at ``/abs/path.git``
      on ``host``.

    Every other form raises ValueError before any shell command executes:
    scp-like ``user@host:path`` (no ``://``), ``ssh://`` paths whose first
    segment marks them home-relative (``relative/...``, ``~/...``,
    ``./...``, ``../...``), foreign ``file://`` authorities, and unknown
    schemes. A malformed URL must never leave stray state behind (a bare
    repo created at the wrong path on the remote host).

    The remote bare repo is a scratch transport mirror for THIS dispatch,
    not history of record, so the push below is intentionally a force
    push: each dispatch overwrites the mirror with the local story tip,
    and the remote worktree is reset to that tip on re-dispatch.

    Runner resolution: a caller-supplied ``run_remote`` wins (even for
    ``file://`` remotes); else ``host`` selects ssh; else a ``file://``
    remote runs its shell commands locally (offline-test mode); anything
    else fails closed - ssh needs an explicit runner.
    """
    # ---- URL validation (before runner resolution and any runner call) ----
    if remote_url.startswith("file://"):
        after_authority = remote_url[len("file://") :]
        if after_authority.lower().startswith("localhost/"):
            after_authority = after_authority[len("localhost") :]
        if not after_authority.startswith("/") or not after_authority.strip("/"):
            raise ValueError(
                f"unsupported remote_url {remote_url!r}: file:// remotes need an "
                "absolute path to a bare repo (file:///abs/path.git or "
                "file://localhost/abs/path.git), not a foreign authority or a "
                "relative path"
            )
        bare = after_authority
    elif remote_url.startswith("ssh://"):
        after_scheme = remote_url[len("ssh://") :]
        authority, _, path = after_scheme.partition("/")
        if not authority or not path.strip("/"):
            raise ValueError(
                f"unsupported remote_url {remote_url!r}: ssh:// remotes need "
                "<host>/<absolute path> (ssh://host/srv/remote-mirror.git)"
            )
        first_segment = path.lstrip("/").split("/", 1)[0]
        if first_segment in _RELATIVE_FIRST_SEGMENTS:
            raise ValueError(
                f"unsupported remote_url {remote_url!r}: ssh:// paths must be "
                f"absolute on the remote host, but {first_segment!r}/... reads as "
                "a home-relative scp-like path (host:relative/...); supported "
                "forms are file:///abs/path.git, file://localhost/abs/path.git "
                "and ssh://[user@]host/abs/path.git"
            )
        bare = "/" + path.lstrip("/")
    else:
        raise ValueError(
            f"unsupported remote_url {remote_url!r}: expected file:///abs/path.git, "
            "file://localhost/abs/path.git or ssh://[user@]host/abs/path.git - "
            "scp-like user@host:path and other schemes are not supported"
        )

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

    # The mirror holds a linked worktree that checks out ``branch``, so git
    # refuses the re-dispatch push ("branch is currently checked out"). The
    # mirror is scratch, not history of record, so let the force-push land.
    quoted_bare = shlex.quote(bare)
    quoted_bare_parent = shlex.quote(os.path.dirname(bare))
    runner(
        f"mkdir -p {quoted_bare_parent} && git init --bare -q {quoted_bare} "
        f"&& git -C {quoted_bare} config receive.denyCurrentBranch ignore"
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "--force", remote_url, branch],
        check=True,
    )
    # First dispatch materializes the worktree; on re-dispatch ``add``
    # refuses (path already registered) and the ``||`` arm resets the
    # existing worktree to the freshly pushed tip. git has no
    # ``worktree reset`` subcommand, so the reset runs in the worktree.
    # Every interpolated value is shlex.quote'd: branch names derive from
    # AI-authored slugs and legally contain ``;``/``$``/backticks, and
    # paths legally contain spaces, so unquoted interpolation would let
    # the shell split words or run extra commands (shell injection).
    runner(
        f"cd {shlex.quote(str(remote_cwd.parent))} "
        f"&& git -C {quoted_bare} worktree add {shlex.quote(str(remote_cwd))} "
        f"{shlex.quote(branch)} "
        f"|| git -C {shlex.quote(str(remote_cwd))} reset --hard {shlex.quote(branch)}"
    )
    return remote_cwd


def sync_back_commits(worktree: Path, branch: str, remote_url: str) -> str:
    """Fetch the remote tip and fast-forward the local story worktree to it.

    Returns ``"unchanged"`` when both tips already match and
    ``"fast_forwarded"`` when the local branch moves up to the remote
    tip. Divergence raises RuntimeError naming both SHAs: someone wrote
    to the local worktree while the remote ran, and silently losing
    either side is exactly the failure this refusal exists to prevent.
    The local branch is never forced or reset - a remote tip that is
    merely behind the local tip is refused too, with its own message.
    The merge also refuses to run unless ``branch`` is the branch actually
    checked out in ``worktree`` (``git symbolic-ref HEAD``), so a caller
    that checked out something else cannot have FETCH_HEAD merged into it
    by mistake.
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
        remote_behind = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "merge-base",
                    "--is-ancestor",
                    remote_sha,
                    local_sha,
                ],
                check=False,
            ).returncode
            == 0
        )
        if remote_behind:
            raise RuntimeError(
                f"refusing to sync back {branch}: remote tip {remote_sha} on "
                f"{remote_url} is behind local tip {local_sha} (the remote "
                "contributed nothing); the local branch is never reset or forced"
            )
        raise RuntimeError(
            f"refusing to sync back {branch}: local tip {local_sha} and remote "
            f"tip {remote_sha} on {remote_url} have diverged; resolve manually, "
            "the local branch is never forced"
        )
    checked_out = subprocess.run(
        ["git", "-C", str(worktree), "symbolic-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if checked_out.returncode != 0 or checked_out.stdout.strip() != (
        f"refs/heads/{branch}"
    ):
        raise RuntimeError(
            f"refusing to sync back {branch}: worktree {worktree} has "
            f"{checked_out.stdout.strip() or 'a detached HEAD'!r} checked out, "
            f"not refs/heads/{branch}; merge --ff-only would land on the "
            "wrong branch"
        )
    subprocess.run(
        ["git", "-C", str(worktree), "merge", "--ff-only", "FETCH_HEAD"],
        check=True,
    )
    return "fast_forwarded"
