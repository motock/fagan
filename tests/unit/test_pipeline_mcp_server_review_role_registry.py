"""Tests for the pipeline MCP server: review consulting role_registry for provider/model fallback.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import fcntl
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    SAMPLE_USAGE_TEXT,
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
    usage_state_path,
    worktree_root,
)

# ---------- Usage gate: new CLI format ----------

SAMPLE_USAGE_TEXT_NEW = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "What's contributing to your limits usage?\n"
    "Approximate, based on local sessions on this machine\n\n"
    "Last 24h · 1127 requests · 11 sessions\n"
    "  82% of your usage came from subagent-heavy sessions\n\n"
    "Last 7d · 7062 requests · 95 sessions\n"
    "  80% of your usage came from sessions active for 8+ hours\n"
)


def test_parse_usage_output_handles_new_request_count_format(monkeypatch):
    """New CLI format (request counts) is parsed into session_pct/week_pct."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 2000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT_NEW)
    # 1127/2000 = 56%, 7062/10000 = 70%
    assert result["session_pct"] == 56
    assert result["week_pct"] == 70


def test_parse_usage_output_new_format_clamps_to_100(monkeypatch):
    """Request count exceeding the threshold clamps to 100%, not above."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 500)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 1000)
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT_NEW)
    assert result["session_pct"] == 100
    assert result["week_pct"] == 100


def test_parse_usage_output_old_format_still_works():
    """Old percentage format continues to parse correctly after the update."""
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT)
    assert result["session_pct"] == 9
    assert result["week_pct"] == 48


def test_parse_usage_output_raises_on_completely_unrecognized_format():
    with pytest.raises(ValueError):
        p._parse_usage_output("some text with no usage data at all")


def test_run_usage_probe_handles_new_cli_format(monkeypatch):
    """Usage probe works end-to-end with the new /cost JSON output format."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 2000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT_NEW})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    result = p._run_usage_probe()
    assert result["session_pct"] == 56
    assert result["week_pct"] == 70
    assert "checked_at" in result


# ---------- Usage gate: bounded fail-closed ----------

def test_check_usage_pauses_after_prolonged_blindness(monkeypatch, usage_state_path):
    """After gate_blind persists beyond USAGE_BLIND_PAUSE_AFTER_SECONDS, set paused=True."""
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 0)   # always trigger stale path
    monkeypatch.setattr(p, "USAGE_BLIND_PAUSE_AFTER_SECONDS", 100)

    old_enough = datetime.now(timezone.utc) - timedelta(seconds=200)
    prev = {
        "paused": False,
        "gate_blind": True,
        "blind_since": old_enough.isoformat(),
        "measured_at": old_enough.isoformat(),
        "checked_at": old_enough.isoformat(),
        "session_pct": 0,
        "week_pct": 0,
        "consecutive_parse_failures": 50,
    }
    _write_usage_state_direct(usage_state_path, prev)

    monkeypatch.setattr(p, "_run_usage_probe", lambda: (_ for _ in ()).throw(ValueError("no parse")))

    result = p.check_usage()
    assert result["paused"] is True, "prolonged blindness should flip gate to paused (fail-closed)"


def test_check_usage_stays_open_during_short_blind_window(monkeypatch, usage_state_path):
    """A fresh blind window (within threshold) remains fail-open."""
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 0)   # always trigger stale path
    monkeypatch.setattr(p, "USAGE_BLIND_PAUSE_AFTER_SECONDS", 3600)

    recent = datetime.now(timezone.utc) - timedelta(seconds=60)
    prev = {
        "paused": False,
        "gate_blind": True,
        "blind_since": recent.isoformat(),
        "measured_at": recent.isoformat(),
        "checked_at": recent.isoformat(),
        "session_pct": 0,
        "week_pct": 0,
        "consecutive_parse_failures": 5,
    }
    _write_usage_state_direct(usage_state_path, prev)

    monkeypatch.setattr(p, "_run_usage_probe", lambda: (_ for _ in ()).throw(ValueError("no parse")))

    result = p.check_usage()
    assert result["paused"] is False, "short blind window should stay fail-open"


# ---------- Usage gate: log throttling ----------

def test_check_usage_blind_logs_only_on_transition_and_interval(monkeypatch, usage_state_path, capsys):
    """Blind stderr line is emitted only on first blindness + every USAGE_BLIND_LOG_INTERVAL polls."""
    monkeypatch.setattr(p, "USAGE_BLIND_LOG_INTERVAL", 10)
    monkeypatch.setattr(p, "USAGE_BLIND_PAUSE_AFTER_SECONDS", 9999)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 0)  # trigger blind path

    base_time = datetime.now(timezone.utc) - timedelta(seconds=100)
    prev = {
        "paused": False,
        "gate_blind": False,
        "measured_at": base_time.isoformat(),
        "checked_at": base_time.isoformat(),
        "session_pct": 0,
        "week_pct": 0,
        "consecutive_parse_failures": 0,
    }
    _write_usage_state_direct(usage_state_path, prev)

    monkeypatch.setattr(p, "_run_usage_probe", lambda: (_ for _ in ()).throw(ValueError("no parse")))

    # First call: first blind transition — should log.
    p.check_usage()
    out1 = capsys.readouterr().err
    assert out1 != "", "first blind transition should log"

    # Calls 2-9: not on the interval — should NOT log.
    for _ in range(8):
        p.check_usage()
    out2 = capsys.readouterr().err
    assert out2 == "", f"mid-interval blind polls should NOT log, got: {out2!r}"

    # Call 10: interval boundary — should log again.
    p.check_usage()
    out3 = capsys.readouterr().err
    assert out3 != "", "interval boundary should log"


# ---------- Path-traversal validation ----------

@pytest.mark.parametrize("bad_key", [
    "../etc/passwd",
    "../../secret",
    "a/b",
    "a\\b",
    "\x00null",
    "plan" + "/" + "story",
])
def test_validate_key_rejects_path_traversal(bad_key):
    with pytest.raises(ValueError, match="invalid"):
        p._validate_key(bad_key)


@pytest.mark.parametrize("good_key", [
    "my-plan",
    "PIPE-123",
    "story_abc",
    "abc123",
    "abc.def",
    "a" * 200,
])
def test_validate_key_allows_safe_names(good_key):
    p._validate_key(good_key)  # must not raise


def test_dispatch_story_rejects_traversal_plan_name(plan_dir, monkeypatch):
    with pytest.raises(ValueError, match="invalid"):
        p.dispatch_story("../evil", "story-1")


def test_dispatch_story_rejects_traversal_story_key(plan_dir, monkeypatch):
    with pytest.raises(ValueError, match="invalid"):
        p.dispatch_story("myplan", "../evil")


def test_check_story_status_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.check_story_status("../evil", "s1")


def test_interrupt_story_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.interrupt_story("../evil", "s1")


def test_review_story_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.review_story("../evil", "s1")


def test_mark_story_done_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.mark_story_done("../evil", "s1")


# ---------- MCP tool API surface ----------

def test_completed_dep_ids_is_not_a_public_mcp_tool():
    """_completed_dep_ids is a private helper and must NOT be exposed as an MCP tool."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "_completed_dep_ids" not in tool_names, (
        "_completed_dep_ids is a private helper and must not be a public MCP tool"
    )


def test_list_ready_stories_is_a_public_mcp_tool():
    """list_ready_stories must be exposed as an MCP tool per the documented API."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "list_ready_stories" in tool_names, (
        "list_ready_stories must be decorated with @mcp.tool() to be callable as an MCP tool"
    )


def test_list_ready_stories_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.list_ready_stories("../evil")


# ---------- Helper used by the new tests above ----------

def _write_usage_state_direct(path, state):
    """Write state directly to the isolated usage path (bypasses monkeypatching of _atomic_write_json)."""
    path.write_text(json.dumps(state))


@pytest.mark.parametrize("prev_paused,session_pct,week_pct,expected", [
    (False, 50, 10, False),
    (False, 90, 10, True),
    (False, 10, 90, True),
    (False, 89, 10, False),
    (True, 80, 10, True),
    (True, 70, 10, True),
    (True, 69, 10, False),
    (True, 10, 75, True),
])
def test_usage_gate(prev_paused, session_pct, week_pct, expected):
    assert p._usage_gate(prev_paused, session_pct, week_pct) is expected


def test_usage_gate_session_and_week_have_independent_pause_thresholds(monkeypatch):
    monkeypatch.setattr(pusage, "SESSION_PAUSE_THRESHOLD", 80)
    monkeypatch.setattr(pusage, "WEEK_PAUSE_THRESHOLD", 95)

    # Week at 85% would have tripped the old shared 80% threshold, but
    # week's own threshold (95) is not yet reached, and session is low.
    assert p._usage_gate(False, session_pct=10, week_pct=85) is False
    # Session alone crossing its own (lower) threshold still trips it.
    assert p._usage_gate(False, session_pct=80, week_pct=10) is True
    # Week crossing its own (higher) threshold also trips it.
    assert p._usage_gate(False, session_pct=10, week_pct=95) is True


def test_usage_gate_session_and_week_have_independent_resume_thresholds(monkeypatch):
    monkeypatch.setattr(pusage, "SESSION_RESUME_THRESHOLD", 60)
    monkeypatch.setattr(pusage, "WEEK_RESUME_THRESHOLD", 75)

    # Already paused; week is still above its own resume threshold even
    # though it's below the (lower) session resume threshold - stays paused.
    assert p._usage_gate(True, session_pct=10, week_pct=80) is True
    # Both windows have dropped below their own resume thresholds - resumes.
    assert p._usage_gate(True, session_pct=10, week_pct=70) is False


def test_check_usage_carries_paused_hysteresis_from_previous_state(usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 95, "week_pct": 10, "paused": True, "checked_at": "x"}
    ))

    def _fake_run(cmd, **kwargs):
        text = (
            "Current session: 80% used · resets Jun 18 at 11:59am (America/Chicago)\n"
            "Current week (all models): 10% used · resets Jun 23 at 9am (America/Chicago)\n"
        )
        class Result:
            returncode = 0
            stdout = json.dumps({"result": text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()
    assert result["paused"] is True


def test_check_usage_clears_paused_once_below_resume_threshold(usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 95, "week_pct": 10, "paused": True, "checked_at": "x"}
    ))

    def _fake_run(cmd, **kwargs):
        text = (
            "Current session: 50% used · resets Jun 18 at 11:59am (America/Chicago)\n"
            "Current week (all models): 10% used · resets Jun 23 at 9am (America/Chicago)\n"
        )
        class Result:
            returncode = 0
            stdout = json.dumps({"result": text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()
    assert result["paused"] is False


def test_check_usage_tool_probes_and_persists_state(usage_state_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["session_pct"] == 9
    assert result["week_pct"] == 48
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["session_pct"] == 9
    assert persisted["week_pct"] == 48


def test_check_usage_falls_back_to_last_known_state_when_cli_omits_percentages(
    usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": "old"}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
        "Last 24h · 540 requests · 9 sessions\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["session_pct"] == 91
    assert result["week_pct"] == 60
    assert result["paused"] is True
    assert result["checked_at"] != "old"
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["session_pct"] == 91


def test_check_usage_raises_on_cli_omitting_percentages_with_no_prior_state(
    usage_state_path, monkeypatch,
):
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    with pytest.raises(ValueError):
        p.check_usage()


def test_usage_state_age_seconds_returns_none_when_checked_at_missing():
    assert p._usage_state_age_seconds({}) is None


def test_usage_state_age_seconds_returns_none_when_checked_at_unparseable():
    assert p._usage_state_age_seconds({"checked_at": "old"}) is None


def test_usage_state_age_seconds_returns_elapsed_seconds_for_valid_timestamp():
    checked_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    age = p._usage_state_age_seconds({"checked_at": checked_at})
    assert age is not None
    assert 110 <= age <= 130


def test_check_usage_keeps_paused_when_blackout_is_within_staleness_window(
    usage_state_path, monkeypatch,
):
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": recent}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is True
    assert result.get("stale") is not True


def test_check_usage_clears_pause_when_blackout_outlasts_staleness_window(
    usage_state_path, monkeypatch,
):
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": long_ago}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["stale"] is True
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["paused"] is False


def test_check_usage_repeated_blackouts_do_not_reset_the_staleness_clock(
    usage_state_path, monkeypatch,
):
    """Each fallback call bumps checked_at to "now" (it's still useful as
    "last time we tried"), so checked_at alone can't be the staleness clock -
    a poller calling check_usage every 60s would perpetually look "fresh" by
    that measure even though the actual session_pct/week_pct have not been
    re-measured in hours. The real measurement time (measured_at) must be
    carried forward unchanged across fallback calls instead."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": recent, "measured_at": long_ago,
    }))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["stale"] is True


def test_check_usage_fallback_preserves_measured_at_for_the_next_call(
    usage_state_path, monkeypatch,
):
    """measured_at must itself be persisted on every fallback call, not just
    read - otherwise it silently disappears after one call and the next
    call falls back to the (just-bumped) checked_at, recreating the exact
    bug this guards against one call later."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": long_ago, "measured_at": long_ago,
    }))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    p.check_usage()
    persisted = json.loads(usage_state_path.read_text())

    assert persisted["measured_at"] == long_ago


def test_check_usage_success_path_sets_measured_at(usage_state_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["measured_at"] == result["checked_at"]


# ---------- Usage gate blind-state visibility ----------
_BLACKOUT_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "What's contributing to your limits usage?\n"
)


def _blackout_run(cmd, **kwargs):
    class Result:
        returncode = 0
        stdout = json.dumps({"type": "result", "result": _BLACKOUT_TEXT})
        stderr = ""
    return Result()


def test_check_usage_marks_gate_blind_when_failing_open(usage_state_path, monkeypatch):
    """When the probe has been dark past the staleness window, failing the gate
    open must be recorded visibly (gate_blind + blind_since), not just printed."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": long_ago, "measured_at": long_ago,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["gate_blind"] is True
    assert result["blind_since"]  # a timestamp was stamped
    assert result["consecutive_parse_failures"] == 1


def test_check_usage_counts_parse_failures_before_going_blind(usage_state_path, monkeypatch):
    """A parse failure still inside the staleness window bumps the counter but
    does not (yet) blind the gate — the last measurement is still trusted."""
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": recent, "measured_at": recent,
        "consecutive_parse_failures": 2,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["consecutive_parse_failures"] == 3
    assert result.get("gate_blind") is not True
    assert result["paused"] is True  # last measurement still trusted


def test_check_usage_preserves_blind_since_across_consecutive_blind_polls(
    usage_state_path, monkeypatch,
):
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    blind_since = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": False,
        "checked_at": long_ago, "measured_at": long_ago,
        "gate_blind": True, "blind_since": blind_since,
        "consecutive_parse_failures": 5,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["gate_blind"] is True
    assert result["blind_since"] == blind_since  # not reset
    assert result["consecutive_parse_failures"] == 6


def test_check_usage_clears_blind_state_on_successful_probe(usage_state_path, monkeypatch):
    """A real measurement clears the blind flags so the dashboard stops alerting."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 50, "week_pct": 50, "paused": False,
        "checked_at": long_ago, "measured_at": long_ago,
        "gate_blind": True, "blind_since": long_ago,
        "consecutive_parse_failures": 9,
    }))

    def _ok_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _ok_run)

    result = p.check_usage()

    assert result["gate_blind"] is False
    assert result["consecutive_parse_failures"] == 0
    assert result.get("blind_since") is None


# ---------- Merge adjudication (pure decision) ----------
@pytest.mark.parametrize("autonomy,threshold,verdict,risk,expected", [
    ("gated", "low", "APPROVE", "low", "merge"),
    ("gated", "low", "APPROVE", "medium", "park"),
    ("gated", "medium", "APPROVE", "medium", "merge"),
    ("gated", "low", "REQUEST_CHANGES", "low", "park"),
    ("full", "low", "APPROVE", "medium", "merge"),
    ("full", "low", "APPROVE", "high", "park"),
    ("dry-run", "high", "APPROVE", "low", "park"),
])
def test_merge_decision(monkeypatch, autonomy, threshold, verdict, risk, expected):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", autonomy)
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", threshold)
    story = {"review_verdict": verdict, "risk": risk}
    assert p._merge_decision(story)["action"] == expected


# ---------- advance_pipeline concurrency lock ----------
# Overlapping advance_pipeline ticks for the same plan (e.g. launchd firing a
# burst of missed StartIntervals after the machine wakes from sleep) must not
# both see the same ready story and dispatch duplicate, colliding agents into
# the same worktree - that's what actually caused repeated zero-output agent
# deaths in production, not per-story flakiness.
def test_advance_pipeline_skips_when_another_tick_holds_the_lock(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lk", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out tick must not dispatch anything")
    monkeypatch.setattr(p, "dispatch_story", _boom)

    lock_path = plan_dir / "lk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.advance_pipeline("lk")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "lk")
    assert manifest["stories"]["T1"]["status"] == "todo"


def test_advance_pipeline_proceeds_when_lock_is_free(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lk2", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("lk2")

    assert result.get("skipped") is None
    assert dispatched == ["T1"]


def test_advance_pipeline_releases_lock_after_each_call(plan_dir, monkeypatch):
    # A held-then-released lock (the normal case: one tick finishes before
    # the next starts) must not leak into a permanent skip.
    _write_manifest(plan_dir, "lk3", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("lk3")
    result = p.advance_pipeline("lk3")

    assert result.get("skipped") is None


def test_advance_pipeline_lock_is_independent_per_plan(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lkA", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    _write_manifest(plan_dir, "lkB", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append((plan, key)))

    lock_path = plan_dir / "lkA.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.advance_pipeline("lkB")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.get("skipped") is None
    assert ("lkB", "T1") in dispatched


# ---------- dispatch_story / interrupt_story _plan_lock serialization ----------
# Two Claude sessions (each with their own MCP server PID) calling
# dispatch_story on the same story in the same window both want to write
# the same manifest and create the same worktree. Without _plan_lock on
# these tools, the second caller treats the first's half-built worktree
# as resumable and spawns a second agent into the same directory. These
# tests confirm the lock is held for both tools and that a held lock makes
# the call return cleanly instead of crashing or racing.

def test_dispatch_story_skips_when_lock_held(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "dlk", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out dispatch_story must not touch the worktree or manifest")
    monkeypatch.setattr(p.subprocess, "run", _boom)
    monkeypatch.setattr(pt, "plane_request", _boom)

    lock_path = plan_dir / "dlk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.dispatch_story("dlk", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert "another dispatch/interrupt" in result.get("reason", "")
    # Manifest untouched (still "todo", no pid written).
    manifest = _read_manifest(plan_dir, "dlk")
    assert manifest["stories"]["S1"]["status"] == "todo"
    assert "pid" not in manifest["stories"]["S1"]


def test_interrupt_story_skips_when_lock_held(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ilk", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": worktree},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out interrupt_story must not signal or checkpoint")
    monkeypatch.setattr(p.os, "kill", _boom)
    monkeypatch.setattr(p.subprocess, "run", _boom)

    lock_path = plan_dir / "ilk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.interrupt_story("ilk", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert "another dispatch/interrupt" in result.get("reason", "")
    # Manifest untouched: still in_progress with its original pid.
    manifest = _read_manifest(plan_dir, "ilk")
    assert manifest["stories"]["S1"]["status"] == "in_progress"
    assert manifest["stories"]["S1"]["pid"] == 4242


# ---------- advance_pipeline nested _plan_lock regression ----------
# advance_pipeline holds _plan_lock for the whole tick and calls dispatch_story
# and interrupt_story, which each re-acquire the same lock. flock is per
# open-file-description, so a second os.open of the lock file fails to re-flock
# within the same process — the nested call used to return skipped:locked and
# advance_pipeline would falsely count it as dispatched/interrupted while doing
# nothing. _plan_lock must be reentrant within a tick so the nested call
# actually runs. These exercise the REAL dispatch_story/interrupt_story (only
# external boundaries mocked), not stubs.

def test_advance_pipeline_actually_dispatches_ready_story_not_just_reports_it(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "nest", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    # check_story_status would try to run tests against an empty mock worktree;
    # the dispatch itself is what we're asserting, so keep the agent "running".
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})

    result = p.advance_pipeline("nest")

    assert result["ok"] is True
    assert "S1" in result["dispatched"]
    story = _read_manifest(plan_dir, "nest")["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 1234


def test_advance_pipeline_actually_interrupts_in_progress_when_dispatch_gated(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    _write_manifest(plan_dir, "nestint", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111,
               "worktree": str(tmp_path / "wt"), "dependencies": []},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    # Dispatch backend gated -> advance_pipeline interrupts in_progress agents.
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (False, "gate down"))
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})

    class _GitResult:
        returncode = 0
        stdout = "sha123\n"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _GitResult())

    result = p.advance_pipeline("nestint")

    assert result["ok"] is True
    assert result["dispatch_paused"] is True
    assert "R1" in result["interrupted"]
    story = _read_manifest(plan_dir, "nestint")["stories"]["R1"]
    assert story["status"] == "interrupted"


def test_advance_pipeline_does_not_interrupt_in_progress_on_memory_pressure_gate(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """A local-memory-pressure gate ('insufficient free memory') is
    self-inflicted by an in-progress dispatch actively loading its model -
    killing it doesn't free a shared/exhaustible resource, it just destroys
    progress and immediately re-triggers the same gate on redispatch.
    Observed live 2026-07-13: a repeating load/interrupt/redispatch cycle
    (a new PID every ~10-20s, never converging) on both glm-4.7-flash and
    qwen3-coder:30b. Only THIS specific reason should be exempted from the
    interrupt sweep; test_advance_pipeline_actually_interrupts_in_progress_
    when_dispatch_gated covers that every other gate reason still
    interrupts as before."""
    _write_manifest(plan_dir, "memgate", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111,
               "worktree": str(tmp_path / "wt"), "dependencies": []},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (False, "insufficient free memory (1024mb < 2048mb floor)"),
    )
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})

    class _GitResult:
        returncode = 0
        stdout = "sha123\n"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _GitResult())

    result = p.advance_pipeline("memgate")

    assert result["ok"] is True
    assert result["dispatch_paused"] is True
    assert result["interrupted"] == []
    story = _read_manifest(plan_dir, "memgate")["stories"]["R1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 111


