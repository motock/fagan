"""Independent ground-truth tests for the token_bucket task.

Authored by the investigator (not the model), never placed in the worktree.
Run by the benchmark harness against the MERGED code to judge whether the
implementation is actually correct, regardless of what tests the model wrote
for itself or what the visible acceptance oracle checked.

API under test:
    TokenBucket(capacity, refill_rate, now=0.0)
    .allow(tokens=1.0, now=None) -> bool
"""
from rate_limiter import TokenBucket


def test_starts_full():
    b = TokenBucket(10, 1, now=0.0)
    for _ in range(10):
        assert b.allow(1, now=0.0) is True
    assert b.allow(1, now=0.0) is False


def test_refill_is_partial():
    b = TokenBucket(1, 1, now=0.0)
    assert b.allow(1, now=0.0) is True
    assert b.allow(1, now=0.5) is False   # only 0.5 token back
    assert b.allow(1, now=1.0) is True    # full token back


def test_cap_at_capacity():
    b = TokenBucket(5, 100, now=0.0)
    assert b.allow(5, now=0.0) is True
    assert b.allow(6, now=10.0) is False  # refill capped at 5
    assert b.allow(5, now=10.0) is True


def test_fractional_tokens_and_time():
    b = TokenBucket(2, 2, now=0.0)
    assert b.allow(2, now=0.0) is True
    assert b.allow(1, now=0.5) is True    # 0.5s * 2 = 1.0 token
    assert b.allow(0.5, now=0.5) is False


def test_rejection_deducts_nothing():
    b = TokenBucket(5, 1, now=0.0)
    assert b.allow(5, now=0.0) is True    # drain to 0
    assert b.allow(2, now=1.0) is False   # only 1 token; reject, deduct nothing
    assert b.allow(1, now=1.0) is True    # the 1 token is still there


def test_time_moving_backwards_is_no_refill():
    b = TokenBucket(5, 1, now=10.0)
    assert b.allow(5, now=10.0) is True   # drain
    assert b.allow(1, now=5.0) is False   # earlier time -> elapsed 0, no refill


def test_over_capacity_request_never_succeeds():
    b = TokenBucket(2, 1, now=0.0)
    assert b.allow(3, now=0.0) is False
    assert b.allow(3, now=1_000_000.0) is False


def test_default_tokens_arg():
    b = TokenBucket(3, 1, now=0.0)
    assert b.allow(now=0.0) is True        # default tokens=1.0, three available
    assert b.allow(now=0.0) is True
    assert b.allow(now=0.0) is True
    assert b.allow(now=0.0) is False       # drained


def test_now_none_reuses_last_time():
    b = TokenBucket(1, 1000, now=0.0)
    assert b.allow(1, now=0.0) is True     # drain to 0
    # now=None reuses the last known time (0.0): no time elapses, no refill,
    # so the drained bucket must still reject despite the huge refill_rate.
    assert b.allow(1) is False
