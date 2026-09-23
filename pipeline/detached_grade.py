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




def _detached_grade_lifecycle(story, story_key, pid, worktree, manifest, manifest_path, test_cmd, test_dir, test_env):
    """Run check_story_status's detached-grade lifecycle with pipeline.server's globals.

    Rebound onto pipeline.server.__dict__ in pipeline/story_status.py exactly like
    check_story_status, so globals().get("start_detached_grade") /
    globals().get("collect_detached_grade") / globals().get("_untrack_scratchpad")
    resolve to pipeline.server's (pytest-conditional) exports rather than this
    module's own.

    Returns (early_result, test_result): early_result is the dict
    check_story_status must return immediately, else None; test_result is None
    when the caller must grade synchronously.
    """
    grading_pid = story.get("grading_pid")
    grading_alive = False
    if grading_pid is not None:
        try:
            os.kill(grading_pid, 0)
            # os.kill succeeds for zombie (defunct) processes too — check ps
            # stat (same idiom as the story-pid liveness probe above).
            ps = subprocess.run(
                ["ps", "-p", str(grading_pid), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
            )
            grade_stat = ps.stdout.strip()
            grading_alive = bool(grade_stat) and not grade_stat.startswith("Z")
        except ProcessLookupError:
            grading_alive = False
        except PermissionError:
            # The pid exists but we may not signal it: keep polling rather
            # than crash the tick (mirrors collect_detached_grade).
            grading_alive = True
        except (OSError, subprocess.SubprocessError):
            # Probe failure: presume the grade is still in flight so the tick
            # stays read-only.
            grading_alive = True

    # Follow-up ticks must not re-grade an already-graded story: once the
    # post-grade logic has run, the grading bookkeeping is consumed and the
    # story's status has moved off in_progress. Key on that cleared state
    # (not on any leftover result file) so a later tick neither re-collects
    # nor re-spawns a duplicate grade.
    if grading_pid is None and story.get("status") != "in_progress":
        return {"status": story.get("status"), "pid": pid}, None

    test_result = None
    if grading_pid is None:
        # The agent process is gone, so it can no longer clean up after
        # itself: any scratchpad path it tracked with its own `git add -f` /
        # `git commit` is untracked and committed away HERE, before the grade,
        # so a stray scratchpad cannot fail the guard tests, reach review, or
        # land in the merge (where a rebase against another story that tracked
        # it conflicts). Recorded on the manifest for audit; the common case
        # (nothing tracked) records nothing and makes no commit.
        # Resolved through globals() exactly like the detached-grade spawner
        # below: the helper is exported only outside pytest (it shells out to
        # git, so exporting it under pytest would run the real function inside
        # every pre-existing test that pins subprocess.run's call sequence).
        untrack = globals().get("_untrack_scratchpad")
        if untrack is not None:
            _untracked = untrack(str(worktree), story_key)
            if _untracked["paths"]:
                story["scratchpad_untrack"] = {
                    **_untracked,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }

        starter = globals().get("start_detached_grade")
        if starter is not None:
            # Security review (REQUEST_CHANGES): the detached grade's result
            # channel must not live in the agent-writable worktree — the code
            # under test could forge a passing grade there during its own
            # build and bypass the acceptance gate. The hardened channel is
            # the DEFAULT: the state root is the pipeline-owned plans/
            # manifests base (the parent of this story's manifest path), with
            # PIPELINE_STATE_DIR as an override. A relative override resolves
            # against that fixed base — never the scheduler's CWD, since the
            # collect tick may run with a different CWD than the spawn tick.
            # The result/log files live under <state_root>/grading/<story_id>/
            # (mode 0o700) and the collector reads ONLY the persisted path.
            # The guard below refuses to aim the hardened channel back into
            # the worktree, so a regression cannot silently move it back.
            env_root = os.environ.get("PIPELINE_STATE_DIR")
            manifest_root = Path(manifest_path).parent
            if env_root:
                state_root = Path(env_root)
                if not state_root.is_absolute():
                    state_root = manifest_root / state_root
            else:
                state_root = manifest_root
            grading_dir = state_root / "grading" / story_key
            grading_dir.mkdir(parents=True, exist_ok=True)
            grading_dir.chmod(0o700)
            result_path = grading_dir / "result.json"
            log_path = grading_dir / "grading.log"
            _worktree_resolved = Path(worktree).resolve()
            for _artifact in (result_path, log_path):
                if _artifact.resolve().is_relative_to(_worktree_resolved):
                    raise RuntimeError(
                        "detached grade result channel must live outside "
                        "the agent-writable worktree "
                        f"({_worktree_resolved}): {_artifact}"
                    )
            try:
                grade_pid = starter(
                    test_cmd,
                    str(test_dir),
                    test_env,
                    str(result_path),
                    str(log_path),
                )
            except OSError:
                # Fail-closed invariant: a spawn failure must not crash the
                # tick - fall through to the synchronous grade below, which is
                # exactly what this path did before the detached hand-off.
                pass
            else:
                story["grading_pid"] = grade_pid
                story["grading_started_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                story["grading_result_path"] = str(result_path)
                _atomic_write_json(manifest_path, manifest)
                # The tick is free; a LATER tick collects the result.
                return {"status": "grading", "pid": grade_pid}, None
        # No starter reachable (or the spawn failed): the synchronous grade
        # below preserves the pre-detached behavior.
    elif grading_alive:
        # Pure read: no manifest write and no timestamp refresh - refreshing
        # grading_started_at would reset the grading watchdog's clock every
        # poll, so the story would never time out.
        return {"status": "grading", "pid": grading_pid}, None
    else:
        # Follow-up ticks must not re-collect an already-consumed grade: the
        # bookkeeping is deleted below and the story's status has moved off
        # in_progress, so a later tick no-ops here instead of re-reading any
        # file or re-advancing the story.
        if story.get("status") != "in_progress":
            return {"status": story.get("status"), "pid": pid}, None
        collector = globals().get("collect_detached_grade")
        grade_result_path = story.get("grading_result_path")
        collected = None
        if collector is not None and grade_result_path:
            collected = collector(grading_pid, grade_result_path)
        fail_closed = {
            "returncode": 1,
            "stdout": "",
            "stderr": "detached grade exited without writing a result",
        }
        if collected is None:
            collected = dict(fail_closed)
        if collected.get("stderr") == fail_closed["stderr"]:
            # The grade left no readable result (collect returned None, or it
            # returned the sibling's fail-closed shape for a missing/malformed
            # result file). The pid is already dead - nothing to kill - so the
            # only remedy is the grading watchdog: a grade outstanding longer
            # than the threshold is failed with the watchdog noted in stderr
            # instead of polling forever on a wrapper that never wrote a
            # result. A genuinely collected result is never discarded here.
            started_at = story.get("grading_started_at")
            age = None
            if isinstance(started_at, str):
                try:
                    started = datetime.fromisoformat(started_at)
                except (TypeError, ValueError):
                    started = None
                if started is not None and started.tzinfo is not None:
                    age = (
                        datetime.now(timezone.utc) - started
                    ).total_seconds()
            if age is not None and age > DETACHED_GRADE_WATCHDOG_SECONDS:
                collected = dict(fail_closed)
                collected["stderr"] = (
                    f"{fail_closed['stderr']}; grading watchdog fired after "
                    f"{age:.0f}s without a collectable result (threshold "
                    f"{DETACHED_GRADE_WATCHDOG_SECONDS}s)"
                )
        # Hand the collected result to the EXISTING post-grade logic below in
        # the shape it already reads (subprocess.run's return value).
        raw_stdout = collected.get("stdout")
        raw_stderr = collected.get("stderr")
        test_result = subprocess.CompletedProcess(
            test_cmd,
            collected.get("returncode", 1),
            stdout=raw_stdout if isinstance(raw_stdout, str) else "",
            stderr=raw_stderr if isinstance(raw_stderr, str) else "",
        )
        # The bookkeeping is consumed: a future dead-pid re-grade starts clean
        # instead of re-collecting a stale result file. collected_at records
        # when the verdict was merged so later ticks key on the cleared state.
        story.pop("grading_pid", None)
        story.pop("grading_started_at", None)
        story.pop("grading_result_path", None)
        story["collected_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(manifest_path, manifest)

    return None, test_result
