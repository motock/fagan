"""The image entrypoint - not a compose shim - starts and supervises the daemon.

``deploy/README.md`` promises that ``ENTRYPOINT`` starts the scheduler daemon
alongside the dashboard and the MCP transport, and that
``PIPELINE_SCHEDULER_DAEMON=0`` turns it off. These tests hold the code to that
promise, and hold ``deploy/compose.yaml`` to being a plain hand-off: an
unsupervised ``&`` in an orchestrator shim leaves a dead daemon inside a
container that still looks healthy.

The runtime tests run ``deploy/entrypoint.sh`` under ``bash`` with a stub
``python`` first on ``PATH``. The stub records its argv, so "the daemon was
started" and "the daemon was never started" are both observations rather than
greps.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "deploy" / "entrypoint.sh"
COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"

TIMEOUT_SECONDS = 60

# A stand-in for `python`: it appends its argv to $STUB_ARGV_LOG and then
# behaves like the process the entrypoint asked for. The scheduler daemon exits
# 3 after a second; everything else outlives the supervision loop's first
# checks, so only the daemon's own exit can end the run.
STUB_PYTHON = """#!/bin/bash
printf '%s\\n' "$*" >> "$STUB_ARGV_LOG"
case "$*" in
    *pipeline.scheduler_daemon*) sleep 1; exit 3 ;;
    *) sleep "${STUB_OTHER_SLEEP:-30}"; exit 0 ;;
esac
"""


def _text(path: Path) -> str:
    assert path.is_file(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


def _stub_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A PATH whose first `python` is the recording stub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python"
    stub.write_text(STUB_PYTHON, encoding="utf-8")
    stub.chmod(0o755)

    argv_log = tmp_path / "argv.log"
    argv_log.write_text("", encoding="utf-8")

    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "STUB_ARGV_LOG": str(argv_log),
        "STUB_OTHER_SLEEP": "30",
        # `shared` skips the OIDC client-secret mount assertion, which is not
        # what this test is about.
        "PIPELINE_IDENTITY_PROVIDER": "shared",
        "PIPELINE_MCP_TRANSPORT": "streamable-http",
        "PLAN_DIR": str(plans),
        "WORKTREE_ROOT": str(worktrees),
    }
    return env, argv_log


def _run_entrypoint(env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def test_compose_no_longer_starts_the_daemon_itself() -> None:
    """The shim hands over; the image entrypoint owns the process tree."""
    text = _text(COMPOSE)
    assert "pipeline.scheduler_daemon" not in text, (
        "deploy/compose.yaml starts the scheduler daemon in its entrypoint shim; "
        "an unsupervised `&` there dies unnoticed inside a healthy-looking "
        "container - deploy/entrypoint.sh supervises it instead"
    )


def test_entrypoint_starts_and_supervises_the_daemon() -> None:
    script = _text(ENTRYPOINT)
    assert "pipeline.scheduler_daemon" in script, (
        "deploy/entrypoint.sh never starts the scheduler daemon, although "
        "deploy/README.md says the ENTRYPOINT does"
    )
    assert "scheduler daemon exited" in script, (
        "deploy/entrypoint.sh does not check the scheduler daemon in its "
        "supervision loop, so a dead daemon leaves the container running"
    )


def test_dead_daemon_takes_the_container_down(tmp_path: Path) -> None:
    env, argv_log = _stub_environment(tmp_path)
    result = _run_entrypoint(env, tmp_path)

    assert result.returncode != 0, (
        "the entrypoint exited 0 after its scheduler daemon died; a container "
        f"whose daemon is gone must not report success (stderr: {result.stderr!r})"
    )
    assert "scheduler daemon exited" in result.stderr, (
        "the entrypoint did not report the daemon's death "
        f"(stderr: {result.stderr!r})"
    )
    assert "pipeline.scheduler_daemon" in argv_log.read_text(encoding="utf-8"), (
        "the stub python was never asked to run the scheduler daemon"
    )


def test_daemon_can_be_switched_off(tmp_path: Path) -> None:
    env, argv_log = _stub_environment(tmp_path)
    env["PIPELINE_SCHEDULER_DAEMON"] = "0"
    env["STUB_OTHER_SLEEP"] = "2"
    result = _run_entrypoint(env, tmp_path)

    assert "pipeline.scheduler_daemon" not in argv_log.read_text(encoding="utf-8"), (
        "PIPELINE_SCHEDULER_DAEMON=0 must start nothing; the daemon runs outside "
        "the container instead (one daemon per PLAN_DIR)"
    )
    assert result.returncode == 0, (
        "with the daemon switched off the entrypoint must still supervise the "
        f"dashboard and the MCP transport (stderr: {result.stderr!r})"
    )


def test_unknown_switch_value_fails_before_anything_starts(tmp_path: Path) -> None:
    env, argv_log = _stub_environment(tmp_path)
    env["PIPELINE_SCHEDULER_DAEMON"] = "yes"
    result = _run_entrypoint(env, tmp_path)

    assert result.returncode != 0, "PIPELINE_SCHEDULER_DAEMON=yes must be a start failure"
    assert "must be 0 or 1" in result.stderr, (
        f"the failure must name the variable and its allowed values (stderr: {result.stderr!r})"
    )
    assert argv_log.read_text(encoding="utf-8") == "", (
        "the entrypoint validated PIPELINE_SCHEDULER_DAEMON only after starting a "
        f"process (argv seen: {argv_log.read_text(encoding='utf-8')!r})"
    )
