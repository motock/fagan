"""Hidden acceptance oracle for the token_bucket task.

Materialized read-only into the worktree before the agent runs. The agent is
told the API but cannot edit this file; the pipeline grades the run on whether
the implementation makes these pass. Kept deliberately separate from
groundtruth.py (the investigator's independent judgment).
"""
import pytest

from rate_limiter import TokenBucket


def test_starts_full():
    b = TokenBucket(10, 1, now=0.0)
    for _ in range(10):
        assert b.allow(1, now=0.0) is True
    assert b.allow(1, now=0.0) is False


def test_refill_over_time():
    b = TokenBucket(1, 1, now=0.0)
    assert b.allow(1, now=0.0) is True
    assert b.allow(1, now=0.5) is False
    assert b.allow(1, now=1.0) is True


def test_cap_at_capacity():
    b = TokenBucket(5, 100, now=0.0)
    assert b.allow(5, now=0.0) is True
    assert b.allow(6, now=10.0) is False
    assert b.allow(5, now=10.0) is True


def test_two_consecutive_successes_then_insufficient_at_same_time():
    """Two successive SUCCESSFUL allow() calls must each advance the
    bucket's internal clock, so a third call at the same `now` as the
    second is judged against the correct remaining balance - not
    re-computed as if time had elapsed again from an earlier point.
    Concrete bug this catches: an implementation that only updates its
    last-call timestamp on rejection (never on success) recomputes elapsed
    time from a stale timestamp on the second success, double-counting
    refill and wrongly allowing a request that should be denied."""
    b = TokenBucket(2, 2, now=0.0)
    assert b.allow(2, now=0.0) is True
    assert b.allow(1, now=0.5) is True    # 0.5s * 2 = 1.0 token refilled
    assert b.allow(0.5, now=0.5) is False  # no time elapsed since the last call


def test_over_capacity_never_succeeds():
    b = TokenBucket(2, 1, now=0.0)
    assert b.allow(3, now=1000.0) is False


def test_allow_rejects_negative_tokens():
    b = TokenBucket(10, 1, now=0.0)
    with pytest.raises(ValueError):
        b.allow(-1, now=0.0)


def test_constructor_rejects_nonpositive_capacity():
    with pytest.raises(ValueError):
        TokenBucket(0, 1, now=0.0)


def test_constructor_rejects_nonpositive_refill_rate():
    with pytest.raises(ValueError):
        TokenBucket(10, 0, now=0.0)
