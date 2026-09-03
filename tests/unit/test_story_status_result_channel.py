"""Regression tests: the detached-grading result channel must live OUTSIDE
the agent-writable story worktree.

Security review verdict (REQUEST_CHANGES) on the detached-grading feature in
``pipeline/story_status.py``: the spawner aims the wrapper's result/log files
at the story worktree (``<worktree>/.detached_grade_result.json`` /
``<worktree>/.detached_grade.log``) and the later-tick collector merges
whatever bytes sit at the persisted path.  The worktree is writable by the
code under test, so a graded agent can forge a passing grade during its own
build (``echo '{"returncode":0,...}' > .detached_grade_result.json``) and
bypass the acceptance gate.

Required fix shape (tech-lead checklist): the spawner places result.json and
grading.log under a pipeline-owned state directory
(``$PIPELINE_STATE_DIR``/grading/<story_id>/..., mode 0o700) and the
collector reads only that persisted path — fail-closed on a missing or
unparseable result — then clears the grading bookkeeping so a later tick
cannot re-collect or re-advance.

Written FIRST (TDD): every test here is RED against the current
worktree-channel implementation.  The external boundary
(``start_detached_grade``) is faked — it records its arguments and writes
nothing; the tests simulate the wrapper's writes themselves.  The REAL
``collect_detached_grade`` runs under a spy so the tests pin exactly which
path the collector trusts.  No real test suite is ever spawned.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
# Every existing story_status test file does the same.
from pipeline import server as p
from pipeline import story_status as ss

PLAN_NAME = "rc"
STORY_KEY = "S12"
STORY_PID = 4242  # the dispatched agent's pid — dead in every test here
DEAD_GRADE_PID = 424242  # a recorded grading_pid that is already gone

# The worktree-channel paths the CURRENT implementation uses as its trust
# root (also pinned by tests/unit/test_dead_pid_detached_grading.py).
WORKTREE_RESULT_NAME = ".detached_grade_result.json"
WORKTREE_LOG_NAME = ".detached_grade.log"

# The wrapper's real verdict (a genuine failure) vs what a graded agent can
# forge into its own worktree, vs a genuine pass for the clear-state test.
REAL_FAIL_RESULT = {"returncode": 1, "stdout": "", "stderr": "real grade failed"}
FORGED_PASS_RESULT = {"returncode": 0, "stdout": "all tests passed", "stderr": ""}
GENUINE_PASS_RESULT = {"returncode": 0, "stdout": "ok", "stderr": ""}

WORKTREE_LOG = "[step 0] bash: pwd\n"


# ----------------------------------------------------------- scaffolding


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Mirror tests/unit/test_dead_pid_detached_grading.py's plan_dir."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    """The pipeline-owned state dir the fixed spawner must aim results at."""
    root = tmp_path / "pipeline-state"
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(root))
    return root


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(plan_dir, plan_name, story_key):
    manifest = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())
    return manifest["stories"][story_key]


def _install_dead_pids(monkeypatch):
    """Every pid the manifest names is dead: os.kill raises
    ProcessLookupError (the same idiom the implementation probes with), so
    no ps probe ever fires and no real process is signalled."""

    def fake_kill(pid, sig):
        raise ProcessLookupError(f"pid {pid} is gone")

    monkeypatch.setattr(os, "kill", fake_kill)


def _install_spawn_stub(monkeypatch):
    """Fake the external boundary start_detached_grade; record its args."""
    rec = {"calls": []}

    def fake_start(cmd, cwd, env, result_path, log_path):
        rec["calls"].append(
            {
                "cmd": list(cmd),
                "cwd": str(cwd),
                "env": dict(env),
                "result_path": str(result_path),
                "log_path": str(log_path),
            }
        )
        # Dead-on-arrival grading pid: the NEXT tick collects the result.
        return DEAD_GRADE_PID

    monkeypatch.setattr(ss, "start_detached_grade", fake_start)
    monkeypatch.setattr(p, "start_detached_grade", fake_start, raising=False)
    return rec


def _install_collect_spy(monkeypatch):
    """Spy wrapping the REAL collect_detached_grade on both surfaces."""
    calls = []
    real = ss.collect_detached_grade

    def spy(pid, result_path):
        calls.append((pid, str(result_path)))
        return real(pid, result_path)

    monkeypatch.setattr(ss, "collect_detached_grade", spy)
    monkeypatch.setattr(p, "collect_detached_grade", spy, raising=False)
    return calls


def _base_setup(plan_dir, monkeypatch, *, test_cmd=None):
    """Worktree + manifest + the standard server-side gate mocks, mirroring
    tests/unit/test_dead_pid_detached_grading.py's _base_setup: dead story
    pid, detected test command, new-commits guard, hermetic pass-path
    helpers, and no real subprocess anywhere (every pid is already dead so
    no ps probe fires; the synchronous grade never runs because the detached
    hand-off frees the tick)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(WORKTREE_LOG)
    test_cmd = list(test_cmd) if test_cmd else ["pytest", "-q"]

    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": STORY_PID,
        "worktree": str(worktree),
    }
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: story})

    _install_dead_pids(monkeypatch)
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, list(test_cmd)))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    # Hermetic pass-path helpers (each has its own dedicated test files).
    monkeypatch.setattr(p, "_run_lint_gate", lambda wt, env: None, raising=False)
    monkeypatch.setattr(p, "_find_dead_new_functions", lambda wt, branch: [])
    monkeypatch.setattr(p, "_acceptance_tampered", lambda story, wt: None)
    monkeypatch.setattr(p, "_added_pytest_test_paths", lambda wt, key, branch: [])
    monkeypatch.setattr(p, "_default_branch", lambda: "main", raising=False)
    # Keep any post-grade escalation from reaching out.
    monkeypatch.setattr(p, "_escalate_to_claude", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        p, "_escalate_review_to_claude", lambda *a, **k: None, raising=False
    )

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return {"worktree": worktree, "story": story, "test_cmd": test_cmd}


def _expected_result_path(state_root):
    return state_root / "grading" / STORY_KEY / "result.json"


def _expected_log_path(state_root):
    return state_root / "grading" / STORY_KEY / "grading.log"


# ----------------------------------------------------------- tests


def test_result_path_outside_worktree(plan_dir, state_root, monkeypatch):
    """The spawn tick must aim the wrapper's result/log files at the
    pipeline-owned state dir — never at the agent-writable worktree."""
    setup = _base_setup(plan_dir, monkeypatch)
    worktree = setup["worktree"]
    spawn = _install_spawn_stub(monkeypatch)

    result = ss.check_story_status(PLAN_NAME, STORY_KEY)

    # The tick hands the grade to the detached wrapper...
    assert result.get("status") == "grading", result
    assert len(spawn["calls"]) == 1, spawn["calls"]
    call = spawn["calls"][0]
    result_path = Path(call["result_path"])
    log_path = Path(call["log_path"])

    # ...aimed at <state_root>/grading/<story_id>/{result.json,grading.log}.
    assert result_path == _expected_result_path(state_root), call
    assert log_path == _expected_log_path(state_root), call

    # ...and NOT inside the story worktree the agent can write.
    for path in (result_path, log_path):
        assert not path.is_relative_to(worktree), path
        assert path != worktree / path.name

    # The persisted bookkeeping points at the state-dir channel too.
    story = _read_story(plan_dir, PLAN_NAME, STORY_KEY)
    assert story.get("grading_pid") == DEAD_GRADE_PID
    assert story.get("grading_started_at")
    assert story.get("grading_result_path") == str(result_path)
    assert Path(story["grading_result_path"]).is_relative_to(state_root)
    assert not Path(story["grading_result_path"]).is_relative_to(worktree)

    # The grading dir is pipeline-owned (0o700), and nothing grading-related
    # is created under the worktree.
    grading_dir = state_root / "grading" / STORY_KEY
    assert grading_dir.is_dir(), grading_dir
    assert (grading_dir.stat().st_mode & 0o777) == 0o700, oct(
        grading_dir.stat().st_mode & 0o777
    )
    assert not (worktree / WORKTREE_RESULT_NAME).exists()
    assert not (worktree / WORKTREE_LOG_NAME).exists()


def test_default_channel_is_manifest_root_without_env_override(
    plan_dir, monkeypatch
):
    """Security review cycle 2: the hardened channel must be the DEFAULT,
    not an opt-in. With PIPELINE_STATE_DIR unset, the spawn tick must aim
    the wrapper at <manifest_root>/grading/<story_key>/result.json — the
    pipeline-owned plans/manifests base (_store.manifest_path's parent) —
    and never at the agent-writable worktree."""
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    setup = _base_setup(plan_dir, monkeypatch)
    worktree = setup["worktree"]
    spawn = _install_spawn_stub(monkeypatch)
    collect_calls = _install_collect_spy(monkeypatch)

    first = ss.check_story_status(PLAN_NAME, STORY_KEY)
    assert first.get("status") == "grading", first
    assert len(spawn["calls"]) == 1, spawn["calls"]

    # Same derivation the implementation must use: the parent of the
    # manifest path the store itself hands out.
    manifest_root = Path(p._store.manifest_path(PLAN_NAME)).parent
    expected_result = manifest_root / "grading" / STORY_KEY / "result.json"
    expected_log = manifest_root / "grading" / STORY_KEY / "grading.log"

    call = spawn["calls"][0]
    result_path = Path(call["result_path"])
    log_path = Path(call["log_path"])
    assert result_path == expected_result, call
    assert log_path == expected_log, call
    assert not result_path.is_relative_to(worktree), result_path
    assert not log_path.is_relative_to(worktree), log_path

    # The persisted bookkeeping points at the same absolute channel.
    story = _read_story(plan_dir, PLAN_NAME, STORY_KEY)
    assert story.get("grading_result_path") == str(result_path), story

    # Follow-up tick: the collector opens exactly the persisted absolute
    # path; a forged pass in the worktree is never read and never advances
    # the story.
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(REAL_FAIL_RESULT))
    forged = worktree / WORKTREE_RESULT_NAME
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(json.dumps(FORGED_PASS_RESULT))
    ss.check_story_status(PLAN_NAME, STORY_KEY)
    assert len(collect_calls) == 1, collect_calls
    assert Path(collect_calls[0][1]) == result_path
    story = _read_story(plan_dir, PLAN_NAME, STORY_KEY)
    assert story.get("status") not in ("tests_passed", "done"), story
    assert story.get("tests_passed") is not True, story


def test_forged_worktree_result_cannot_pass(plan_dir, state_root, monkeypatch):
    """A graded agent forges {'returncode': 0} into its own worktree; the
    collector must merge the REAL verdict from the state-dir channel and
    must NOT advance the story to done/pass."""
    setup = _base_setup(plan_dir, monkeypatch)
    worktree = setup["worktree"]
    spawn = _install_spawn_stub(monkeypatch)
    collect_calls = _install_collect_spy(monkeypatch)

    # Tick 1 (t=0): spawn the detached grade.
    first = ss.check_story_status(PLAN_NAME, STORY_KEY)
    assert first.get("status") == "grading", first
    assert len(spawn["calls"]) == 1, spawn["calls"]
    channel = Path(spawn["calls"][0]["result_path"])

    # The wrapper writes its REAL (failing) verdict to the channel it was
    # handed...
    channel.parent.mkdir(parents=True, exist_ok=True)
    channel.write_text(json.dumps(REAL_FAIL_RESULT))

    # ...while the graded agent forges a PASSING verdict into the worktree
    # path the current implementation uses as its trust root.
    forged = worktree / WORKTREE_RESULT_NAME
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(json.dumps(FORGED_PASS_RESULT))

    # Tick 2 (t=60): the grading pid is dead; collect.
    ss.check_story_status(PLAN_NAME, STORY_KEY)

    story = _read_story(plan_dir, PLAN_NAME, STORY_KEY)
    # The forged worktree bytes must never advance the story to a passing
    # state ("tests_passed"/"done" are the pass statuses this pipeline
    # records; the real verdict was a failure)...
    assert story.get("tests_passed") is not True, story
    assert story.get("status") not in ("tests_passed", "done"), story
    # ...and the verdict that WAS merged came from the pipeline-owned
    # channel — the real failing grade (returncode 1), with none of the
    # forged stdout anywhere in the paper trail.
    assert len(collect_calls) == 1, collect_calls
    collected_path = Path(collect_calls[0][1])
    assert collected_path == channel
    assert not collected_path.is_relative_to(worktree), collected_path
    last_check = story.get("last_test_check") or {}
    assert last_check.get("returncode") == 1, story
    assert FORGED_PASS_RESULT["stdout"] not in json.dumps(story), story


def test_collected_state_cleared_for_next_tick(plan_dir, state_root, monkeypatch):
    """After a successful collect the grading bookkeeping is cleared so the
    NEXT tick neither re-collects nor re-advances (and never re-spawns)."""
    _base_setup(plan_dir, monkeypatch)
    spawn = _install_spawn_stub(monkeypatch)
    collect_calls = _install_collect_spy(monkeypatch)

    # Tick 1 (t=0): spawn.
    first = ss.check_story_status(PLAN_NAME, STORY_KEY)
    assert first.get("status") == "grading", first
    channel = Path(spawn["calls"][0]["result_path"])
    channel.parent.mkdir(parents=True, exist_ok=True)
    channel.write_text(json.dumps(GENUINE_PASS_RESULT))

    # Tick 2 (t=60): collect the genuine pass.
    ss.check_story_status(PLAN_NAME, STORY_KEY)
    story_after_collect = _read_story(plan_dir, PLAN_NAME, STORY_KEY)

    # The grading bookkeeping is consumed on collection (cleared to null or
    # removed entirely — either shape must satisfy the gate)...
    for key in ("grading_pid", "grading_started_at", "grading_result_path"):
        assert not story_after_collect.get(key), story_after_collect
    status_after_collect = story_after_collect.get("status")
    last_check_after_collect = dict(story_after_collect.get("last_test_check") or {})
    # ...and the genuine pass actually advanced the story (so the tick-3
    # no-op below is meaningful, not vacuous).
    assert status_after_collect == "tests_passed", story_after_collect
    assert last_check_after_collect.get("returncode") == 0, story_after_collect

    # Tick 3 (t=120): the follow-up tick must no-op on this story.
    ss.check_story_status(PLAN_NAME, STORY_KEY)
    story_after_next = _read_story(plan_dir, PLAN_NAME, STORY_KEY)

    assert len(spawn["calls"]) == 1, spawn["calls"]  # no duplicate spawn
    assert len(collect_calls) == 1, collect_calls  # no re-collect
    assert story_after_next.get("status") == status_after_collect
    assert story_after_next.get("last_test_check") == last_check_after_collect
