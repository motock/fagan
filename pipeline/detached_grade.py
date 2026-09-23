"""Detached-grade primitives for the scheduler-tick recovery path.

Extracted verbatim from the scheduler-tick status module (behavior-preserving
file move).  The rebound status check resolves these bare names against
pipeline.server's namespace, so the original module re-exports them and keeps
exporting them onto ``_server`` at import time.
"""

import json
import os
import subprocess
import sys

from .build_detect import failed_node_ids


def _baseline_exempted_failures(story: dict, test_result) -> list[str] | None:
    """The failing node ids of this run that the recorded pre-dispatch
    baseline was ALREADY failing, or None when this run must not be
    exempted.

    None (never an empty list) in every case the exemption cannot be
    justified, so the caller's red run stays red - this is a fail-closed
    check, and a run that reports a single failure or error the baseline did
    not has to keep rejecting:

    * no recorded baseline, or one that was not itself failing;
    * a baseline with no parseable failing node ids (a non-pytest runner,
      or a truncated payload) - nothing to compare against;
    * a run whose own failures cannot be parsed - an unparseable red run is
      a red run;
    * a run reporting any failing or erroring node id the baseline did not.

    Returns the exempted node ids otherwise, so the caller can record
    exactly what it waved through.
    """
    baseline = story.get("baseline_test_check")
    if not isinstance(baseline, dict):
        return None
    if baseline.get("returncode") in (None, 0):
        return None
    baseline_ids = baseline.get("failed_node_ids")
    if not baseline_ids:
        return None
    run_ids = failed_node_ids(getattr(test_result, "stdout", "") or "")
    if not run_ids:
        return None
    if not set(run_ids) <= set(baseline_ids):
        return None
    return run_ids


GRADE_WRAPPER = """\
import json
import subprocess
import sys

cmd = json.loads(sys.argv[1])
result_path = sys.argv[2]
log_path = sys.argv[3]
proc = subprocess.run(cmd, capture_output=True, text=True)
if proc.returncode != 0:
    # A red suite is re-run ONCE before the grade rejects the story: a green
    # retry proves the failure was not this story's change (a transient
    # collision with another agent's run, a flaky test, or a stale recorded
    # failure), so it must not reject. A reproducible failure still rejects,
    # reported with the FIRST run's output - the first run is the one that
    # carries the real failure text.
    first = proc
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        proc = first
payload = {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
with open(result_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(proc.stdout)
    fh.write(proc.stderr)
"""


def start_detached_grade(
    cmd: list[str], cwd: str, env: dict, result_path: str, log_path: str
) -> int:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            GRADE_WRAPPER,
            json.dumps(cmd),
            str(result_path),
            str(log_path),
        ],
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def collect_detached_grade(pid: int, result_path: str) -> dict | None:
    alive = True
    try:
        os.kill(pid, 0)
        # os.kill succeeds for zombie (defunct) processes too — check ps stat
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
        stat = ps.stdout.strip()
        if not stat or stat.startswith("Z"):
            alive = False
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    except (OSError, subprocess.SubprocessError):
        alive = True
    if alive:
        return None
    fail_closed = {
        "returncode": 1,
        "stdout": "",
        "stderr": "detached grade exited without writing a result",
    }
    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return fail_closed
    if not isinstance(data, dict):
        return fail_closed
    return data


