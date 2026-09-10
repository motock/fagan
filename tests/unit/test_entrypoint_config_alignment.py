"""Entrypoint config alignment guard (CFG-E3): shell path vs Python path.

Two mechanisms feed config to a pipeline process:

1. SHELL PATH — ``scripts/pipeline-env.sh`` sources ``$ROOT/.pipeline.env``
   (then ``.dashboard.env``) under ``set -a``; ``scripts/dashboard.sh`` and
   ``scripts/scheduler.sh`` resolve ``ROOT`` and then source the helper.
2. PYTHON PATH — ``pipeline/__init__.py``'s ``_load_env_file`` bootstrap runs
   at import time (inert under pytest, so a bare ``python -c`` subprocess is
   the honest probe) and populates ``os.environ`` before ``pipeline.paths``
   reads ``PLAN_DIR``.

If these two ever disagree about precedence or parsing, the alignment gap
silently returns. These tests drive BOTH mechanisms against the same
temporary ``.pipeline.env`` written into the repo root and assert they yield
identical values.

Test-only story: no production code is touched. If any test here fails with
the two mechanisms DISAGREEING (not both erroring), that is a finding to
report, not something to fix in production code from this dispatch.

xdist safety: the suite runs under ``-n auto`` (pytest-xdist), and racing
workers would otherwise create/delete the shared repo-root ``.pipeline.env``
under each other's subprocesses (the exact hazard documented in
test_pipeline_env_bootstrap.py). Every test in this module therefore holds a
cross-process ``flock`` on a lock file OUTSIDE the repo (so ``git status``
stays clean) for the whole fixture lifecycle — write, subprocess runs, and
teardown — so only one worker touches the repo-root env file at a time. The
fixture snapshots any pre-existing operator ``.pipeline.env`` and restores it
in teardown (even on failure), so a developer's real file is never destroyed
and nothing leaks into later runs.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".pipeline.env"
DEFAULT_PLAN_DIR = "~/.claude/plans"
FILE_PLAN_DIR = "/tmp/alignment-test-plans"
EXPORTED_PLAN_DIR = "/exported/should-lose"

ENV_FILE_CONTENT = f"# temporary alignment-test file\nPLAN_DIR={FILE_PLAN_DIR}\n"

# The shell path, driven exactly the way scripts/dashboard.sh drives it:
# resolve ROOT first, then source the helper (which sources .pipeline.env
# under allexport), then read the value. The echo applies the DOCUMENTED
# default ("~/.claude/plans", pipeline/paths.py) with ~ expanded, because the
# shell helper itself has no default — when no env file sets PLAN_DIR the
# variable is simply unset, and the shell consumer resolves it to the same
# documented default the Python path expands to. When a file IS present the
# default branch never fires and the echo is the file's literal value.
SHELL_CMD = (
    'ROOT="{root}"; set -a; . "$ROOT/scripts/pipeline-env.sh"; set +a; '
    'echo "${{PLAN_DIR:-$HOME/.claude/plans}}"'
)

# The Python path, driven the way launchd drives it: a bare `python -c` with
# no opt-out set. Importing pipeline.paths triggers the package __init__'s
# env-file bootstrap, then reads the constant it resolved.
PYTHON_CMD = "import pipeline.paths; print(pipeline.paths.PLAN_DIR)"


@contextlib.contextmanager
def _repo_env_lock() -> Iterator[None]:
    """Cross-process exclusive lock serializing repo-root .pipeline.env access.

    The lock file lives in the system temp dir (keyed by the repo root) so it
    never shows up in ``git status``. If flock is unavailable (non-POSIX), the
    lock degrades to a no-op rather than failing the suite.
    """
    lock_path = (
        Path(tempfile.gettempdir())
        / f"pipeline-env-alignment-{hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:16]}.lock"
    )
    f = lock_path.open("w")
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _child_env(plan_dir: str | None) -> dict[str, str]:
    """Build a deterministic child environment.

    Always based on the real process env, with the loader opt-out and any
    explicit env-file override removed (the loader must run against the
    repo-root default lookup), and PLAN_DIR set exactly as the test wants it
    (removed entirely when ``plan_dir`` is None) so ambient operator env can
    never make a test pass or fail by accident.
    """
    env = dict(os.environ)
    env.pop("PIPELINE_SKIP_ENV_FILE", None)
    env.pop("PIPELINE_ENV_FILE", None)
    if plan_dir is None:
        env.pop("PLAN_DIR", None)
    else:
        env["PLAN_DIR"] = plan_dir
    return env


def _run_shell(env: dict[str, str]) -> str:
    """Run the shell path and return the echoed PLAN_DIR."""
    proc = subprocess.run(
        ["bash", "-c", SHELL_CMD.format(root=REPO_ROOT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"shell path failed: {proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert lines, "shell path printed nothing"
    return lines[-1]


def _run_python(env: dict[str, str]) -> str:
    """Run the Python path in a subprocess and return the printed PLAN_DIR."""
    proc = subprocess.run(
        [sys.executable, "-c", PYTHON_CMD],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"python path failed: {proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert lines, "python path printed nothing"
    return lines[-1]


@pytest.fixture()
def pipeline_env_file() -> Iterator[Path]:
    """Write a temporary .pipeline.env into the repo root; restore in teardown.

    A pre-existing operator file is snapshotted and rewritten verbatim; when
    none existed the temp file is removed. Runs under the cross-process lock
    so teardown cannot race another xdist worker's subprocess.
    """
    with _repo_env_lock():
        pre_existing = (
            ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else None
        )
        ENV_FILE.write_text(ENV_FILE_CONTENT, encoding="utf-8")
        try:
            yield ENV_FILE
        finally:
            if pre_existing is None:
                ENV_FILE.unlink(missing_ok=True)
            else:
                ENV_FILE.write_text(pre_existing, encoding="utf-8")


@pytest.fixture()
def no_pipeline_env_file() -> Iterator[None]:
    """Ensure NO .pipeline.env exists in the repo root; restore in teardown."""
    with _repo_env_lock():
        pre_existing = (
            ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else None
        )
        ENV_FILE.unlink(missing_ok=True)
        try:
            yield
        finally:
            if pre_existing is not None:
                ENV_FILE.write_text(pre_existing, encoding="utf-8")


class TestEntrypointConfigAlignment:
    def test_shell_path_yields_file_plan_dir(self, pipeline_env_file):
        """SHELL PATH: sourcing the helper yields the file's PLAN_DIR."""
        assert _run_shell(_child_env(None)) == FILE_PLAN_DIR

    def test_python_path_yields_file_plan_dir(self, pipeline_env_file):
        """PYTHON PATH: bare-python import yields the file's PLAN_DIR."""
        assert _run_python(_child_env(None)) == FILE_PLAN_DIR

    def test_shell_and_python_agree(self, pipeline_env_file):
        """AGREEMENT: the two mechanisms yield the SAME PLAN_DIR."""
        shell_value = _run_shell(_child_env(None))
        python_value = _run_python(_child_env(None))
        assert shell_value == FILE_PLAN_DIR
        assert python_value == FILE_PLAN_DIR
        assert shell_value == python_value, (
            f"shell and python paths disagree: {shell_value!r} != {python_value!r}"
        )

    def test_precedence_file_wins_over_exported_env_both_paths(
        self, pipeline_env_file
    ):
        """PRECEDENCE AGREEMENT: with PLAN_DIR already exported to a different
        value in the child environment, BOTH mechanisms still yield the FILE's
        value (the file is durable operator intent) and still agree."""
        env = _child_env(EXPORTED_PLAN_DIR)
        shell_value = _run_shell(env)
        python_value = _run_python(env)
        assert shell_value == FILE_PLAN_DIR, (
            f"shell path let the exported env win over the file: {shell_value!r}"
        )
        assert python_value == FILE_PLAN_DIR, (
            f"python path let the exported env win over the file: {python_value!r}"
        )
        assert shell_value == python_value

    def test_negative_no_env_file_yields_default_and_agreement(
        self, no_pipeline_env_file
    ):
        """NEGATIVE: with no .pipeline.env present, both mechanisms yield the
        default ~/.claude/plans and still agree."""
        assert not ENV_FILE.exists()
        shell_value = _run_shell(_child_env(None))
        python_value = _run_python(_child_env(None))
        # pipeline.paths expands ~ at import time, so the Python path prints
        # the expanded default; the shell echo is compared the same way.
        expected = os.path.expanduser(DEFAULT_PLAN_DIR)
        assert shell_value == expected, (
            f"shell path default drifted: {shell_value!r}"
        )
        assert python_value == expected, (
            f"python path default drifted: {python_value!r}"
        )
        assert shell_value == python_value