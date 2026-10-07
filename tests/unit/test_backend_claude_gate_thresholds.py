"""ClaudeCliDriver.resource_status() re-derives the usage pause from the cached
percentages with THIS process's thresholds, OR'd with the poller's cached flag.

Regression for the 2026-10-06 incident: the poller ran with a pause threshold
of 101 and stored paused=false at session_pct=100, so this gate called the
backend servable while every Claude call was capped.
"""
import pytest

from app import backend_claude as bc
from app import pipeline_mcp_server as p
from pipeline import usage


@pytest.fixture(autouse=True)
def _synthetic_thresholds(monkeypatch):
    monkeypatch.setattr(usage, "SESSION_PAUSE_THRESHOLD", 95)
    monkeypatch.setattr(usage, "WEEK_PAUSE_THRESHOLD", 90)
    monkeypatch.setattr(bc, "_claude_identity_status", None)


def _status_for(monkeypatch, state):
    monkeypatch.setattr(p, "_read_usage_state", lambda: state)
    return bc.ClaudeCliDriver().resource_status()


def test_should_pause_when_cached_flag_is_false_but_session_pct_meets_this_process_threshold(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "session_pct": 100})
    assert status["ok"] is False


def test_should_pause_when_session_pct_exactly_equals_threshold(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "session_pct": 95})
    assert status["ok"] is False


def test_should_pause_when_cached_flag_is_false_but_week_pct_meets_threshold(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "week_pct": 90})
    assert status["ok"] is False


def test_should_stay_servable_when_cached_flag_is_false_and_both_pcts_are_below_threshold(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "session_pct": 94, "week_pct": 89})
    assert status["ok"] is True


def test_should_pause_when_cached_flag_is_true_even_with_zero_percentages(monkeypatch):
    status = _status_for(monkeypatch, {"paused": True, "session_pct": 0, "week_pct": 0})
    assert status["ok"] is False


def test_should_treat_a_garbled_percentage_as_zero_rather_than_raising(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "session_pct": "not-a-number"})
    assert status["ok"] is True


def test_should_treat_a_missing_percentage_as_zero(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False})
    assert status["ok"] is True


def test_should_trust_the_cached_flag_when_the_state_is_stale(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "stale": True, "session_pct": 100})
    assert status["ok"] is True


def test_should_trust_the_cached_flag_when_the_gate_is_blind(monkeypatch):
    status = _status_for(monkeypatch, {"paused": False, "gate_blind": True, "session_pct": 100})
    assert status["ok"] is True


def test_should_stay_paused_from_cached_flag_when_the_state_is_stale(monkeypatch):
    status = _status_for(monkeypatch, {"paused": True, "stale": True, "session_pct": 0})
    assert status["ok"] is False


def test_should_fail_open_when_there_is_no_usage_state_at_all(monkeypatch):
    status = _status_for(monkeypatch, {})
    assert status["ok"] is True
