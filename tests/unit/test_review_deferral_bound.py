# ruff: noqa: F811  (imported pytest fixtures are used only as parameters)
"""The bound on consecutive rate-limited review deferrals."""

import pytest

from app import backend
from pipeline import review_deferral as rd
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _RATE_LIMIT_MSG,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
)

_VAR = "PIPELINE_REVIEW_DEFER_PARK_AFTER"


def _arrange(plan_dir, monkeypatch, name, limit, *, count=0, raises=False):
    monkeypatch.delenv("PIPELINE_REVIEW_FALLBACK", raising=False)
    if limit is None:
        monkeypatch.delenv(_VAR, raising=False)
    else:
        monkeypatch.setenv(_VAR, limit)
    story = {"summary": "Add thing", "status": "tests_passed",
             "worktree": str(plan_dir / "wt"), "risk": "low",
             "rework_attempts": 1}
    if count:
        story["review_deferred_count"] = count
    _write_manifest(plan_dir, name, {"S1": story})

    def _stub(wt, br, backend_name=None, **k):
        if raises:
            raise backend.RateLimitedError("429")
        return _RATE_LIMIT_MSG

    monkeypatch.setattr(p, "_run_reviewer", _stub)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)


# ---------- review_defer_park_after ----------
def test_should_fall_back_to_the_default_when_the_limit_is_unset(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    assert rd.review_defer_park_after() == 30


def test_should_fall_back_to_the_default_when_the_limit_is_not_numeric(monkeypatch):
    monkeypatch.setenv(_VAR, "banana")
    assert rd.review_defer_park_after() == 30


def test_should_treat_whitespace_only_limit_as_unset(monkeypatch):
    monkeypatch.setenv(_VAR, "   ")
    assert rd.review_defer_park_after() == 30


def test_should_read_a_numeric_limit_from_the_environment(monkeypatch):
    monkeypatch.setenv(_VAR, "3")
    assert rd.review_defer_park_after() == 3


def test_should_return_zero_and_negative_limits_unchanged(monkeypatch):
    monkeypatch.setenv(_VAR, "0")
    assert rd.review_defer_park_after() == 0
    monkeypatch.setenv(_VAR, "-4")
    assert rd.review_defer_park_after() == -4


# ---------- park_rate_limited_review ----------
def _park(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    story = {"status": "tests_passed", "review_deferred_count": 7, "rework_attempts": 2}
    manifest = {"stories": {"S1": story}}
    result = rd.park_rate_limited_review(
        "plan", "S1", story, manifest, tmp_path / "m.json")
    return result, story


def test_should_set_the_parked_reason_when_parking(tmp_path, monkeypatch):
    _, story = _park(tmp_path, monkeypatch)
    assert story["status"] == "parked"
    assert "7" in story["parked_reason"]
    assert _VAR in story["parked_reason"]


def test_should_not_touch_rework_attempts_when_parking(tmp_path, monkeypatch):
    _, story = _park(tmp_path, monkeypatch)
    assert story["rework_attempts"] == 2


def test_should_return_a_parked_result_when_parking(tmp_path, monkeypatch):
    result, _ = _park(tmp_path, monkeypatch)
    assert result == {"ok": False, "status": "parked", "deferred": "rate_limited"}


# ---------- integration through p.review_story ----------
@pytest.mark.parametrize("raises", [False, True], ids=["verdict_path", "exception_path"])
def test_should_park_when_the_deferral_count_reaches_the_limit(
    plan_dir, agents_dir, monkeypatch, raises,
):
    _arrange(plan_dir, monkeypatch, "bound", "3", raises=raises)

    p.review_story("bound", "S1")
    p.review_story("bound", "S1")
    result = p.review_story("bound", "S1")

    assert result["status"] == "parked"
    story = _read_manifest(plan_dir, "bound")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["rework_attempts"] == 1


@pytest.mark.parametrize("raises", [False, True], ids=["verdict_path", "exception_path"])
def test_should_keep_deferring_while_the_count_is_below_the_limit(
    plan_dir, agents_dir, monkeypatch, raises,
):
    _arrange(plan_dir, monkeypatch, "below", "3", raises=raises)

    first = p.review_story("below", "S1")
    second = p.review_story("below", "S1")

    assert first.get("deferred") == "rate_limited"
    assert second.get("deferred") == "rate_limited"
    assert _read_manifest(plan_dir, "below")["stories"]["S1"]["status"] == "tests_passed"


@pytest.mark.parametrize("limit", ["0", "-1"], ids=["zero", "negative"])
def test_should_disable_the_bound_when_the_limit_is_not_positive(
    plan_dir, agents_dir, monkeypatch, limit,
):
    _arrange(plan_dir, monkeypatch, "unbounded", limit, count=100)

    result = p.review_story("unbounded", "S1")

    assert result.get("deferred") == "rate_limited"
    assert result["status"] == "tests_passed"


def test_should_park_at_the_default_limit_when_unset(plan_dir, agents_dir, monkeypatch):
    _arrange(plan_dir, monkeypatch, "dflt", None, count=29)

    result = p.review_story("dflt", "S1")

    assert result["status"] == "parked"
