"""TDD spec for OA2-03: the stale-activity / wall-clock watchdog termination
path must increment a ``watchdog_streak`` counter so repeated hangs converge
to the same fallback/escalation signal the step-cap streak produces at
``STEP_CAP_FALLBACK_THRESHOLD``, instead of looping forever.

Convergence invariant (2026-09-21 review §3.3): EVERY termination path must
increment a counter that participates in a streak/cap, so repeated kills
converge to fallback/escalation rather than resuming the same struggling
model indefinitely. Before this story the watchdog path terminated,
checkpointed, rebriefed and returned ``'interrupted'`` while incrementing NO
counter at all.

Wiring contract graded here (``pipeline/story_status.py``):

  * the watchdog termination path increments ``story["watchdog_streak"]``
    BEFORE ``_terminate_and_checkpoint`` persists the manifest, so the value
    survives across ticks (incrementing after the checkpoint would persist a
    lagging value and escalation at the threshold would never fire);
  * a successful grade clears it in the same place ``dispatch_attempts`` /
    ``step_cap_streak`` / ``infra_failure_streak`` are cleared;
  * at ``watchdog_streak >= STEP_CAP_FALLBACK_THRESHOLD`` the path engages the
    SAME fallback/escalation signal the step-cap streak path produces at its
    threshold (shared helper), instead of a plain resume;
  * ``watchdog_streak`` is a SEPARATE counter: a watchdog kill leaves
    ``step_cap_streak`` and ``infra_failure_streak`` untouched.

REBINDING TRAP: ``check_story_status`` is rebound against
``pipeline.server``'s namespace (``types.FunctionType`` at the bottom of
story_status.py), so every bare name in its body resolves against
``pipeline.server`` at call time. All stubs therefore go on ``p``
(``pipeline.server``), never on the story_status module namespace.
"""

import json
import os
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Import server BEFORE story_status: story_status rebinds check_story_status
# against pipeline.server's namespace, and importing story_status first trips
# the module-level import cycle (story_status <-> server).
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import story_status  # noqa: F401  (triggers the rebinding/export)
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _STEP_CAP_MARKER_LOCAL,
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _make_fake_git_run,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)

STORY_KEY = "S1"
STALE_SECONDS = 1800
WATCHDOG_SECONDS = 3600
PRIMARY_MODEL = "gpt-oss:20b"
FALLBACK_MODEL = "glm-5.2:cloud"


@pytest.fixture()
def oa2_plan_dir(tmp_path, monkeypatch):
    """Isolated PLAN_DIR for this module.

    Defined locally rather than importing the shared ``plan_dir`` fixture:
    that fixture's name collides with this module's helper parameters, which
    ruff's F811 flags as a redefinition (the repo exempts the split
    ``test_pipeline_mcp_server_*.py`` files for exactly this false positive).
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    # persistence/concurrency import PLAN_DIR as a free variable at module
    # load, so the patch must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(pcon, "PLAN_DIR", plans)
    return plans


# --------------------------------------------------------------------------
# stubs / helpers
# --------------------------------------------------------------------------
def _raise_process_lookup(pid, sig):
    raise ProcessLookupError()


def _explode(*a, **k):
    raise AssertionError("this seam must not run on the watchdog path")


class _AlivePS:
    """``ps -p <pid> -o stat=`` result for a live, non-zombie process."""

    stdout = " S"
    returncode = 0


def _manifest_path(plans_dir, plan_name):
    return plans_dir / f"{plan_name}.manifest.json"


def _set_fallback_model(plans_dir, plan_name, model):
    manifest = _read_manifest(plans_dir, plan_name)
    manifest["local_model_fallback"] = model
    _manifest_path(plans_dir, plan_name).write_text(json.dumps(manifest))


def _make_watchdog_story(
    tmp_path, *, worktree_name="wt", dispatched_seconds_ago=7200,
    log_age_seconds=4000, **extra,
):
    """An in_progress story with a live pid and STALE activity.

    ``dispatched_seconds_ago`` must exceed the stale threshold too: the
    watchdog clamps activity age to elapsed, so a fresh dispatch with an old
    agent.log is deliberately NOT killed (see the stale-activity floor).
    """
    worktree = tmp_path / worktree_name
    worktree.mkdir(parents=True, exist_ok=True)
    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": os.getpid(),  # guaranteed-live pid; terminate is always stubbed
        "dispatched_at": (
            datetime.now(timezone.utc) - timedelta(seconds=dispatched_seconds_ago)
        ).isoformat(),
        "worktree": str(worktree),
        "model": PRIMARY_MODEL,
        "backend": "local",
        "dispatched_model": PRIMARY_MODEL,
    }
    story.update(extra)
    if log_age_seconds is not None:
        agent_log = worktree / "agent.log"
        agent_log.write_text("step 1 ok\n", encoding="utf-8")
        mtime = time.time() - log_age_seconds
        os.utime(agent_log, (mtime, mtime))
    return story


def _redispatch(env, *, dispatched_seconds_ago=7200, log_age_seconds=4000):
    """Simulate the next tick re-dispatching the interrupted story.

    The watchdog leaves the story ``interrupted``; the dispatcher then flips
    it back to ``in_progress`` with a fresh (but still stale-activity)
    dispatch. Without this the second call would short-circuit on the
    ``status == 'interrupted'`` early return and never reach the watchdog.
    """
    manifest = _read_manifest(env.plan_dir, env.plan_name)
    story = manifest["stories"][STORY_KEY]
    story["status"] = "in_progress"
    story["pid"] = os.getpid()
    story["dispatched_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=dispatched_seconds_ago)
    ).isoformat()
    agent_log = Path(story["worktree"]) / "agent.log"
    mtime = time.time() - log_age_seconds
    os.utime(agent_log, (mtime, mtime))
    _manifest_path(env.plan_dir, env.plan_name).write_text(json.dumps(manifest))
    return story


def _persisted(env):
    return _read_manifest(env.plan_dir, env.plan_name)["stories"][STORY_KEY]


def _signal(result, story):
    """The observable fallback/escalation signal, minus the per-run pid."""
    return {
        "result": {k: v for k, v in result.items() if k != "pid"},
        "status": story.get("status"),
        "backend": story.get("backend"),
        "escalated": story.get("escalated"),
        "model": story.get("model"),
    }


def _run_step_cap_at_threshold(plans_dir, plan_name, monkeypatch, *, fallback_model):
    """Drive the step-cap streak path to its threshold and capture its output.

    This is the reference signal the watchdog path must match - the test
    compares against THIS output rather than a hardcoded guess.
    """
    worktree = plans_dir / f"wt-{plan_name}"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "agent.log").write_text(
        f"{_STEP_CAP_MARKER_LOCAL}\n", encoding="utf-8")
    _write_manifest(plans_dir, plan_name, {
        STORY_KEY: {
            "summary": "thing", "status": "in_progress", "pid": 4242,
            "worktree": str(worktree), "model": PRIMARY_MODEL,
            "backend": "local", "dispatched_model": PRIMARY_MODEL,
            "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
            "step_cap_streak_model": PRIMARY_MODEL,
        },
    })
    if fallback_model is not None:
        _set_fallback_model(plans_dir, plan_name, fallback_model)
    monkeypatch.setattr(p.os, "kill", _raise_process_lookup)
    monkeypatch.setattr(p, "detect_test_command", _explode)
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status(plan_name, STORY_KEY)
    story = _read_manifest(plans_dir, plan_name)["stories"][STORY_KEY]
    return result, story


@pytest.fixture()
def watchdog_env(oa2_plan_dir, monkeypatch):
    plan_name = f"oa2-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", WATCHDOG_SECONDS)
    monkeypatch.setattr(p, "DISPATCH_STALE_ACTIVITY_SECONDS", STALE_SECONDS)
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 0)
    # Pin the escalation policy so the ambient shell env cannot flip the
    # no-fallback branch on (or off) underneath a test.
    monkeypatch.delenv("PIPELINE_AUTO_ESCALATE", raising=False)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)

    terminate_calls = []

    def _spy_terminate(manifest, manifest_path, plan_name, story_key, story,
                       *, pid=None, step=None, summary=None):
        terminate_calls.append({"step": step, "summary": summary, "pid": pid})
        # Mimic the real _terminate_and_checkpoint's state mutation (it marks
        # the story interrupted and persists) without SIGTERMing the test
        # process or shelling out to git.
        story["status"] = "interrupted"
        story["last_commit"] = "deadbeef"
        story["interrupted_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(json.dumps(manifest))
        return "deadbeef"

    monkeypatch.setattr(p, "_terminate_and_checkpoint", _spy_terminate)
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", lambda *a, **k: None)
    # The watchdog path's only subprocess.run call is the `ps -p <pid>`
    # liveness probe; _terminate_and_checkpoint is stubbed so no git runs.
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _AlivePS())

    return types.SimpleNamespace(
        plan_dir=oa2_plan_dir, plan_name=plan_name, terminate_calls=terminate_calls,
    )


# --------------------------------------------------------------------------
# behavior
# --------------------------------------------------------------------------
def test_single_watchdog_kill_resumes_without_escalating(watchdog_env, tmp_path):
    """Boundary: one kill is below the threshold - the story still resumes as
    'interrupted' (not escalated) but the streak is now 1."""
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: _make_watchdog_story(tmp_path),
    })

    result = p.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {
        "status": "interrupted",
        "pid": os.getpid(),
        "watchdog_killed": True,
    }
    assert len(watchdog_env.terminate_calls) == 1
    story = _persisted(watchdog_env)
    assert story["watchdog_streak"] == 1
    assert story["status"] == "interrupted"


def test_two_watchdog_kills_streak_is_two_and_resumes(watchdog_env, tmp_path):
    """Two consecutive kills leave watchdog_streak == 2 and the story is still
    resumed as 'interrupted' - escalation only fires at the threshold."""
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: _make_watchdog_story(tmp_path),
    })

    first = p.check_story_status(watchdog_env.plan_name, STORY_KEY)
    assert first["watchdog_killed"] is True
    assert _persisted(watchdog_env)["watchdog_streak"] == 1

    _redispatch(watchdog_env)
    second = p.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert second == {
        "status": "interrupted",
        "pid": os.getpid(),
        "watchdog_killed": True,
    }
    assert len(watchdog_env.terminate_calls) == 2
    story = _persisted(watchdog_env)
    assert story["watchdog_streak"] == 2
    assert story["status"] == "interrupted"


def test_successful_grade_resets_watchdog_streak(
    watchdog_env, tmp_path, monkeypatch,
):
    """A dispatch that produced output and ran its tests breaks the streak:
    watchdog_streak resets to 0 alongside the sibling streaks."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Done.\nAll tests pass.\n", encoding="utf-8")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: {
            "summary": "thing", "status": "in_progress", "pid": 4242,
            "worktree": str(worktree), "watchdog_streak": 2,
            "step_cap_streak": 2, "step_cap_streak_model": PRIMARY_MODEL,
            "infra_failure_streak": 2, "infra_failure_streak_model": PRIMARY_MODEL,
            "dispatch_attempts": 1,
        },
    })
    monkeypatch.setattr(p.os, "kill", _raise_process_lookup)
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Green:
        stdout = "all green"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Green())

    p.check_story_status(watchdog_env.plan_name, STORY_KEY)

    story = _persisted(watchdog_env)
    assert story["status"] == "tests_passed"
    assert story.get("watchdog_streak", 0) == 0


def test_watchdog_streak_at_threshold_engages_fallback(
    watchdog_env, tmp_path, monkeypatch,
):
    """At the threshold the watchdog path must engage the SAME fallback signal
    the step-cap streak path produces - compared against that path's own
    output, not a hardcoded guess."""
    # 1) The watchdog path at threshold, with a local fallback configured.
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: _make_watchdog_story(
            tmp_path, worktree_name="wt-wd",
            watchdog_streak=p.STEP_CAP_FALLBACK_THRESHOLD - 1),
    })
    _set_fallback_model(watchdog_env.plan_dir, watchdog_env.plan_name, FALLBACK_MODEL)

    wd_result = p.check_story_status(watchdog_env.plan_name, STORY_KEY)
    wd_story = _persisted(watchdog_env)

    # 2) The step-cap streak path at its threshold, same plan config.
    sc_plan = f"oa2-sc-{uuid.uuid4().hex[:8]}"
    sc_result, sc_story = _run_step_cap_at_threshold(
        watchdog_env.plan_dir, sc_plan, monkeypatch, fallback_model=FALLBACK_MODEL)

    # The fallback signal is the model switch (backend stays local) - the
    # watchdog path must land on exactly the same state as the step-cap path.
    assert sc_story["model"] == FALLBACK_MODEL
    assert wd_story["model"] == sc_story["model"]
    assert wd_story["backend"] == sc_story["backend"] == "local"
    # Still a resume, not an escalation - and the streak is NOT reset.
    assert wd_result["status"] == sc_result["status"] == "interrupted"
    assert wd_story["watchdog_streak"] == p.STEP_CAP_FALLBACK_THRESHOLD


def test_watchdog_streak_at_threshold_escalates_like_step_cap(
    watchdog_env, tmp_path, monkeypatch,
):
    """With no local fallback and auto dispatch, the watchdog path must
    escalate to Claude with the SAME signal (return value + story state) the
    step-cap streak path produces at its threshold."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")

    # 1) The watchdog path at threshold.
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: _make_watchdog_story(
            tmp_path, worktree_name="wt-wd",
            watchdog_streak=p.STEP_CAP_FALLBACK_THRESHOLD - 1),
    })
    wd_result = p.check_story_status(watchdog_env.plan_name, STORY_KEY)
    wd_story = _persisted(watchdog_env)

    # 2) The step-cap streak path at its threshold, same plan config.
    sc_plan = f"oa2-sc-{uuid.uuid4().hex[:8]}"
    sc_result, sc_story = _run_step_cap_at_threshold(
        watchdog_env.plan_dir, sc_plan, monkeypatch, fallback_model=None)

    assert _signal(wd_result, wd_story) == _signal(sc_result, sc_story)
    assert wd_result["status"] == "todo"
    assert wd_story["backend"] == "claude"
    assert wd_story["escalated"] is True


def test_clean_story_never_gains_streak(watchdog_env, tmp_path):
    """Negative: a clean, uninterrupted story (fresh activity, inside the
    ceiling) never gains a watchdog streak."""
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: _make_watchdog_story(
            tmp_path, dispatched_seconds_ago=60, log_age_seconds=0),
    })

    result = p.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []
    assert _persisted(watchdog_env).get("watchdog_streak", 0) == 0


def test_step_cap_streak_and_infra_failure_streak_unchanged(
    watchdog_env, tmp_path,
):
    """A watchdog kill must not fold into the sibling streaks: watchdog_streak
    is a separate counter and step_cap_streak / infra_failure_streak are
    untouched."""
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, {
        STORY_KEY: _make_watchdog_story(
            tmp_path,
            step_cap_streak=1, step_cap_streak_model=PRIMARY_MODEL,
            infra_failure_streak=1, infra_failure_streak_model=PRIMARY_MODEL),
    })

    result = p.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result["watchdog_killed"] is True
    story = _persisted(watchdog_env)
    assert story["watchdog_streak"] == 1
    assert story["step_cap_streak"] == 1
    assert story["step_cap_streak_model"] == PRIMARY_MODEL
    assert story["infra_failure_streak"] == 1
    assert story["infra_failure_streak_model"] == PRIMARY_MODEL
