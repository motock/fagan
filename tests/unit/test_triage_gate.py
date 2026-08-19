"""Tests for the auto-triage gate and candidate selection in pipeline.triage.

These tests verify the two new public functions added to pipeline/triage.py:

* ``_auto_triage_enabled`` - the PIPELINE_AUTO_TRIAGE feature flag, which is
  independent of both PIPELINE_AUTO_ESCALATE and PIPELINE_BACKEND_DISPATCH and
  which ships OFF by default (unset means False).
* ``triage_candidates`` - the deterministic, sorted list of story keys that the
  triage sweep should consider, driven by ``parked``/``failed`` status or a
  ``step_cap_streak`` at/above ``STEP_CAP_FALLBACK_THRESHOLD``.

The implementation does not exist yet; this suite is intentionally RED until
pipeline/triage.py is updated.
"""

import pytest

from pipeline.config import STEP_CAP_FALLBACK_THRESHOLD
from pipeline.triage import __all__ as triage_all
from pipeline.triage import _auto_triage_enabled
from pipeline.triage import triage_candidates


# ---------------------------------------------------------------------------
# __all__ membership - the two new names must both be exported.
# ---------------------------------------------------------------------------

def test_auto_triage_enabled_in_all():
    assert "_auto_triage_enabled" in triage_all


def test_triage_candidates_in_all():
    assert "triage_candidates" in triage_all


# ---------------------------------------------------------------------------
# _auto_triage_enabled
# ---------------------------------------------------------------------------

def test_auto_triage_enabled_unset_is_false(monkeypatch):
    monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)
    assert _auto_triage_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON", " 1 "])
def test_auto_triage_enabled_truthy(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", value)
    assert _auto_triage_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_auto_triage_enabled_falsy(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", value)
    assert _auto_triage_enabled() is False


def test_auto_triage_enabled_unrecognized_fails_closed(monkeypatch):
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "banana")
    assert _auto_triage_enabled() is False


def test_auto_triage_enabled_independent_of_escalate(monkeypatch):
    # PIPELINE_AUTO_ESCALATE must NOT turn on triage.
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "1")
    monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)
    assert _auto_triage_enabled() is False


def test_auto_triage_enabled_independent_of_dispatch(monkeypatch):
    # PIPELINE_BACKEND_DISPATCH must NOT turn on triage.
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)
    assert _auto_triage_enabled() is False


# ---------------------------------------------------------------------------
# triage_candidates
# ---------------------------------------------------------------------------

def test_triage_candidates_parked_and_failed_selected():
    stories = {
        "a": {"status": "parked"},
        "b": {"status": "failed"},
        "c": {"status": "todo"},
    }
    assert triage_candidates(stories) == ["a", "b"]


def test_triage_candidates_interrupted_without_streak_not_selected():
    # interrupted is NOT a trigger: it auto-resumes on the next scheduler tick.
    stories = {
        "i": {"status": "interrupted"},
    }
    assert triage_candidates(stories) == []


@pytest.mark.parametrize(
    "status",
    ["todo", "in_progress", "changes_requested", "pr_open", "done"],
)
def test_triage_candidates_non_trigger_statuses_not_selected(status):
    stories = {"k": {"status": status}}
    assert triage_candidates(stories) == []


def test_triage_candidates_interrupted_with_streak_at_threshold_selected():
    # The streak is the interrupted-adjacent signal worth acting on.
    stories = {
        "i": {
            "status": "interrupted",
            "step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD,
        }
    }
    assert triage_candidates(stories) == ["i"]


def test_triage_candidates_streak_below_threshold_not_selected():
    stories = {
        "i": {
            "status": "interrupted",
            "step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD - 1,
        }
    }
    assert triage_candidates(stories) == []


def test_triage_candidates_empty_dict_no_keyerror():
    # A story dict with no `status` key must not raise KeyError.
    assert triage_candidates({"k": {}}) == []


def test_triage_candidates_non_integer_streak_no_typeerror():
    # A non-integer step_cap_streak counts as 0 and must not raise TypeError.
    stories = {"k": {"status": "todo", "step_cap_streak": "three"}}
    assert triage_candidates(stories) == []


def test_triage_candidates_empty_input_returns_empty_list():
    assert triage_candidates({}) == []


def test_triage_candidates_result_is_sorted():
    stories = {
        "zeta": {"status": "parked"},
        "alpha": {"status": "failed"},
        "mid": {"status": "parked"},
    }
    result = triage_candidates(stories)
    assert result == sorted(result)
    assert result == ["alpha", "mid", "zeta"]


def test_triage_candidates_streak_alone_triggers_without_status():
    # A story with no status but a qualifying streak is still selected.
    stories = {
        "k": {"step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD},
    }
    assert triage_candidates(stories) == ["k"]


def test_triage_candidates_streak_at_threshold_with_parked_status():
    # Both signals present - still selected exactly once.
    stories = {
        "k": {
            "status": "parked",
            "step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD,
        }
    }
    assert triage_candidates(stories) == ["k"]