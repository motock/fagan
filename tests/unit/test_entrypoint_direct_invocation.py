"""
Test that every documented CLI entrypoint runs when invoked directly.

The import‑only tests never exercise the module‑level import graph the way the
docs invoke it. This test runs each entrypoint as a subprocess from a
directory outside the repository, ensuring the entrypoint bootstraps its own
sys.path instead of depending on the caller's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ENTRYPOINTS = [
    "app/pipeline_mcp_server.py",
    "scripts/choose_providers.py",
    "scripts/install_checks.py",
    "scripts/install_global_rules.py",
    "scripts/smoke_getting_started.py",
]

@pytest.mark.parametrize("rel", ENTRYPOINTS)
def test_entrypoint_help_succeeds_when_invoked_directly(tmp_path, rel):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / rel), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"{rel} returned {proc.returncode}\n{proc.stderr}"
    assert "ModuleNotFoundError" not in proc.stderr
