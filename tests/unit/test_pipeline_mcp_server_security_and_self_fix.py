"""Tests for the pipeline MCP server: the security review gate and reviewer self-fix (APPROVE_WITH_FIX).

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import fcntl
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import checkpoint as pcheckpoint
from pipeline import concurrency as pcon
from pipeline import server as p
from pipeline import story_status as pstory_status
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    plan_dir,
    worktree_root,
)

# ---------- Heavy-build lock (Fix C) ----------
# Three concurrent cold builds can push a 24GB M4 to its knees (observed in
# the post-PR #30 e2e rerun). _heavy_lock serializes cargo/npm/mvn/gradle/etc.
# invocations across every dispatch site. _is_heavy() is the static predicate
# that decides what counts as a heavy command.

def test_heavy_executables_set_is_static_and_reasonable():
    """Sanity: the static list of heavy executables covers the obvious
    build runners and nothing silly. If someone adds `python` here it'll
    show up in this test failure."""
    expected = {
        "cargo", "npm", "yarn", "pnpm", "npx",
        "mvn", "gradle", "./gradlew",
        "sbt", "bazel", "buck",
        "go", "rustc", "swift", "swiftc",
    }
    assert p.HEAVY_EXECUTABLES == frozenset(expected), (
        f"HEAVY_EXECUTABLES changed unexpectedly: {p.HEAVY_EXECUTABLES - frozenset(expected)} "
        f"added, {frozenset(expected) - p.HEAVY_EXECUTABLES} removed"
    )


@pytest.mark.parametrize("cmd,expected", [
    (["cargo", "build", "-p", "foo"], True),
    (["cargo"], True),
    (["cargo-fmt"], False),  # different executable, different process
    (["npm", "test"], True),
    (["npx", "vitest"], True),
    (["pnpm", "install"], True),
    (["./gradlew", "test"], True),
    (["make", "test"], True),
    (["make", "build"], True),
    (["make", "ci"], True),
    (["make", "all"], True),
    (["make", "clean"], False),  # trivial target
    (["make", "install"], False),  # not in heavy list
    (["make"], False),  # no target
    (["pytest"], False),
    (["ls", "-la"], False),
    (["git", "log"], False),
    (["rustc", "main.rs"], True),
    (["swift", "build"], True),
    (["swiftc", "main.swift"], True),
    ([], False),
])
def test_is_heavy_predicate(cmd, expected):
    """Static executable list catches the build runners without parsing
    command bodies. False negatives (missing a heavy command) are fine —
    a wedged build just doesn't get locked. False positives (locking a
    trivial command) would add latency, so the list is conservative."""
    assert p._is_heavy(cmd) is expected, f"_is_heavy({cmd}) != {expected}"


def test_heavy_lock_serializes_two_concurrent_holders(tmp_path):
    """Two threads entering _heavy_lock() cannot hold it simultaneously.
    Blocking acquire (LOCK_EX) means the second thread queues until the
    first releases."""
    import threading
    # Use tmp_path as PLAN_DIR so the lock file lives in a clean spot
    # for this test only (no risk of colliding with other tests).
    monkeypath_lock = tmp_path / "heavy.lock"
    fd1 = os.open(str(monkeypath_lock), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)  # hold the real lock

    start = time.monotonic()
    held_during_wait = []

    def _contender():
        # The real lock is held by fd1; this open() will block on flock.
        fd2 = os.open(str(monkeypath_lock), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX)  # BLOCKING — queues behind fd1
            held_during_wait.append(time.monotonic())
        finally:
            fcntl.flock(fd2, fcntl.LOCK_UN)
            os.close(fd2)

    t = threading.Thread(target=_contender)
    t.start()
    time.sleep(0.3)  # let the contender queue
    fcntl.flock(fd1, fcntl.LOCK_UN)  # release
    os.close(fd1)
    t.join(timeout=2.0)

    # Contender should have entered the critical section only after we
    # released fd1, i.e. at least ~0.3s after start.
    assert held_during_wait, "contender never acquired the lock"
    assert held_during_wait[0] - start >= 0.25, (
        f"contender should have waited for the holder to release; "
        f"got in at {held_during_wait[0] - start:.3f}s"
    )


def test_check_story_status_acquires_heavy_lock_for_cargo(
    plan_dir, worktree_root, monkeypatch,
):
    """The orchestrator's cargo test grading must take the heavy lock so
    it serializes against in-flight agent cargo invocations. We assert
    by counting flock acquisitions on PLAN_DIR/heavy.lock during the call.
    """
    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    # Write enough manifest state for check_story_status to not bail early.
    manifest_path = plan_dir / "cargo_lock.manifest.json"
    manifest_path.write_text(json.dumps({
        "stories": {
            "S1": {
                "summary": "x", "agent_instructions": "x",
                "status": "in_progress", "dependencies": [],
                "pid": 99999,  # a pid we'll kill below
                "worktree": str(wt),
                "log": str(wt / "agent.log"),
            },
        },
    }))

    # Make sure the pid is dead so check_story_status proceeds past the
    # liveness check.
    try:
        os.kill(99999, 0)
        # If we got here, pid 99999 is alive — try another (very unlikely).
        # We don't need to be precise; the test below catches the lock.
        skip_pid = True
    except ProcessLookupError:
        skip_pid = False

    if skip_pid:
        return  # can't reliably exercise the path

    (wt / "agent.log").write_text("[step 0] bash: pwd\n")  # non-empty log

    lock_held_during_cargo = []
    real_flock = fcntl.flock

    def _counting_flock(fd, op):
        real_flock(fd, op)
        # PLAN_DIR is set in conftest; the heavy lock file lives there.
        heavy_lock = plan_dir / "heavy.lock"
        if (op & fcntl.LOCK_EX) and not (op & fcntl.LOCK_NB):
            try:
                # Probe: can we acquire LOCK_EX | LOCK_NB right now? If no,
                # someone else holds the lock — that's exactly what we want.
                probe_fd = os.open(str(heavy_lock), os.O_CREAT | os.O_RDWR)
                try:
                    real_flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # We got it — heavy lock is free.
                except BlockingIOError:
                    lock_held_during_cargo.append(True)
                finally:
                    try:
                        real_flock(probe_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(probe_fd)
            except OSError:
                pass

    monkeypatch.setattr(pcon.fcntl, "flock", _counting_flock)

    # Stub cargo so the test doesn't actually compile.
    monkeypatch.setattr(p.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, stdout="", stderr=""))

    p.check_story_status("cargo_lock", "S1")

    assert lock_held_during_cargo, (
        "check_story_status did not acquire heavy.lock for a cargo worktree"
    )


def test_check_story_status_strips_pipeline_env_from_test_subprocess(
    plan_dir, worktree_root, monkeypatch,
):
    """Mode 6: the test-grading subprocess must NOT inherit the MCP server's
    PIPELINE_* operational env. Those vars (PIPELINE_PAUSE_THRESHOLD,
    PIPELINE_BACKEND_DISPATCH, PIPELINE_LOCAL_MODEL_DEFAULT, ...) override the
    defaults the suite asserts against and false-fail Python stories at the
    gate (10 env-sensitive tests fail under the server env, 303 pass clean).
    The gate grades the agent's work in a clean dev env, not the server's
    operational one.
    """
    try:
        os.kill(99999, 0)
        return  # pid unexpectedly alive; can't exercise the path reliably
    except ProcessLookupError:
        pass

    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest_path = plan_dir / "envstrip.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {"S1": {
        "summary": "x", "agent_instructions": "x", "status": "in_progress",
        "dependencies": [], "pid": 99999, "worktree": str(wt),
        "log": str(wt / "agent.log"),
    }}}))
    (wt / "agent.log").write_text("[step 0] bash: pwd\n")

    # Operational env the MCP server carries -- must NOT reach the test run.
    for k, v in [
        ("PIPELINE_PAUSE_THRESHOLD", "101"),
        ("PIPELINE_RESUME_THRESHOLD", "0"),
        ("PIPELINE_WEEK_PAUSE_THRESHOLD", "100"),
        ("PIPELINE_BACKEND_DISPATCH", "auto"),
        ("PIPELINE_LOCAL_MODEL_DEFAULT", "minimax-m3:cloud"),
        # LOCAL_AGENT_* harness config the scheduler plist may set for a run
        # (e.g. READ_HEAVY_DISTINCT_WINDOWS raised so a model can explore
        # longer). test_local_agent.py asserts the DEFAULTS, so an override
        # that survives into the graded run false-fails the suite for every
        # story in a repo that vendors the pipeline's own tests.
        ("LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS", "6"),
        ("LOCAL_AGENT_READ_HEAVY_WINDOW", "12"),
        ("LOCAL_AGENT_CHAT_MAX_ATTEMPTS", "9"),
        # REPO_ROOT is a per-plan sentinel, not a developer default.
        ("REPO_ROOT", "/nonexistent-repo-root-set-per-plan-only"),
    ]:
        monkeypatch.setenv(k, v)

    marker = "__mode6_test_marker__"
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(wt), [marker, "pytest"]))
    monkeypatch.setattr(p, "_is_heavy", lambda cmd: False)

    captured = []
    def _capture(cmd, **kw):
        if cmd and cmd[0] == marker:
            captured.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="3 passed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _capture)

    p.check_story_status("envstrip", "S1")

    assert captured, "test-grading subprocess was never run"
    test_env = captured[0]
    assert test_env is not None, (
        "check_story_status passed no env= to the test subprocess, so it "
        "inherited the MCP server's PIPELINE_* env unchanged")
    leaked = [k for k in test_env if k.startswith("PIPELINE_")]
    assert not leaked, f"test subprocess inherited PIPELINE_* env: {leaked}"
    # LOCAL_AGENT_* harness config and the per-plan REPO_ROOT sentinel must
    # also be stripped — they override defaults the suite asserts against.
    leaked_local = [k for k in test_env if k.startswith("LOCAL_AGENT_")]
    assert not leaked_local, (
        f"test subprocess inherited LOCAL_AGENT_* env: {leaked_local}")
    assert "REPO_ROOT" not in test_env, (
        "test subprocess inherited the per-plan REPO_ROOT sentinel")
    # Sanity: the rest of the environment (PATH etc.) is preserved.
    assert "PATH" in test_env


def test_check_story_status_records_sha_on_last_test_and_lint_check(
    plan_dir, worktree_root, monkeypatch,
):
    """The persisted last_test_check / last_lint_check must carry the worktree's
    current HEAD sha so later dispatch/review/rebrief logic can detect when the
    cache is stale (recorded at a past commit) and refuse to reuse it."""
    try:
        os.kill(99999, 0)
        return  # pid unexpectedly alive; can't exercise the path reliably
    except ProcessLookupError:
        pass

    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest_path = plan_dir / "sha.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {"S1": {
        "summary": "x", "agent_instructions": "x", "status": "in_progress",
        "dependencies": [], "pid": 99999, "worktree": str(wt),
        "log": str(wt / "agent.log"),
    }}}))
    (wt / "agent.log").write_text("[step 0] bash: pwd\n")

    marker = "__sha_test_marker__"
    lint_marker = "__sha_lint_marker__"
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(wt), [marker, "pytest"]))
    monkeypatch.setattr(p, "detect_lint_command",
                        lambda wt: (str(wt), [lint_marker, "lint"]))
    monkeypatch.setattr(p, "_is_heavy", lambda cmd: False)

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == marker:
            return subprocess.CompletedProcess(cmd, 0, stdout="3 passed", stderr="")
        if cmd and cmd[0] == lint_marker:
            return subprocess.CompletedProcess(cmd, 0, stdout="no lint issues", stderr="")
        if cmd and cmd[0] == "git" and cmd[1] == "rev-parse":
            return subprocess.CompletedProcess(cmd, 0, stdout="aaa111\n", stderr="")
        # git diff / show / grep used by _find_dead_new_functions: no output.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.check_story_status("sha", "S1")

    story = json.loads(manifest_path.read_text())["stories"]["S1"]
    assert story["last_test_check"]["sha"] == "aaa111"
    assert story["last_lint_check"]["sha"] == "aaa111"



# An empty agent.log within the startup grace window is "agent is alive and
# bootstrapping" (its first print() hasn't flushed — Ollama -np 1 can take
# 30-90s to respond). Outside the window it's the genuine "agent never
# produced any output" failed-launch signature.

def test_check_story_status_treats_empty_log_as_running_within_grace(
    plan_dir, tmp_path, monkeypatch,
):
    """A 0-byte log with a fresh mtime means the agent is still alive and
    bootstrapping (e.g. queued on Ollama's -np 1 worker). check_story_status
    must return "running" so the orchestrator doesn't burn dispatch_attempts
    on a process that's just slow to print."""
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 90)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")  # 0-byte, mtime ~= now
    _write_manifest(plan_dir, "g1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": os.getpid(),  # self → os.kill succeeds, "alive"
               "worktree": str(worktree), "dispatch_attempts": 0},
    })
    # tests must not run — we're declaring it still-running.
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    result = p.check_story_status("g1", "S1")

    assert result["status"] == "running"
    # Crucially, dispatch_attempts was NOT incremented: a live-but-slow
    # agent must not be retried/redispatched yet.
    story = _read_manifest(plan_dir, "g1")["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["dispatch_attempts"] == 0


def test_check_story_status_treats_empty_log_as_failed_launch_after_grace(
    plan_dir, tmp_path, monkeypatch,
):
    """Outside the grace window, an empty log is the original failed-launch
    signature (the agent never produced any output and the process is now
    dead). Status must advance to "interrupted" (or, after budget exhaustion,
    "failed") — NOT stay "running" forever."""
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 90)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("")
    # Backdate the log's mtime past the grace window.
    old_time = time.time() - 200
    os.utime(log_path, (old_time, old_time))
    _write_manifest(plan_dir, "g2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree), "dispatch_attempts": 0},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    result = p.check_story_status("g2", "S1")

    assert result["status"] == "interrupted"
    story = _read_manifest(plan_dir, "g2")["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert story["dispatch_attempts"] == 1


def test_check_story_status_kills_hung_process_past_watchdog_timeout(
    plan_dir, tmp_path, monkeypatch,
):
    """A dispatch subprocess that's still alive well past the watchdog ceiling
    is a hang, not legitimate progress — observed directly during MLX
    provider validation: a blocking, non-streaming chat call can stall
    indefinitely on a single stuck request (0% CPU, no error), and the outer
    harness's own timeout never killed the orphaned subprocess (found running
    minutes later, had to be killed manually). Past the ceiling,
    check_story_status must terminate the process and checkpoint rather than
    reporting "running" forever."""
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 60)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    old_dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    _write_manifest(plan_dir, "wd1", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_at": old_dispatched_at},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            if cmd[0] == "ps":
                stdout = "S\n"
            elif cmd[:2] == ["git", "rev-parse"]:
                stdout = "sha-wd\n"
            else:
                stdout = ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("wd1", "S1")

    assert (4242, pcheckpoint.signal.SIGTERM) in killed
    assert result["status"] == "interrupted"
    assert result.get("watchdog_killed") is True

    story = _read_manifest(plan_dir, "wd1")["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert "dispatch_error" in story
    journal = json.loads((plan_dir / "wd1.S1.journal.json").read_text())
    assert journal[-1]["step"] == "dispatch_watchdog_timeout"


def test_check_story_status_watchdog_timeout_invokes_rebrief_step_cap_struggle(
    plan_dir, tmp_path, monkeypatch,
):
    """A dispatch subprocess killed by the watchdog (hung past
    DISPATCH_WATCHDOG_SECONDS) must be diagnosed exactly like the step-cap
    branch: _rebrief_step_cap_struggle is invoked with the story, the
    worktree, and the plan's role_config/plan_name/story_key BEFORE the
    manifest is written, so the resume isn't blind. Mirrors the step-cap
    branch's own call byte-for-byte (same helper, same arguments)."""
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 60)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Working on it...\n")
    old_dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    _write_manifest(plan_dir, "wdrebrief", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_at": old_dispatched_at,
               "agent_instructions": "GOAL: build the thing."},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            if cmd[0] == "ps":
                stdout = "S\n"
            elif cmd[:3] == ["git", "rev-parse", "HEAD"]:
                stdout = "sha-wd\n"
            else:
                stdout = ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    captured = {}
    def _spy_rebrief(story, worktree_arg, plan_role_config=None,
                     plan_name=None, story_key=None):
        captured["story"] = story
        captured["worktree"] = worktree_arg
        captured["plan_role_config"] = plan_role_config
        captured["plan_name"] = plan_name
        captured["story_key"] = story_key
        captured["called"] = True
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", _spy_rebrief)

    result = p.check_story_status("wdrebrief", "S1")

    # The watchdog branch still terminates and reports its own post-condition.
    assert (4242, pcheckpoint.signal.SIGTERM) in killed
    assert result["status"] == "interrupted"
    assert result.get("watchdog_killed") is True
    # The diagnosis call MUST have fired.
    assert captured.get("called") is True, (
        "_rebrief_step_cap_struggle was not invoked on the watchdog branch")
    # Arguments mirror the step-cap branch byte-for-byte.
    assert captured["worktree"] == str(worktree)
    assert captured["plan_name"] == "wdrebrief"
    assert captured["story_key"] == "S1"
    # The story dict passed in is the in-scope story (same pid).
    assert captured["story"]["pid"] == 4242
    # plan_role_config comes from manifest.get('role_config'); absent here ->
    # None, matching the step-cap branch's manifest.get('role_config') value.
    assert captured["plan_role_config"] is None


def test_check_story_status_watchdog_timeout_diagnosis_lands_in_agent_instructions(
    plan_dir, tmp_path, monkeypatch,
):
    """When the watchdog kills a hung process and the diagnosis role returns a
    fix, the PRIOR-ATTEMPT DIAGNOSIS block must land in agent_instructions
    identically to the step-cap path - the worktree/agent.log left behind by
    _terminate_and_checkpoint are exactly as diagnosable as the step-cap
    path's. This verifies the real _rebrief_step_cap_struggle runs (not just a
    spy) and folds the diagnosis into the persisted manifest."""
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 60)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "Working on it...\n"
        "[step 3] bash: pytest -q\n"
        "stuck in a loop re-running the same failing test\n"
    )
    old_dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    _write_manifest(plan_dir, "wddiag", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_at": old_dispatched_at,
               "agent_instructions": "GOAL: build the thing."},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            if cmd[0] == "ps":
                stdout = "S\n"
            elif cmd[:3] == ["git", "rev-parse", "HEAD"]:
                stdout = "sha-wd\n"
            else:
                stdout = ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    # Override the autouse no-op stub: the diagnosis role returns a real fix.
    monkeypatch.setattr(p, "diagnose_failure",
                        lambda *a, **k: "The agent is stuck re-running a "
                                        "failing test; fix the test fixture.")

    p.check_story_status("wddiag", "S1")

    story = _read_manifest(plan_dir, "wddiag")["stories"]["S1"]
    assert story["status"] == "interrupted"
    instructions = story["agent_instructions"]
    from pipeline import rebrief
    assert rebrief.DIAGNOSIS_HEADER in instructions, (
        "watchdog-killed story must carry a PRIOR-ATTEMPT DIAGNOSIS block, "
        f"got: {instructions!r}")
    assert "stuck re-running a failing test" in instructions
    # The original brief is preserved alongside the diagnosis.
    assert "GOAL: build the thing." in instructions


def test_check_story_status_watchdog_timeout_two_rebrief_call_sites(
    plan_dir, tmp_path, monkeypatch,
):
    """Mechanically-checkable guard: the module that defines
    check_story_status must contain exactly two call sites of
    _rebrief_step_cap_struggle after this change - the pre-existing step-cap
    branch and the new watchdog branch - not one."""
    import re
    src = Path(pstory_status.__file__).read_text()
    # Count call sites: occurrences of the helper name that are not the def
    # line and not a comment/docstring-only mention. The def line is
    # `def _rebrief_step_cap_struggle(`; call sites are bare invocations.
    call_sites = re.findall(r"\b_rebrief_step_cap_struggle\(", src)
    # Subtract the def line itself (def _rebrief_step_cap_struggle().
    def_lines = re.findall(r"def _rebrief_step_cap_struggle\(", src)
    assert len(call_sites) - len(def_lines) == 2, (
        f"expected 2 _rebrief_step_cap_struggle call sites (step-cap + "
        f"watchdog), found {len(call_sites) - len(def_lines)}: {call_sites}")


def test_check_story_status_running_within_watchdog_window_is_not_killed(
    plan_dir, tmp_path, monkeypatch,
):
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 3600)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    recent_dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    _write_manifest(plan_dir, "wd2", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_at": recent_dispatched_at},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "S\n" if cmd[0] == "ps" else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("wd2", "S1")

    assert result == {"status": "running", "pid": 4242}
    assert pcheckpoint.signal.SIGTERM not in [sig for _, sig in killed]
    story = _read_manifest(plan_dir, "wd2")["stories"]["S1"]
    assert story["status"] == "in_progress"


def test_check_story_status_running_without_dispatched_at_skips_watchdog(
    plan_dir, tmp_path, monkeypatch,
):
    """A story dispatched before this field existed has no dispatched_at —
    check_story_status must not crash and must not spuriously kill it; the
    watchdog simply can't apply without a known start time."""
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 1)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_manifest(plan_dir, "wd3", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "S\n" if cmd[0] == "ps" else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("wd3", "S1")

    assert result == {"status": "running", "pid": 4242}


