"""Git/WIP/suite impls extracted from the local oracle agent module (LAO-GIT).

The five functions below were moved VERBATIM from the oracle module
(scripts/local_agent_oracle.py: git, exclude_runtime_artifacts,
worktree_dirty, auto_commit, _full_suite_result), renamed to
``<name>_impl`` with ``origin`` added as the first parameter.

Why ``origin``: the oracle module is file-execed under MULTIPLE module
names in one pytest process (the shared test helper's
"local_agent_oracle", the acceptance fixtures' per-param variants). A
proxy resolving one canonical sys.modules name cannot route to the right
instance. Each delegating wrapper in the oracle module therefore passes
its own module's ``globals()`` dict, and every oracle-module-owned free
variable below is read as ``origin["NAME"]`` at call time.
``monkeypatch.setattr(mod, "NAME", fake)`` writes into that same dict, so
both reads AND re-binds land on the instance the test actually patched
(the refined variant of pipeline/service.py's ``_ServerRef``
call-time-resolution pattern).

This module imports ONLY the stdlib — it must never import the oracle
module or its config module (no cycles, ever). It is the oracle twin of
scripts/local_agent_git.py: near-identical, but separately maintained —
the bodies here are the oracle module's own (auto_commit's message shape
and _full_suite_result's subprocess.run calls differ from the la twin's).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def git_impl(origin, *args):
    return subprocess.run(["git", *args], check=False, cwd=origin["CWD"], capture_output=True, text=True)


def exclude_runtime_artifacts_impl(origin) -> None:
    """Keep dispatch runtime junk out of git. agent.log lives *inside* the
    worktree and is written live, so without this it would (a) make the tree
    perpetually "dirty" — tripping commit-enforcement on every `done` — and
    (b) get swept into commits by `git add -A` and carried into the eventual
    merge. Same for pytest's __pycache__/*.pyc. Written to git's real
    info/exclude path, which `git rev-parse --git-path` resolves correctly
    whether .git is a directory (plain repo) or a file (a `git worktree`)."""
    rel = origin["git"]("rev-parse", "--git-path", "info/exclude").stdout.strip()
    if not rel:
        return
    if os.path.isabs(rel):
        path = Path(rel)
    elif (origin["CWD"] / ".git").is_dir() and not rel.startswith(".git"):
        # git returned a path relative to the git dir (e.g. "info/exclude");
        # resolve it against the .git directory under the worktree root.
        path = origin["CWD"] / ".git" / rel
    else:
        path = origin["CWD"] / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        additions = [p for p in ("agent.log", "__pycache__/", "*.pyc", ".agent_transcript.json", ".agent_done", ".agent_done.tmp", ".agent_done.consumed") if p not in existing]
        if additions:
            path.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "")
                            + "\n".join(additions) + "\n")
    except OSError:
        pass


def worktree_dirty_impl(origin) -> bool:
    return bool(origin["git"]("status", "--porcelain").stdout.strip())


def auto_commit_impl(origin, reason: str) -> None:
    origin["git"]("add", "-A")
    origin["git"]("commit", "-m", reason)


def _full_suite_result_impl(origin) -> tuple[bool, str, str | None]:
    """Gate-aware wrapper around the original _full_suite_result logic.
    Returns (passed, tail, gate)."""
    test_dir, test_cmd = origin["p"].detect_test_command(origin["CWD"])
    if not test_cmd:
        return True, "", None
    argv = test_cmd
    needs_heavy = bool(argv) and origin["p"]._is_heavy(argv)

    def _run_once():
        if needs_heavy:
            with origin["p"]._heavy_lock():
                return subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)  # noqa: PLW1510
        return subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)  # noqa: PLW1510

    r = _run_once()
    if r.returncode != 0:
        # A red suite is re-run ONCE before it rejects done: a green retry
        # proves the failure was not this story's change (a stale recorded
        # failure, a flake, or a transient collision with another agent's
        # run), so it must not reject. A reproducible failure still rejects,
        # with the FIRST run's tail (it carries the real failure text) -
        # unless every failure in it was already failing in the pre-dispatch
        # baseline snapshot (see _baseline_only_failures).
        retry = _run_once()
        if retry.returncode != 0 and not _baseline_only_failures(origin, r.stdout):
            return False, (r.stdout + r.stderr)[-500:], "test"
    lint = origin["p"].detect_lint_command(origin["CWD"])
    if lint is not None:
        lint_dir, lint_cmd = lint
        lr = subprocess.run(lint_cmd, check=False, cwd=lint_dir, capture_output=True, text=True)
        if lr.returncode != 0:
            return False, (lr.stdout + lr.stderr)[-500:], "lint"
    return True, "", None
def _baseline_only_failures(origin, stdout: str) -> bool:
    """Whether every failure in ``stdout`` was already failing in the
    pre-dispatch baseline snapshot recorded for this worktree.

    dispatch.py writes that snapshot's failing node ids into
    ``.dispatch_baseline_test_checked`` before the agent's first run. A rework
    round on a repo with pre-existing, unrelated failures could otherwise never
    reach a green full suite: it would keep rejecting ``done`` until the
    suite-reject cap parked it, even though the tick grading that same round
    exempts exactly those failures.

    Fails closed on every unjustifiable case - no marker, a legacy "ok" marker,
    an unreadable or empty baseline, or a single failure the baseline does not
    also name - so a baseline we cannot read exempts nothing.
    """
    try:
        payload = json.loads(
            (Path(origin["CWD"]) / ".dispatch_baseline_test_checked").read_text(
                encoding="utf-8"
            )
        )
        baseline_ids = set(payload["failed_node_ids"])
    except Exception:  # noqa: BLE001 (an unreadable baseline exempts nothing)
        return False
    if not baseline_ids:
        return False
    try:
        run_ids = origin["p"].failed_node_ids(stdout)
    except Exception:  # noqa: BLE001 (an unparseable run exempts nothing)
        return False
    return bool(run_ids) and set(run_ids) <= baseline_ids
