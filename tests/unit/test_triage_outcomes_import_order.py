"""Loading the resolution recorder first must not hit an import cycle.

``pipeline.triage_outcomes`` is imported by ``pipeline.service_story_ops``, which
``pipeline.service`` imports. Each module must load in a fresh interpreter on its
own, or the decision-record tool and the unpark CLI fail at import time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module",
    ["pipeline.triage_outcomes", "pipeline.unpark", "pipeline.decision_record_tool"],
)
def test_module_imports_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
