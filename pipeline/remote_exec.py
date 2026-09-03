"""Local supervisor for one off-box agent run.

``remote_exec.main`` is the entrypoint the dispatch driver calls with four
required flags (``--worktree``, ``--remote-url``, ``--host``,
``--spec-file``). It reads the dispatch spec JSON, prepares the remote side
via ``remote_sync.ensure_remote_worktree``, runs the harness argv on the GPU
box over a single ssh argument built by ``build_remote_shell``, streams the
harness output through this process's own stdout/stderr (the execution seam
already points those at the caller's log file - this module never opens,
truncates, or writes a log of its own), and brings the story commits back
with ``remote_sync.sync_back_commits``.

Exit-code contract: the harness returncode is the result, except that a
sync-back failure on an otherwise-successful run is reported as 1 - a story
whose commits cannot come back is not a success even though the agent
exited 0. A sync-back failure never masks a nonzero harness exit code, and
an unreachable host fails loudly (the ``CalledProcessError`` from
``remote_sync._ssh_run`` propagates) instead of being reported as a harness
failure.

Security: ``build_remote_shell`` interpolates every dynamic value through
``shlex.quote`` because the string it builds executes on the remote host;
quoting is a security control, not cosmetics.
"""

from __future__ import annotations

import argparse
import functools
import json
import shlex
import subprocess
import sys
from pathlib import Path

from pipeline import remote_sync


def build_remote_shell(cmd: list[str], env: dict | None, remote_cwd: str) -> str:
    """Build the single shell string handed to one ssh argument.

    The shape is ``cd <remote_cwd> && env K=V... <cmd...>`` with every
    dynamic value passed through ``shlex.quote``; an absent or empty
    ``env`` omits the ``env`` prefix entirely.
    """
    parts = [f"cd {shlex.quote(remote_cwd)}"]
    tail = ""
    if env:
        assignments = " ".join(
            shlex.quote(f"{key}={value}") for key, value in env.items()
        )
        tail = f"env {assignments} "
    tail += " ".join(shlex.quote(element) for element in cmd)
    return f"{parts[0]} && {tail}"


def main(argv: list[str]) -> int:
    """Run one harness dispatch off-box and return the harness exit code."""
    parser = argparse.ArgumentParser(
        description="Supervise one agent harness run on a remote host."
    )
    parser.add_argument("--worktree", required=True, help="local story worktree")
    parser.add_argument("--remote-url", required=True, dest="remote_url")
    parser.add_argument("--host", required=True, help="ssh host to run on")
    parser.add_argument("--spec-file", required=True, dest="spec_file")
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.spec_file).read_text())
    cmd = spec["cmd"]
    env = spec["env"]
    branch = spec["branch"]
    remote_cwd = Path(spec["remote_cwd"])
    worktree = Path(args.worktree)

    remote_sync.ensure_remote_worktree(
        worktree,
        branch,
        args.remote_url,
        remote_cwd=remote_cwd,
        run_remote=functools.partial(remote_sync._ssh_run, args.host),
    )

    proc = subprocess.Popen(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            args.host,
            build_remote_shell(cmd, env, str(remote_cwd)),
        ]
    )
    returncode = proc.wait()

    try:
        remote_sync.sync_back_commits(worktree, branch, args.remote_url)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"sync-back failed for {branch}: {exc}", file=sys.stderr)
        if returncode == 0:
            return 1
    return returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))