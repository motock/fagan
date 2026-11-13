"""Acceptance-oracle runs must never write into the live plan store.

ROOT CAUSE (live store carried a stray ``acc-plan`` record, status todo, no
owner, mtime 2026-11-12)
-------------------------------------------------------------------------
``tests/acceptance_escalation_repo_root.py`` drives ``pipeline.escalation``
directly. Both escalation paths finish with
``_store.records().put_manifest("default", plan_name, manifest)``, and the
store resolves ``PLAN_DIR`` through the server's live binding at call time.
That file never pinned ``PLAN_DIR`` to ``tmp_path``, so the ``acc-plan``
manifest was written into whatever plans directory the process imported
(the operator's real store under the scheduler).

This guard runs the acceptance files in a child pytest process whose
``PLAN_DIR`` points at an empty sentinel directory, then asserts the sentinel
is still empty. It fails while any file in that set writes into the ambient
store, and it is the regression guard for the fixture pin.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ORACLE_FILES = [
    "tests/acceptance_escalation_repo_root.py",
    "tests/unit/test_pipeline_mcp_server_usage_visibility_and_locks.py",
]


@pytest.mark.parametrize("oracle_file", ORACLE_FILES)
def test_oracle_run_writes_no_manifest_into_the_ambient_plan_dir(
    tmp_path, oracle_file
):
    sentinel = tmp_path / "live-plans"
    sentinel.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env["PLAN_DIR"] = str(sentinel)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
         "-p", "no:cacheprovider", oracle_file],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert sorted(p.name for p in sentinel.iterdir()) == []
