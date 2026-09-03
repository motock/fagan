"""Regression tests — security review cycle 2, Blocking finding 1.

``pipeline/story_status.py`` (spawn branch of ``check_story_status``'s
dead-pid path) hardened the detached grade's result channel, but gated the
hardening on ``PIPELINE_STATE_DIR`` — an env var nothing in the repo sets.
With the variable unset (every real deployment), the ``else`` branch ran and
the grade verdict was written to and read from the agent-writable story
worktree (``<worktree>/.detached_grade_result.json``), so the code under test
could forge a passing grade during its own build and bypass the acceptance
gate at the collect tick.

Required fix shape (tech-lead checklist step 3): the pipeline-owned state
dir must be the DEFAULT — derived from the same root that owns the plans/
manifests (the parent of the store's manifest path) — with
``PIPELINE_STATE_DIR`` as an override, and the legacy worktree channel
deleted outright.  A relative ``PIPELINE_STATE_DIR`` resolves against that
fixed pipeline root, never the scheduler's CWD.

These tests pin the real-deployment condition: ``PIPELINE_STATE_DIR`` unset,
manifest at the pipeline root, worktree in a separate agent-writable
directory.  The external boundary (``start_detached_grade``) is faked — it
records its arguments and writes nothing; the tests simulate the wrapper's
writes themselves.  The REAL ``collect_detached_grade`` runs under a spy so
the tests pin exactly which path the collector trusts.  No real test suite is
ever spawned.
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
STORY_KEY = "0042"  # _validate_key-clean ([A-Za-z0-9._-])
STORY_PID = 4242  # the dispatched agent's pid — dead in every test here
DEAD_GRADE_PID = 424242  # a recorded grading_pid that is already gone

# The forgeable worktree-channel paths the pre-fix implementation used as its
# trust root.  After the fix NO code path may select them.
WORKTREE_RESULT_NAME = ".detached_grade_result.json"
WORKTREE_LOG_NAME = ".detached_grade.log"

# The wrapper's real verdict (a genuine failure) vs what a graded agent can
# forge into its own worktree.
REAL_FAIL_RESULT = {"returncode": 1, "stdout": "", "stderr": "real grade failed"}
FORGED_PASS_RESULT = {"returncode": 0, "stdout": "all tests passed", "stderr": ""}

WORKTREE_LOG = "[step 0] bash: pwd\n"


# ----------------------------------------------------------- scaffolding


@pytest.fixture
def pipeline_root(tmp_path, monkeypatch):
    """A tmp pipeline root that owns the plans/manifests: the store's
    manifest path sits directly inside it (``<root>/rc.manifest.json``), so
    the DEFAULT state root the fix must derive is the root itself.  The env
    override is deleted — the real-deployment condition the finding is
    about."""
    root = tmp_path / "pipeline"
    root.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", root)
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    return root


@pytest.fixture
def worktree(tmp_path):
    """A separate agent-writable worktree — deliberately OUTSIDE the
    pipeline root, like a build area the graded agent can write."""
    wt = tmp_path / "wt-0042"
    wt.mkdir()
    (wt / "agent.log").write_text(WORKTREE_LOG)
    return wt


def _write_manifest(pipeline_root, plan_name, stories):
    (pipeline_root / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(pipeline_root, plan_name, story_key):
    manifest = json.loads(
        (pipeline_root / f"{plan_name}.manifest.json").read_text()
    )
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


def _base_setup(pipeline_root, worktree, monkeypatch, *, test_cmd=None):
    """Manifest + the standard server-side gate mocks, mirroring
    tests/unit/test_story_status_result_channel.py's _base_setup: dead story
    pid, detected test command, new-commits guard, hermetic pass-path
    helpers, and no real subprocess anywhere (every pid is already dead so
    no ps probe fires; the synchronous grade never runs because the detached
    hand-off frees the tick)."""
    test_cmd = list(test_cmd) if test_cmd else ["pytest", "-q"]

    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": STORY_PID,
        "worktree": str(worktree),
    }
    _write_manifest(pipeline_root, PLAN_NAME, {STORY_KEY: story})

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
    return {"story": story, "test_cmd": test_cmd}


def _manifest_root():
    """The same derivation the implementation must use: the parent of the
    manifest path the store itself hands out."""
    return Path(p._store.manifest_path(PLAN_NAME)).parent


def _expected_result_path(state_root):
    return state_root / "grading" / STORY_KEY / "result.json"


def _expected_log_path(state_root):
    return state_root / "grading" / STORY_KEY / "grading.log"


# ----------------------------------------------------------- tests


def test_default_result_channel_is_pipeline_owned_without_env(
    pipeline_root, worktree, monkeypatch
):
    """Blocking finding 1: with PIPELINE_STATE_DIR unset (the real-deployment
    condition), the spawn tick must aim the wrapper's result/log files at
    ``<pipeline_root>/grading/<story_key>/`` — the pipeline-owned plans/
    manifests base — and never at the agent-writable worktree.  The
    persisted ``grading_result_path`` (the field the collector reads) must
    point there too, and no worktree-channel file may be created."""
    _base_setup(pipeline_root, worktree, monkeypatch)
    spawn = _install_spawn_stub(monkeypatch)

    result = ss.check_story_status(PLAN_NAME, STORY_KEY)

    # The tick hands the grade to the detached wrapper...
    assert result.get("status") == "grading", result
    assert len(spawn["calls"]) == 1, spawn["calls"]
    call = spawn["calls"][0]
    result_path = Path(call["result_path"])
    log_path = Path(call["log_path"])

    # ...aimed at <pipeline_root>/grading/0042/{result.json,grading.log}.
    assert result_path == _expected_result_path(pipeline_root), call
    assert log_path == _expected_log_path(pipeline_root), call
    assert result_path == _expected_result_path(_manifest_root()), call

    # ...and NOT inside the story worktree the agent can write.
    for path in (result_path, log_path):
        assert not path.is_relative_to(worktree), path

    # The persisted bookkeeping points at the pipeline-owned channel — the
    # exact field the collector later reads via story.get("grading_result_path").
    story = _read_story(pipeline_root, PLAN_NAME, STORY_KEY)
    assert story.get("grading_pid") == DEAD_GRADE_PID
    assert story.get("grading_started_at")
    assert story.get("grading_result_path") == str(result_path), story
    assert Path(story["grading_result_path"]).is_relative_to(pipeline_root)
    assert not Path(story["grading_result_path"]).is_relative_to(worktree)

    # The grading dir is pipeline-owned (0o700)...
    grading_dir = pipeline_root / "grading" / STORY_KEY
    assert grading_dir.is_dir(), grading_dir
    assert (grading_dir.stat().st_mode & 0o777) == 0o700, oct(
        grading_dir.stat().st_mode & 0o777
    )

    # ...and nothing grading-related is created under the worktree: the
    # forgeable channel must not exist on the default path at all.
    assert not (worktree / WORKTREE_RESULT_NAME).exists()
    assert not (worktree / WORKTREE_LOG_NAME).exists()


def test_forged_worktree_result_cannot_pass_on_default_channel(
    pipeline_root, worktree, monkeypatch
):
    """The reviewer's attack path, under the real-deployment condition (env
    unset): the graded agent writes ``{"returncode": 0, ...}`` into its own
    worktree during its build; the next tick's collector must read ONLY the
    persisted pipeline-owned path and must NOT advance the story on the
    forged bytes."""
    _base_setup(pipeline_root, worktree, monkeypatch)
    spawn = _install_spawn_stub(monkeypatch)
    collect_calls = _install_collect_spy(monkeypatch)

    # Tick 1 (t=0): spawn the detached grade on the default channel.
    first = ss.check_story_status(PLAN_NAME, STORY_KEY)
    assert first.get("status") == "grading", first
    assert len(spawn["calls"]) == 1, spawn["calls"]
    channel = Path(spawn["calls"][0]["result_path"])
    assert channel == _expected_result_path(pipeline_root), channel
    assert not channel.is_relative_to(worktree), channel

    # The wrapper writes its REAL (failing) verdict to the channel it was
    # handed...
    channel.parent.mkdir(parents=True, exist_ok=True)
    channel.write_text(json.dumps(REAL_FAIL_RESULT))

    # ...while the graded agent forges a PASSING verdict into the worktree
    # path the pre-fix implementation used as its trust root.
    forged = worktree / WORKTREE_RESULT_NAME
    forged.write_text(json.dumps(FORGED_PASS_RESULT))

    # Tick 2 (t=60): the grading pid is dead; collect.
    ss.check_story_status(PLAN_NAME, STORY_KEY)

    story = _read_story(pipeline_root, PLAN_NAME, STORY_KEY)
    # The forged worktree bytes must never advance the story to a passing
    # state; the real verdict was a failure.
    assert story.get("tests_passed") is not True, story
    assert story.get("status") not in ("tests_passed", "done"), story
    # The verdict that WAS merged came from the pipeline-owned channel —
    # the real failing grade (returncode 1), with none of the forged stdout
    # anywhere in the paper trail.
    assert len(collect_calls) == 1, collect_calls
    collected_path = Path(collect_calls[0][1])
    assert collected_path == channel, collect_calls
    assert not collected_path.is_relative_to(worktree), collected_path
    last_check = story.get("last_test_check") or {}
    assert last_check.get("returncode") == 1, story
    assert FORGED_PASS_RESULT["stdout"] not in json.dumps(story), story


def test_relative_env_override_resolves_against_pipeline_root(
    pipeline_root, worktree, monkeypatch
):
    """Reviewer suggestion, folded into the same hunk: a RELATIVE
    PIPELINE_STATE_DIR must resolve against the fixed pipeline root (the
    manifest base), never the scheduler's CWD — so the spawn tick and the
    later collect tick resolve the same absolute path even if the daemon's
    working directory changes between them."""
    monkeypatch.setenv("PIPELINE_STATE_DIR", "state")
    # The scheduler runs from a directory unrelated to the pipeline root.
    elsewhere = pipeline_root.parent / "srv" / "scheduler"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    _base_setup(pipeline_root, worktree, monkeypatch)
    spawn = _install_spawn_stub(monkeypatch)

    result = ss.check_story_status(PLAN_NAME, STORY_KEY)
    assert result.get("status") == "grading", result
    assert len(spawn["calls"]) == 1, spawn["calls"]

    call = spawn["calls"][0]
    result_path = Path(call["result_path"])
    log_path = Path(call["log_path"])

    # Resolved against the pipeline root, not the scheduler's CWD.
    assert result_path == _expected_result_path(pipeline_root / "state"), call
    assert log_path == _expected_log_path(pipeline_root / "state"), call
    assert result_path.is_absolute(), result_path
    assert not result_path.is_relative_to(elsewhere), result_path
    assert not result_path.is_relative_to(worktree), result_path

    # The persisted bookkeeping points at the same absolute channel.
    story = _read_story(pipeline_root, PLAN_NAME, STORY_KEY)
    assert story.get("grading_result_path") == str(result_path), story