"""Every documented CLI entrypoint must run when invoked directly.

Import-only tests (``from scripts import install_global_rules``) never exercise
the module-level import graph the way the docs invoke it: pytest puts the repo
root on ``sys.path``, so a module-level ``from pipeline import ...`` always
resolves. A direct ``python scripts/install_global_rules.py`` puts ``scripts/``
- not the repo root - on ``sys.path[0]``, so the same import raises
``ModuleNotFoundError``. These tests exec each entrypoint as a subprocess from a
temporary cwd *outside* the repo, which proves the entrypoint bootstraps its own
``sys.path`` instead of depending on the caller's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ENTRYPOINTS = (
    "app/pipeline_mcp_server.py",
    "scripts/choose_providers.py",
    "scripts/install_checks.py",
    "scripts/install_global_rules.py",
    "scripts/smoke_getting_started.py",
)


@pytest.mark.parametrize("rel", ENTRYPOINTS)
def test_entrypoint_help_succeeds_when_invoked_directly(rel: str, tmp_path: Path) -> None:
    """``--help`` exits 0 with no ModuleNotFoundError from a foreign cwd."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / rel), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{rel} --help exited {proc.returncode} when run directly from {tmp_path}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "ModuleNotFoundError" not in proc.stderr, (
        f"{rel} --help could not import its own dependencies when run directly:\n"
        f"{proc.stderr}"
    )
