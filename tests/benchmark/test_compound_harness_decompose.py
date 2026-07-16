"""Offline self-test for compound_harness.py's guided-decomposition plumbing
(GUIDED_DECOMPOSITION_PLAN.md). Runs compound_harness.py with the `mock`
backend as a subprocess - no model/network required - proving:

1. --decompose cloud/local doesn't break the existing condition-M plumbing
   (still drives to done/merged/groundtruth-passed).
2. The planner path is actually exercised when decompose is on (MockBackend's
   complete() gets called), and NOT exercised when it's off - this is the
   distinction between "the flag is wired" and "the flag merely doesn't
   crash anything".

Run: pytest tests/benchmark/test_compound_harness_decompose.py
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


def _run(condition, decompose="off", decompose_scratchpad="on", workdir=None,
         trial=0, task="ratelimiter_inspect"):
    cell_root = Path(workdir)
    subprocess.run(
        [PY, str(BENCH / "compound_harness.py"), "--task", task,
         "--condition", condition, "--model", "mock", "--trial", str(trial),
         "--workdir", str(cell_root), "--timeout", "120", "--tick", "1",
         "--decompose", decompose, "--decompose-scratchpad", decompose_scratchpad],
        check=True, capture_output=True, text=True, env=dict(os.environ),
    )
    cell = cell_root / f"{task}__{condition}__mock__t{trial}"
    return json.loads((cell / "result.json").read_text())


def test_mock_decompose_off_default_never_invokes_planner(tmp_path):
    r = _run("M", workdir=tmp_path)
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["groundtruth_passed"] is True
    assert r["decompose_planner_calls"] == 0


def test_mock_decompose_cloud_invokes_planner_once_and_still_drives_to_done(tmp_path):
    r = _run("M", decompose="cloud", workdir=tmp_path)
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["groundtruth_passed"] is True
    # One story, dispatched fresh exactly once -> exactly one planner call,
    # not one per tick (guards against re-planning on every advance_pipeline
    # poll instead of once at dispatch).
    assert r["decompose_planner_calls"] == 1


def test_mock_decompose_local_invokes_planner_once_and_still_drives_to_done(tmp_path):
    r = _run("M", decompose="local", workdir=tmp_path)
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["groundtruth_passed"] is True
    assert r["decompose_planner_calls"] == 1
