import os
import subprocess
from pathlib import Path

import pytest

# Clear PIPELINE_*/LOCAL_AGENT_* env vars at conftest import time, before any
# test module is collected. Several pipeline modules read these via
# os.environ.get(...) ONCE at their own import time (e.g. pipeline.ci's
# PIPELINE_MERGE_CI_GATE, pipeline.config's SESSION_PAUSE_THRESHOLD). When the
# suite runs inside a dispatched agent's own bash tool, the scheduler plist
# sets these (e.g. PIPELINE_MERGE_CI_GATE=0, PIPELINE_PAUSE_THRESHOLD=101),
# which leaks into the pytest subprocess. If a test module outside tests/unit
# (e.g. tests/benchmark) imports such a pipeline module while the var is set,
# the module-level constant binds to the leaked value and pollutes the shared
# module for the rest of the session, making unrelated tests fail on a correct
# codebase. Clearing here, at the suite root, covers every test directory.
_ISOLATED_ENV_PREFIXES = ("PIPELINE_", "LOCAL_AGENT_")

for _key in list(os.environ):
    if _key.startswith(_ISOLATED_ENV_PREFIXES):
        del os.environ[_key]

# CI sets AGENTS_DIR to the repo's checked-in personas (.github/workflows/ci.yml);
# default it to the same value here so a bare `pytest -q` resolves them too.
# setdefault, so an explicit AGENTS_DIR (e.g. a user's ~/.claude/agents) still wins.
os.environ.setdefault("AGENTS_DIR", str(Path(__file__).resolve().parents[1] / "agents"))

# REPO_ROOT is scrubbed by exact name (it carries no PIPELINE_ prefix). The
# scheduler plist exports REPO_ROOT=/nonexistent-repo-root-set-per-plan-only
# (the "set per plan only" sentinel) into every dispatched agent's bash tool,
# and pipeline/server.py binds REPO_ROOT from the env ONCE at import time.
# Plans whose manifest records no repo_root then resolve to that sentinel via
# _repo_root_for, so any test that dispatches without stubbing repo_root fails
# with "refusing to set up a worktree for a missing repository". The suite must
# be hermetic against an ambient REPO_ROOT: tests that need one set it
# themselves (monkeypatch.setenv) or record it in the plan manifest.
if "REPO_ROOT" in os.environ:
    del os.environ["REPO_ROOT"]

# CFG env bootstrap: pipeline/__init__.py reads <repo_root>/.pipeline.env at import time; this opt-out makes that loader inert for the whole pytest run so a developer's repo-root file (e.g. PLAN_DIR written by CFG-D2's standalone-setup.sh) never leaks into the suite. Set AFTER the scrub loop above (it deletes every PIPELINE_* key) and before any pipeline import can occur.
os.environ["PIPELINE_SKIP_ENV_FILE"] = "1"

@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(_ISOLATED_ENV_PREFIXES):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _untracked_root_entries():
    """Untracked top-level entries in the repo root, per git.

    Returns the ``?? `` porcelain lines whose path has no ``/`` except an
    optional trailing one (a top-level file or directory). Nested untracked
    paths and tracked/modified files are ignored. An empty set when git cannot
    be run at all (e.g. a nested pytest with PATH scrubbed).
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    entries = set()
    for line in proc.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        if "/" in path.rstrip("/"):
            continue
        entries.add(path)
    return entries


_root_entries_at_start = None


def pytest_sessionstart(session):
    """Record the repo root's untracked entries before the run (controller only)."""
    global _root_entries_at_start
    if hasattr(session.config, "workerinput"):
        return
    _root_entries_at_start = _untracked_root_entries()


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if it created NEW untracked entries in the repo root."""
    if hasattr(session.config, "workerinput"):
        return
    if _root_entries_at_start is None:
        return
    new_entries = _untracked_root_entries() - _root_entries_at_start
    if new_entries:
        print(
            "ERROR: the test run created untracked files in the repo root: "
            f"{sorted(new_entries)}"
        )
        session.exitstatus = 1
