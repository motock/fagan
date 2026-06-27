"""Independent ground-truth tests for the token-bucket task.

Authored by the investigator (not the model). These define what "did the task"
means, regardless of what tests the model wrote for itself. Each arm's committed
code is judged against THIS file.

API under test (stated identically to the model in every arm):
    TokenBucket(capacity, refill_rate, now=0.0)
    .allow(tokens=1.0, now=None) -> bool
"""
from rate_limiter import TokenBucket


def test_starts_full():
    b = TokenBucket(10, 1, now=0.0)
    for _ in range(10):
        assert b.allow(1, now=0.0) is True
    assert b.allow(1, now=0.0) is False


def test_refill_over_time():
    b = TokenBucket(1, 1, now=0.0)
    assert b.allow(1, now=0.0) is True     # drain
    assert b.allow(1, now=0.5) is False    # only 0.5 token refilled
    assert b.allow(1, now=1.0) is True     # 1.0 token refilled


def test_cap_at_capacity():
    b = TokenBucket(5, 100, now=0.0)
    assert b.allow(5, now=0.0) is True       # drain to 0
    assert b.allow(6, now=10.0) is False     # refill capped at 5, can't exceed
    assert b.allow(5, now=10.0) is True      # exactly capacity available


def test_fractional():
    b = TokenBucket(2, 2, now=0.0)
    assert b.allow(2, now=0.0) is True       # drain to 0
    assert b.allow(1, now=0.5) is True       # 0.5s * 2 = 1.0 token
    assert b.allow(0.5, now=0.5) is False    # nothing left


def test_failure_does_not_deduct():
    b = TokenBucket(2, 1, now=0.0)
    assert b.allow(2, now=0.0) is True       # 0 left
    assert b.allow(1, now=0.5) is False      # only 0.5 token: must FAIL
    assert b.allow(0.5, now=0.5) is True      # the 0.5 must still be there


def test_monotonic_clock():
    b = TokenBucket(1, 1, now=10.0)
    assert b.allow(1, now=10.0) is True      # 0 left
    assert b.allow(1, now=5.0) is False      # time went backwards: no refill


def test_request_over_capacity_never_succeeds():
    b = TokenBucket(2, 1, now=0.0)
    assert b.allow(3, now=0.0) is False
