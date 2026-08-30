"""Git/WIP/suite impls extracted from the local dispatch agent module (LA-GIT).

The five functions below were moved VERBATIM from the agent module
(git, exclude_runtime_artifacts, worktree_dirty, auto_wip_commit,
_full_suite_result), renamed to ``<name>_impl`` with ``origin`` added as the
first parameter.

Why ``origin``: the agent module is file-execed under MULTIPLE module
names in one pytest process (the shared test helper's "local_agent",
test_dropped_top_level_vars' "local_agent_dropped_vars", the acceptance
fixtures' per-param variants). A proxy resolving one canonical sys.modules
name cannot route to the right instance. Each delegating wrapper in the
agent module therefore passes its own module's ``globals()`` dict,
and every agent-module-owned free variable below is read as
``origin["NAME"]`` at call time. ``monkeypatch.setattr(mod, "NAME", fake)``
writes into that same dict, so both reads AND re-binds land on the instance
the test actually patched (the refined variant of pipeline/service.py's
``_ServerRef`` call-time-resolution pattern).

This module imports ONLY the stdlib — it must never import the agent module
or its config module (no cycles, ever).
"""
from __future__ import annotations

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


def auto_wip_commit_impl(origin, reason: str) -> None:
    origin["git"]("add", "-A")
    origin["git"]("commit", "-m", f"WIP ({reason})")


def _full_suite_result_impl(origin) -> tuple[bool, str, str | None]:
    """Run the FULL worktree suite (unscoped), for the L1 CI-fail-rework
    done-gate. Mirrors the merge gate's _ci_status_stub runner
    (tests/benchmark/harness.py:623) and the oracle variant's helper:
    detect_test_command + the heavy lock, run the detected command verbatim
    (no acceptance scoping - this agent has no acceptance oracle), return
    (passed, tail[-500:], gate). No detectable test command -> (True, '', None) (nothing
    to fail). Kept in sync with the oracle variant's _full_suite_result.

    Mode 40: once tests pass, also run detect_lint_command (if the repo has
    one) and fold a lint failure into the same (False, tail, 'lint') result - the
    live incident that motivated this was an agent exiting DONE with a
    green suite but a lint-failing CI, because nothing local ever checked
    lint before this. No detected lint command -> unchanged (True, '', None).
    """
    p = origin["p"]
    test_dir, test_cmd = p.detect_test_command(origin["CWD"])
    if not test_cmd:
        return True, "", None
    argv = test_cmd
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-500:], "test"
    lint = p.detect_lint_command(origin["CWD"])
    if lint is not None:
        lint_dir, lint_cmd = lint
        lr = subprocess.run(lint_cmd, check=False, cwd=lint_dir, capture_output=True, text=True)
        if lr.returncode != 0:
            return False, (lr.stdout + lr.stderr)[-500:], "lint"
    return True, "", None