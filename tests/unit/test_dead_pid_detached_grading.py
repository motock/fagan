"""Detached grading wired into ``check_story_status``'s dead-pid fall-through.

Live incident being fixed (2026-09-02): when an in_progress story's dispatch
process dies, ``check_story_status`` re-grades it SYNCHRONOUSLY via
``subprocess.run(test_cmd, ...)`` inside the scheduler tick
(pipeline/story_status.py ~lines 449-467).  ``advance.py:484`` calls
``check_story_status`` for every in_progress story inside the tick, so one
dead story blocked dispatch/review/merge for ALL plans for the duration of a
full suite run (~19 minutes of scheduler silence, observed live).

The sibling story (adp-01, already merged) added the detached-grading
primitives to ``pipeline/story_status.py``: ``GRADE_WRAPPER``,
``start_detached_grade(cmd, cwd, env, result_path, log_path) -> int`` and
``collect_detached_grade(pid, result_path) -> dict | None``.  THIS story wires
them into the dead-pid fall-through of ``check_story_status``:

  1. ``DETACHED_GRADE_WATCHDOG_SECONDS`` — module constant reusing the value
     of ``DISPATCH_WATCHDOG_SECONDS`` (same policy governs both; no new env
     var).
  2. Dead-pid fall-through, BEFORE the existing command-scoping +
     ``subprocess.run`` block, keyed on a new per-story field
     ``story['grading_pid']``:
       - no ``grading_pid``  -> scope the test command exactly as the existing
         code does, build the stripped ``test_env`` dict, aim the result/log
         files at ``worktree/'.detached_grade_result.json'`` and
         ``worktree/'.detached_grade.log'``, call ``start_detached_grade``,
         record ``grading_pid`` / ``grading_started_at`` (UTC isoformat) /
         ``grading_result_path`` in the manifest, atomically write it, and
         return ``{'status': 'grading', 'pid': pid}`` — the tick is free; a
         LATER tick picks the result up.
       - ``grading_pid`` alive (same os.kill/ps zombie-aware idiom)
         -> ``{'status': 'grading', 'pid': story['grading_pid']}``, manifest
         untouched.
       - ``grading_pid`` dead -> ``collect_detached_grade(pid, result_path)``;
         ``None`` despite a dead pid means the fail-closed failed grade; then
         the grading watchdog: ``now - grading_started_at >
         DETACHED_GRADE_WATCHDOG_SECONDS`` -> failed grade whose stderr notes
         the watchdog (nothing is killed — the pid is already dead).
       - with a collected result, the EXISTING post-grade logic resumes
         unchanged (last_test_check paper-trail, tests_passed/failed,
         dispatch_attempts clearing, escalation), fed through a tiny local
         shim; the grading bookkeeping fields are deleted once consumed.
  3. Fail-closed invariant: if ``start_detached_grade`` raises (OSError
     spawning), fall back to the EXISTING synchronous run rather than
     crashing the tick.

Out of scope (survivor list — asserted, not modified): the live-story
watchdog/``_terminate_and_checkpoint`` branches, the empty-agent-log startup
grace, the marker-driven completion path's synchronous run, and every
function outside ``pipeline/story_status.py``.

Written FIRST (TDD): every behavioral test here must be RED until the wiring
lands.  The external boundary (``start_detached_grade``) is faked — the fake
records its arguments and writes the result file, exactly the
external-boundary mock rule; no real test suite is ever spawned.
"""

import ast
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
# Every existing story_status test file does the same.
from pipeline import server as p
from pipeline import story_status as ss

PLAN_NAME = "dg"
STORY_KEY = "S1"
STORY_PID = 4242  # the dispatched agent's pid — dead in every test here
DEAD_GRADE_PID = 424242  # a recorded grading_pid that is already gone
# A fake spawned-grader pid that looks ALIVE if anything probes it (our own
# pid), so a first-poll test cannot be broken by an over-eager liveness check.
FAKE_GRADE_PID = os.getpid()

RESULT_NAME = ".detached_grade_result.json"
LOG_NAME = ".detached_grade.log"

# The exact fail-closed stderr the sibling's collect_detached_grade returns
# when a dead grading pid left no readable result (pinned by
# tests/unit/test_detached_grade.py).
FAIL_CLOSED_STDERR = "detached grade exited without writing a result"

WORKTREE_LOG = "[step 0] bash: pwd\n"


# ----------------------------------------------------------- scaffolding


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Mirror tests/unit/test_check_story_status_lint_gate.py's plan_dir."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _story_status_source() -> str:
    return Path(ss.__file__).read_text()


def _watchdog_seconds() -> int:
    """The threshold the implementation must govern the grading watchdog by.

    Falls back to DISPATCH_WATCHDOG_SECONDS while RED so the behavioral
    watchdog tests fail on the missing wiring rather than on the missing
    constant; the constant-equality test pins DETACHED == DISPATCH exactly.
    """
    return getattr(ss, "DETACHED_GRADE_WATCHDOG_SECONDS",
                   ss.DISPATCH_WATCHDOG_SECONDS)


class _FakeProbe:
    """Stand-in for the `ps -p <pid> -o stat=` subprocess.run result."""

    def __init__(self, stat: str):
        self.returncode = 0
        self.stdout = stat
        self.stderr = ""


def _install_dead_pid(monkeypatch, *, alive_pids=()):
    """os.kill fake: ProcessLookupError for every pid except `alive_pids`.

    Records every (pid, sig) so tests can assert nothing was ever signalled
    beyond the 0-arity liveness probe.
    """
    kills = []
    alive = set(alive_pids)

    def fake_kill(pid, sig=0):
        kills.append((pid, sig))
        if pid not in alive:
            raise ProcessLookupError()

    monkeypatch.setattr(p.os, "kill", fake_kill)
    return kills


def _install_run_mock(monkeypatch, *, test_cmd, sync_test_result=None,
                      ps_stat="S"):
    """subprocess.run fake for the server-side gate.

    - `git ...`        -> CompletedProcess with stdout "fakesha\\n" (the
                          last_test_check HEAD paper-trail).
    - `ps -p ...`      -> _FakeProbe(ps_stat) (zombie-aware liveness idiom).
    - the test command -> `sync_test_result` when given (the OSError
                          fallback path), else an AssertionError tripwire:
                          the detached path must NEVER run the suite
                          synchronously.
    - anything else    -> AssertionError (unexpected call).
    Returns the list every call is appended to.
    """
    calls = []
    test_cmd = list(test_cmd)

    def run_mock(cmd, **kwargs):
        calls.append(list(cmd))
        cmd = list(cmd)
        if cmd and cmd[0] == "git":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="fakesha\n", stderr="")
        if cmd and cmd[0] == "ps":
            return _FakeProbe(ps_stat)
        if cmd == test_cmd:
            if sync_test_result is not None:
                rc, out, err = sync_test_result
                return subprocess.CompletedProcess(cmd, rc, stdout=out,
                                                   stderr=err)
            raise AssertionError(
                "the test suite ran SYNCHRONOUSLY inside the tick — the "
                f"dead-pid path must hand off to a detached grade instead; "
                f"got {cmd}"
            )
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return calls


def _install_start_detached(monkeypatch, *, raise_oserror=False):
    """Fake start_detached_grade on BOTH patch surfaces.

    check_story_status's bare names resolve against pipeline.server's
    namespace (the types.FunctionType rebinding), while a local
    `from pipeline.story_status import ...` would read the story_status
    module — patch both so either wiring is covered.
    """
    rec = {"calls": 0, "cmd": None, "cwd": None, "env": None,
           "result_path": None, "log_path": None}

    def fake_start(cmd, cwd, env, result_path, log_path):
        rec["calls"] += 1
        rec["cmd"] = list(cmd)
        rec["cwd"] = str(cwd)
        rec["env"] = dict(env)
        rec["result_path"] = str(result_path)
        rec["log_path"] = str(log_path)
        if raise_oserror:
            raise OSError("spawn failed (simulated)")
        # Simulate the detached wrapper completing: write the result file.
        Path(result_path).write_text(json.dumps(
            {"returncode": 0, "stdout": "fake detached grade", "stderr": ""}))
        return FAKE_GRADE_PID

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


def _base_setup(plan_dir, monkeypatch, *, story_overrides=None,
                test_cmd=None, has_new_commits=True, sync_test_result=None,
                alive_pids=()):
    """Worktree + manifest + the standard server-side gate mocks.

    Mirrors test_check_story_status_lint_gate.py's _base_setup: dead story
    pid, detected test command, new-commits guard, and the pass-path helpers
    (lint gate / dead-code gate) pinned so the ONLY variable is the
    detached-grading wiring under test.
    """
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(WORKTREE_LOG)
    test_cmd = list(test_cmd) if test_cmd else ["pytest", "-q"]

    story = {"summary": "thing", "status": "in_progress",
             "pid": STORY_PID, "worktree": str(worktree)}
    if story_overrides:
        story.update(story_overrides)
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: story})

    _install_dead_pid(monkeypatch, alive_pids=alive_pids)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (wt, list(test_cmd)))
    monkeypatch.setattr(p, "_worktree_has_new_commits",
                        lambda *a, **k: has_new_commits)
    # Hermetic pass-path helpers (each has its own dedicated test files).
    monkeypatch.setattr(p, "_run_lint_gate", lambda wt, env: None)
    monkeypatch.setattr(p, "_find_dead_new_functions", lambda wt, branch: [])
    monkeypatch.setattr(p, "_acceptance_tampered", lambda story, wt: None)
    monkeypatch.setattr(p, "_added_pytest_test_paths",
                        lambda wt, key, branch: [])
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    calls = _install_run_mock(
        monkeypatch, test_cmd=test_cmd, sync_test_result=sync_test_result)
    return {"worktree": worktree, "story": story, "test_cmd": test_cmd,
            "run_calls": calls}


def _seed_grading(story, worktree, *, pid=DEAD_GRADE_PID, age_seconds=0.0,
                  result=None, drop_result_path=False):
    """Pre-seed the per-story grading bookkeeping a prior tick would have
    written, and optionally the result file the detached wrapper produced."""
    story["grading_pid"] = pid
    started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    story["grading_started_at"] = started.isoformat()
    if not drop_result_path:
        story["grading_result_path"] = str(worktree / RESULT_NAME)
    if result is not None:
        (worktree / RESULT_NAME).write_text(json.dumps(result))
    return story


def _assert_utc_iso(value, what):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"{what} must be tz-aware"
    assert parsed.utcoffset() == timezone.utc.utcoffset(
        datetime.now(timezone.utc)), f"{what} must be UTC isoformat"


# ------------------------------------------- 1. the watchdog constant


def test_detached_grade_watchdog_constant_exists_and_matches_dispatch():
    """DETACHED_GRADE_WATCHDOG_SECONDS must exist at module level and REUSE
    DISPATCH_WATCHDOG_SECONDS's value — one policy governs both watchdogs,
    and no new env var is invented."""
    assert hasattr(ss, "DETACHED_GRADE_WATCHDOG_SECONDS"), (
        "pipeline.story_status must define DETACHED_GRADE_WATCHDOG_SECONDS"
    )
    value = ss.DETACHED_GRADE_WATCHDOG_SECONDS
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value > 0
    assert value == ss.DISPATCH_WATCHDOG_SECONDS, (
        "DETACHED_GRADE_WATCHDOG_SECONDS must reuse DISPATCH_WATCHDOG_SECONDS "
        f"(got {value!r} vs {ss.DISPATCH_WATCHDOG_SECONDS!r})"
    )


def test_watchdog_constant_defined_at_module_level_without_new_env_var():
    """AST check: the constant is a top-level assignment in
    pipeline/story_status.py and its defining expression reads no os.environ
    (the brief forbids inventing a new env var)."""
    src = _story_status_source()
    tree = ast.parse(src)
    lines = src.splitlines()
    found = None
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            targets = [node.target]
        for t in targets:
            if t.id == "DETACHED_GRADE_WATCHDOG_SECONDS":
                found = node
    assert found is not None, (
        "DETACHED_GRADE_WATCHDOG_SECONDS must be assigned at module level in "
        "pipeline/story_status.py"
    )
    segment = "\n".join(lines[found.lineno - 1:found.end_lineno])
    assert "environ" not in segment, (
        "DETACHED_GRADE_WATCHDOG_SECONDS must reuse DISPATCH_WATCHDOG_SECONDS "
        "rather than reading a new env var"
    )


# ------------------------------------------- 2. first poll: spawn detached


def test_dead_pid_story_without_grading_pid_starts_detached_grade(
    plan_dir, monkeypatch,
):
    """Required case (a): a dead-pid story with NO grading_pid must hand the
    grade to start_detached_grade, record the bookkeeping in the manifest,
    and return {'status': 'grading'} immediately — NO test suite may run
    inside the tick."""
    setup = _base_setup(plan_dir, monkeypatch)
    worktree = setup["worktree"]
    rec = _install_start_detached(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    # The tick returns immediately with the grading hand-off shape.
    assert result["status"] == "grading", (
        f"expected the detached hand-off, got {result!r}"
    )
    assert result["pid"] == FAKE_GRADE_PID

    # The spawn happened exactly once, with the detected test command.
    assert rec["calls"] == 1, f"start_detached_grade calls: {rec['calls']}"
    assert rec["cmd"] == setup["test_cmd"]
    assert rec["cwd"] == str(worktree)

    # The bookkeeping landed in the manifest on disk (atomic write happened).
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["grading_pid"] == FAKE_GRADE_PID
    assert "grading_started_at" in story, (
        "story['grading_started_at'] must be recorded in the manifest"
    )
    _assert_utc_iso(story["grading_started_at"], "grading_started_at")
    assert story["grading_result_path"] == str(
        plan_dir / "grading" / STORY_KEY / "result.json"
    )
    # The story stays pollable: a LATER tick must pick the result up, so the
    # status cannot move off in_progress on the first poll.
    assert story["status"] == "in_progress"

    # Nothing was signalled beyond the 0-arity liveness probe (asserted in
    # the watchdog test); here the run-mock tripwire already proves no suite
    # ran synchronously.
    assert setup["run_calls"] == [], (
        f"no subprocess.run call is expected on the detached hand-off, got "
        f"{setup['run_calls']}"
    )


def test_detached_grade_receives_scoped_test_command(plan_dir, monkeypatch):
    """The command handed to start_detached_grade must be the SCOPED one —
    the existing acceptance-scoping logic runs in place and its output is
    what gets detached (not the raw detected command)."""
    setup = _base_setup(
        plan_dir, monkeypatch,
        story_overrides={"acceptance": [{"path": "test_oracle.py",
                                         "source": ""}]},
    )
    worktree = setup["worktree"]
    rec = _install_start_detached(monkeypatch)

    scoped_marker = "SCOPED-BY-THE-EXISTING-SCOPER"
    scoper_calls = []

    def fake_scoper(cmd, acceptance_paths, test_dir):
        scoper_calls.append((list(cmd), list(acceptance_paths),
                             str(test_dir)))
        return [*cmd, scoped_marker, str(acceptance_paths[0])]

    monkeypatch.setattr(p, "_scope_test_cmd_to_acceptance", fake_scoper)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "grading"
    # The existing scoping helper ran in place, on the detected command,
    # with the story's acceptance paths materialized under the worktree.
    assert scoper_calls, "the existing acceptance-scoping logic must run"
    assert scoper_calls[0][0] == setup["test_cmd"]
    assert scoper_calls[0][1] == [str(worktree / "test_oracle.py")]
    # ...and ITS output (not the raw command) is what gets detached.
    assert rec["cmd"] == [*setup["test_cmd"], scoped_marker,
                          str(worktree / "test_oracle.py")], (
        f"start_detached_grade must receive the scoped command, got "
        f"{rec['cmd']}"
    )


def test_detached_grade_receives_stripped_env_and_worktree_artifact_paths(
    plan_dir, monkeypatch,
):
    """The detached grade runs under the SAME stripped env dict the
    synchronous run builds today (PIPELINE_*/LOCAL_AGENT_*/REPO_ROOT
    removed), and its result/log files land under the pipeline-owned
    grading state dir (<state root>/grading/<story>/) — outside the
    agent-writable worktree, per the security review's blocking finding."""
    monkeypatch.setenv("PIPELINE_DG_SENTINEL", "1")
    monkeypatch.setenv("LOCAL_AGENT_DG_SENTINEL", "1")
    monkeypatch.setenv("REPO_ROOT", "/nonexistent-dg-sentinel")
    _base_setup(plan_dir, monkeypatch)
    worktree = plan_dir / "wt"
    rec = _install_start_detached(monkeypatch)

    assert p.check_story_status(PLAN_NAME, STORY_KEY)["status"] == "grading"

    env = rec["env"]
    assert isinstance(env, dict), "test_env must be a plain dict"
    assert "PATH" in env, "the stripped env must still carry PATH"
    assert "PIPELINE_DG_SENTINEL" not in env
    assert "LOCAL_AGENT_DG_SENTINEL" not in env
    assert "REPO_ROOT" not in env

    assert rec["result_path"] == str(
        plan_dir / "grading" / STORY_KEY / "result.json"
    ), "the detached result file must be <state root>/grading/<story>/result.json"
    assert rec["log_path"] == str(
        plan_dir / "grading" / STORY_KEY / "grading.log"
    ), "the detached log file must be <state root>/grading/<story>/grading.log"
    # Security review (blocking finding 1): the verdict channel must live
    # OUTSIDE the agent-writable worktree — the code under test could
    # otherwise forge a passing grade there during its own build.
    assert not Path(rec["result_path"]).is_relative_to(worktree)
    assert not Path(rec["log_path"]).is_relative_to(worktree)


# ------------------------------------------- 3. poll while grade in flight


def test_live_grading_pid_returns_grading_and_leaves_manifest_untouched(
    plan_dir, monkeypatch,
):
    """Required case (b): a story whose grading_pid is ALIVE returns
    {'status': 'grading'} on every poll and the manifest is untouched on the
    second poll (no refreshed timestamps, no duplicate spawn)."""
    setup = _base_setup(plan_dir, monkeypatch, alive_pids=(os.getpid(),))
    worktree = setup["worktree"]
    _seed_grading(setup["story"], worktree, pid=os.getpid())
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    rec = _install_start_detached(monkeypatch)
    _install_collect_spy(monkeypatch)

    first = p.check_story_status(PLAN_NAME, STORY_KEY)
    assert first["status"] == "grading"
    assert first["pid"] == os.getpid()

    before = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    second = p.check_story_status(PLAN_NAME, STORY_KEY)
    after = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]

    assert second["status"] == "grading"
    assert second["pid"] == os.getpid()
    assert after == before, (
        "the second poll must not touch the manifest while the grade is in "
        f"flight; diff: {before!r} -> {after!r}"
    )
    assert after["grading_pid"] == os.getpid()
    assert after["grading_started_at"] == before["grading_started_at"]
    # No duplicate grade may be spawned while one is in flight.
    assert rec["calls"] == 0, (
        "a live grading_pid must not trigger another start_detached_grade"
    )


# ------------------------------------------- 4. collecting a finished grade


def test_dead_grading_pid_with_passing_result_resumes_post_grade_logic(
    plan_dir, monkeypatch,
):
    """Required case (c): a dead grading_pid whose result file records rc=0
    must flow through the EXISTING post-grade logic — tests_passed, the
    Mode-29 last_test_check paper-trail persisted, streak bookkeeping
    cleared, and the grading fields consumed (deleted)."""
    setup = _base_setup(plan_dir, monkeypatch,
                        story_overrides={"dispatch_attempts": 2})
    worktree = setup["worktree"]
    _seed_grading(setup["story"], worktree,
                  result={"returncode": 0, "stdout": "1 passed\n",
                          "stderr": ""})
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    collect_calls = _install_collect_spy(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    # collect_detached_grade was called with the recorded pid + result path.
    assert collect_calls == [(DEAD_GRADE_PID, str(worktree / RESULT_NAME))], (
        f"collect_detached_grade must be called with (grading_pid, "
        f"grading_result_path); got {collect_calls}"
    )

    assert result["status"] == "tests_passed"
    assert result["tests_passed"] is True
    assert result["test_command"] == setup["test_cmd"]
    assert result["output_tail"] == "1 passed\n"

    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "tests_passed"
    # The Mode-29 diagnostic paper-trail keeps working through the shim.
    check = story["last_test_check"]
    assert check["cmd"] == setup["test_cmd"]
    assert check["cwd"] == str(worktree)
    assert check["returncode"] == 0
    assert check["stdout_tail"] == "1 passed\n"
    assert check["sha"] == "fakesha", (
        "the worktree HEAD sha must still be recorded alongside the check"
    )
    _assert_utc_iso(check["ts"], "last_test_check ts")
    # Existing streak bookkeeping is cleared exactly as the sync path does.
    assert "dispatch_attempts" not in story
    # The grading bookkeeping is consumed once the result is collected.
    assert "grading_pid" not in story
    assert "grading_started_at" not in story
    assert "grading_result_path" not in story


def test_dead_grading_pid_with_failing_result_marks_failed_like_sync_path(
    plan_dir, monkeypatch,
):
    """Required case (d): rc=1 in the result file must fire the failure
    branch exactly as the synchronous path does today — failed status,
    last_test_check with the failing returncode and stderr tail, lint gate
    never invoked, bookkeeping consumed."""
    setup = _base_setup(plan_dir, monkeypatch,
                        story_overrides={"dispatch_attempts": 2})
    worktree = setup["worktree"]
    _seed_grading(setup["story"], worktree,
                  result={"returncode": 1, "stdout": "",
                          "stderr": "boom"})
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    collect_calls = _install_collect_spy(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert collect_calls == [(DEAD_GRADE_PID, str(worktree / RESULT_NAME))]
    assert result["status"] == "failed"
    assert result["tests_passed"] is False
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "failed"
    check = story["last_test_check"]
    assert check["returncode"] == 1
    assert check["stderr_tail"] == "boom"
    assert check["stdout_tail"] == ""
    # A failed grade never reaches the lint gate (mirrors the existing
    # dead-pid failure tests).
    assert "last_lint_check" not in story
    assert "dispatch_attempts" not in story
    assert "grading_pid" not in story
    assert "grading_started_at" not in story
    assert "grading_result_path" not in story


def test_dead_grading_pid_failure_fires_review_on_acceptance_fail_optin(
    plan_dir, monkeypatch,
):
    """The escalation branch must fire through the collected path exactly as
    it does today: PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1 routes a failing
    grade that produced real work to reviewable tests_passed with
    acceptance_failed_review=True."""
    setup = _base_setup(plan_dir, monkeypatch)
    _seed_grading(setup["story"], setup["worktree"],
                  result={"returncode": 1, "stdout": "", "stderr": "boom"})
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    _install_collect_spy(monkeypatch)
    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "tests_passed"
    assert story["acceptance_failed_review"] is True
    assert story["last_test_check"]["returncode"] == 1


def test_collected_pass_with_no_new_commits_still_fails_empty_branch(
    plan_dir, monkeypatch,
):
    """The empty-agent-branch false-positive guard survives the detached
    route: a collected rc=0 against a worktree with no new commits is still
    'failed' with reason empty_agent_branch, never tests_passed."""
    setup = _base_setup(plan_dir, monkeypatch, has_new_commits=False)
    _seed_grading(setup["story"], setup["worktree"],
                  result={"returncode": 0, "stdout": "1 passed\n",
                          "stderr": ""})
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    collect_calls = _install_collect_spy(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert collect_calls, "the collected result must be gathered first"
    assert result["status"] == "failed"
    assert result["reason"] == "empty_agent_branch"
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "failed"
    assert story["failure_reason"]


# --------------------------------- 5. fail-closed + malformed result files


def test_dead_grading_pid_without_result_file_fails_closed(
    plan_dir, monkeypatch,
):
    """collect_detached_grade returns None despite a dead pid when the
    wrapper never wrote a result: the wiring must treat that as the
    fail-closed FAILED grade — never a pass, never a crash, never a hang."""
    setup = _base_setup(plan_dir, monkeypatch)
    _seed_grading(setup["story"], setup["worktree"])  # no result file
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    _install_collect_spy(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "failed"
    assert result["tests_passed"] is False
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "failed"
    check = story["last_test_check"]
    assert check["returncode"] == 1
    assert check["stderr_tail"] == FAIL_CLOSED_STDERR


def test_dead_grading_pid_with_malformed_result_json_fails_closed(
    plan_dir, monkeypatch,
):
    """A result file that is not valid JSON is the same pathological case:
    collect fails closed, the wiring records a failed grade."""
    setup = _base_setup(plan_dir, monkeypatch)
    (setup["worktree"] / RESULT_NAME).write_text("{definitely not json{{")
    _seed_grading(setup["story"], setup["worktree"])
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    _install_collect_spy(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["last_test_check"]["returncode"] == 1
    assert story["last_test_check"]["stderr_tail"] == FAIL_CLOSED_STDERR


def test_missing_grading_result_path_fails_closed_without_crashing(
    plan_dir, monkeypatch,
):
    """Malformed bookkeeping (grading_pid recorded but grading_result_path
    missing) must never crash the tick and never guess 'passed': the story
    is either resolved to a failed grade or cleanly re-graded — anything but
    tests_passed."""
    setup = _base_setup(plan_dir, monkeypatch)
    _seed_grading(setup["story"], setup["worktree"], drop_result_path=True)
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    _install_collect_spy(monkeypatch)

    # Must not raise.
    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] != "tests_passed"
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] != "tests_passed"


# ------------------------------------------- 6. the grading watchdog


def test_grading_watchdog_fails_grade_when_started_at_exceeds_threshold(
    plan_dir, monkeypatch,
):
    """Required case (e): grading_started_at older than
    DETACHED_GRADE_WATCHDOG_SECONDS with no collectable result must resolve
    to a FAILED grade whose stderr notes the watchdog — not a hang.  The pid
    is already dead, so nothing may be signalled beyond the liveness probe."""
    setup = _base_setup(plan_dir, monkeypatch)
    _seed_grading(setup["story"], setup["worktree"],
                  age_seconds=_watchdog_seconds() + 60)  # no result file
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    _install_collect_spy(monkeypatch)
    kills = _install_dead_pid(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "failed"
    check = story["last_test_check"]
    assert check["returncode"] == 1
    assert "watchdog" in check["stderr_tail"].lower(), (
        f"the watchdog path must note the watchdog in stderr; got "
        f"{check['stderr_tail']!r}"
    )
    # The watchdog only covers the pathological collect case: the pid is
    # already dead, so nothing is killed.
    assert all(sig == 0 for _, sig in kills), (
        f"the watchdog must not signal anything; kills seen: {kills}"
    )


def test_grading_watchdog_does_not_fire_before_the_threshold(
    plan_dir, monkeypatch,
):
    """Boundary: grading_started_at well inside the watchdog window must NOT
    trip the watchdog — the collected result's own verdict stands."""
    setup = _base_setup(plan_dir, monkeypatch)
    _seed_grading(setup["story"], setup["worktree"],
                  age_seconds=max(_watchdog_seconds() // 2, 0),
                  result={"returncode": 1, "stdout": "", "stderr": "boom"})
    _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: setup["story"]})
    _install_collect_spy(monkeypatch)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    check = story["last_test_check"]
    assert check["returncode"] == 1
    assert check["stderr_tail"] == "boom"
    assert "watchdog" not in check["stderr_tail"].lower()


# ------------------------------------------- 7. OSError spawn fallback


def test_start_detached_grade_oserror_falls_back_to_synchronous_grade(
    plan_dir, monkeypatch,
):
    """Required case (f): if start_detached_grade itself raises (OSError
    spawning), the tick must fall back to the EXISTING synchronous run and
    grade correctly — the old behavior is preserved when the spawn fails.
    No grading bookkeeping may be left behind."""
    setup = _base_setup(
        plan_dir, monkeypatch,
        sync_test_result=(0, "1 passed\n", ""),
    )
    rec = _install_start_detached(monkeypatch, raise_oserror=True)
    monkeypatch.setenv("PIPELINE_DG_SENTINEL", "1")

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    # The spawn was attempted and failed, and the fallback still graded.
    assert rec["calls"] == 1, (
        "the fallback must fire after attempting start_detached_grade"
    )
    assert result["status"] == "tests_passed"
    assert result["tests_passed"] is True
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert story["status"] == "tests_passed"
    check = story["last_test_check"]
    assert check["returncode"] == 0
    assert check["stdout_tail"] == "1 passed\n"
    # A failed spawn leaves NO grading bookkeeping behind.
    assert "grading_pid" not in story
    assert "grading_started_at" not in story
    assert "grading_result_path" not in story
    # The synchronous fallback grades under the same stripped env contract.
    assert setup["run_calls"], "the synchronous grade must have run"
    assert setup["run_calls"][0] == setup["test_cmd"]


# ------------------------------------------- 8. survivor-list guards


def test_live_story_pid_still_returns_running_without_grading_bookkeeping(
    plan_dir, monkeypatch,
):
    """Survivor guard: a LIVE story pid takes the existing running/watchdog
    branch at the top of check_story_status and must never gain grading
    bookkeeping — the marker-driven/live path is out of scope for this
    story."""
    _base_setup(
        plan_dir, monkeypatch,
        story_overrides={"dispatched_at":
                         datetime.now(timezone.utc).isoformat()},
        alive_pids=(STORY_PID,),
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "running"
    assert result["pid"] == STORY_PID
    story = _read_manifest(plan_dir, PLAN_NAME)["stories"][STORY_KEY]
    assert "grading_pid" not in story
    assert "grading_started_at" not in story
    assert "grading_result_path" not in story


def test_grading_pid_bookkeeping_confined_to_dead_pid_region():
    """Success criterion: `grep -n grading_pid pipeline/story_status.py`
    shows the bookkeeping ONLY inside check_story_status's dead-pid region —
    never in the live/marker path, the sibling primitives, or any other
    function."""
    src = _story_status_source()
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "check_story_status"
    )
    lo, hi = fn.lineno, fn.end_lineno
    markers = ("grading_pid", "grading_started_at", "grading_result_path")
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value in markers and not lo <= node.lineno <= hi):
            offenders.append((node.lineno, node.value))
    assert offenders == [], (
        "grading bookkeeping field names must appear only inside "
        f"check_story_status (lines {lo}-{hi}); found outside: {offenders}"
    )


def test_existing_story_status_surface_survives():
    """Survivor list: the branches this story must not touch are still
    present, and the synchronous run (marker path + OSError fallback) still
    lives inside check_story_status.  Membership only — check_story_status's
    dead-pid region is being edited by this very story."""
    src = _story_status_source()
    for needle in (
        "def check_story_status(",
        "_terminate_and_checkpoint",
        "_rebrief_step_cap_struggle",
        "DISPATCH_WATCHDOG_SECONDS",
        "DISPATCH_STARTUP_GRACE_SECONDS",
        "DISPATCH_MAX_ATTEMPTS",
        "STEP_CAP_MARKERS",
        "INFRA_FAILURE_LOG_SUBSTRING",
        "GRADE_WRAPPER",
        "def start_detached_grade(",
        "def collect_detached_grade(",
    ):
        assert needle in src, f"survivor missing from story_status.py: {needle}"

    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "check_story_status"
    )
    segment = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "subprocess.run(" in segment, (
        "the synchronous grade run must remain inside check_story_status "
        "(the marker-driven path and the OSError fallback both use it)"
    )
    assert "_heavy_lock" in segment, (
        "the heavy-command serialization around the synchronous run must "
        "remain inside check_story_status"
    )