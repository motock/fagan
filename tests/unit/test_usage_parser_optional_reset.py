"""Regression tests for UG-1: subscription percentages when the reset suffix is absent.

The Claude CLI omits the "· resets <text>" suffix for a session at 0%
("Current session: 0% used"). The parser must still prefer the authoritative
subscription percentages over the request-count approximation in that case,
otherwise a fully available subscription can be paused on local request volume
alone (PIPELINE_PAUSE_THRESHOLD=95).
"""

import pytest

from pipeline import usage as pusage
from pipeline.usage import _parse_usage_output

# Exact degraded shape measured live 2026-10-20 from the real probe output:
# the session line carries no reset suffix, the week line does.
DEGRADED_TEXT = (
    "Current session: 0% used\n"
    "Current week (all models): 31% used · resets Oct 20 at 8:59am (America/Chicago)\n"
    "Last 24h · 1139 requests\n"
    "Last 7d · 1000 requests\n"
)

LEGACY_TEXT = (
    "Current session: 42% used · resets Oct 20 at 9:00am (America/Chicago)\n"
    "Current week (all models): 31% used · resets Oct 20 at 8:59am (America/Chicago)\n"
)

REQUEST_COUNT_ONLY_TEXT = (
    "Last 24h · 1139 requests\n"
    "Last 7d · 1000 requests\n"
)


def test_degraded_session_without_reset_prefers_percentages(monkeypatch):
    """Percentages win over request counts when only the week line has a reset."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 3000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)

    result = _parse_usage_output(DEGRADED_TEXT)

    assert result == {
        "session_pct": 0,
        "week_pct": 31,
        "session_reset": "",
        "week_reset": "Oct 20 at 8:59am (America/Chicago)",
    }


def test_legacy_shape_with_both_resets_is_unchanged():
    """Both lines carrying resets keeps the original percentages and reset strings."""
    result = _parse_usage_output(LEGACY_TEXT)

    assert result == {
        "session_pct": 42,
        "week_pct": 31,
        "session_reset": "Oct 20 at 9:00am (America/Chicago)",
        "week_reset": "Oct 20 at 8:59am (America/Chicago)",
    }


def test_request_count_only_text_uses_fallback(monkeypatch):
    """Text with no percentage lines still falls back to request-count arithmetic."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 3000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)

    result = _parse_usage_output(REQUEST_COUNT_ONLY_TEXT)

    # 1139 * 100 // 3000 == 37, 1000 * 100 // 10000 == 10
    assert result["session_pct"] == 37
    assert result["week_pct"] == 10


def test_unrecognized_text_raises_value_error():
    """Text with neither format still raises ValueError so callers can fall back."""
    with pytest.raises(ValueError):
        _parse_usage_output("some unexpected format with no usage lines")


def test_missing_week_reset_yields_empty_week_reset():
    """A week line without a reset still parses; week_reset is empty, not missing."""
    text = (
        "Current session: 12% used · resets Oct 20 at 9:00am (America/Chicago)\n"
        "Current week (all models): 34% used\n"
        "Last 24h · 1139 requests\n"
        "Last 7d · 1000 requests\n"
    )

    result = _parse_usage_output(text)

    assert result["session_pct"] == 12
    assert result["week_pct"] == 34
    assert result["session_reset"] == "Oct 20 at 9:00am (America/Chicago)"
    assert result["week_reset"] == ""


def test_zero_percentage_is_preserved(monkeypatch):
    """A parsed 0% is a real value, not a missing one to be replaced by the fallback."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 3000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)
    text = (
        "Current session: 0% used\n"
        "Current week (all models): 31% used\n"
        "Last 24h · 1139 requests\n"
        "Last 7d · 1000 requests\n"
    )

    result = _parse_usage_output(text)

    assert result["session_pct"] == 0
    assert result["week_pct"] == 31
