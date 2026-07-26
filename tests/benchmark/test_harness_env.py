"""Unit tests for harness.py's PIPELINE_BACKEND_REVIEW environment default.

The harness must default the review gate to the cloud Claude reviewer
(PIPELINE_BACKEND_REVIEW=claude) when the invoking shell sets nothing, but
must respect an explicit override so an invoker can trade review quality for
zero Claude usage on a given run (the REVIEW-LOCAL-FALLBACK use case).

Run: pytest tests/benchmark/test_harness_env.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
PIPELINE_REPO = BENCH.parents[1]
VENV_PY = PIPELINE_REPO / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else sys.executable

if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness


def test_default_sets_claude_when_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    harness._set_review_backend_env()
    assert os.environ["PIPELINE_BACKEND_REVIEW"] == "claude"


def test_override_local_is_preserved(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    harness._set_review_backend_env()
    assert os.environ["PIPELINE_BACKEND_REVIEW"] == "local"


def test_override_arbitrary_value_is_preserved(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "some-future-backend")
    harness._set_review_backend_env()
    assert os.environ["PIPELINE_BACKEND_REVIEW"] == "some-future-backend"


def test_mock_cell_still_completes_with_no_override(tmp_path):
    env = dict(os.environ)
    env.pop("PIPELINE_BACKEND_REVIEW", None)
    # Keep the cell hermetic/offline even if the invoking shell has Plane
    # configured globally -- ingest_plan must synthesize local story keys.
    for k in ("PLANE_BASE", "PLANE_API_KEY", "PLANE_PROJECT", "PLANE_WORKSPACE"):
        env.pop(k, None)
    subprocess.run(
        [PY, str(BENCH / "harness.py"), "--task", "token_bucket", "--model", "mock",
         "--trial", "0", "--workdir", str(tmp_path),
         "--timeout", "120", "--tick", "1"],
        check=True, capture_output=True, text=True, env=env,
    )
    cell = tmp_path / "token_bucket__mock__t0"
    result = json.loads((cell / "result.json").read_text())
    assert result["final_status"] == "done"
    assert result["merged"] is True
    assert result["review_verdict"] == "APPROVE"
