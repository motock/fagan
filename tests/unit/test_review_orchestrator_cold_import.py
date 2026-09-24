"""pipeline.review_orchestrator must import cold, without pulling in the server.

It used to run ``import pipeline.server as _server`` at module level for a single
call site inside review_story. pipeline.server imports this module, so a cold
``import pipeline.review_orchestrator`` re-entered it half-initialized and died
with an ImportError. tests/unit/conftest.py pre-imports pipeline.server, which hides
that in-process, so every probe here runs in a subprocess.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def test_review_orchestrator_imports_cold_in_a_fresh_interpreter():
    result = _probe("import pipeline.review_orchestrator")
    assert result.returncode == 0, result.stderr


def test_importing_review_orchestrator_does_not_load_the_server():
    result = _probe("import sys, pipeline.review_orchestrator; print('pipeline.server' in sys.modules)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_server_still_re_exports_the_review_orchestrator_names():
    result = _probe(
        "import pipeline.server as p, pipeline.review_orchestrator as r; "
        "print(p._original_review_story is r._original_review_story, "
        "p._verify_reviewer_auto_fix is r._verify_reviewer_auto_fix)"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True True"


def test_review_orchestrator_first_then_server_still_imports():
    result = _probe("import pipeline.review_orchestrator, pipeline.server")
    assert result.returncode == 0, result.stderr
