"""Independent ground-truth tests for the token_bucket task.

Authored by the investigator (not the model), never placed in the worktree.
Run by the benchmark harness against the MERGED code to judge whether the
implementation is actually correct, regardless of what tests the model wrote
for itself or what the visible acceptance oracle checked.

API under test:
    TokenBucket(capacity, refill_rate, now=0.0)
    .allow(tokens=1.0, now=None) -> bool
"""
import pytest
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


def test_backward_jump_does_not_rewind_high_water_mark():
    """A rejected/backward `now` must not overwrite the bucket's internal
    clock. Concrete bug this catches: an implementation that unconditionally
    sets `self._last_time = now` (even on the branch where elapsed was
    clamped to 0 for a backward jump) rewinds its own high-water mark. A
    later call with a `now` between the backward value and the true
    high-water mark then computes a bogus large elapsed interval and
    over-refills - a rate-limit bypass. A single backward call alone (see
    test_time_moving_backwards_is_no_refill above) does not expose this;
    it requires a THIRD call, still behind the true high-water mark, to
    show the bucket was illegitimately refilled."""
    b = TokenBucket(1, 1, now=10.0)
    assert b.allow(1, now=10.0) is True    # drain at the true high-water mark
    assert b.allow(1, now=0.0) is False    # backward jump: no refill, rejected
    assert b.allow(1, now=5.0) is False    # still behind the true high-water mark (10.0)


def test_rejected_forward_call_does_not_double_count_refill():
    """A REJECTED allow() at a FORWARD `now` must still advance the internal
    clock. Concrete bug this catches: an implementation that mutates its
    token balance on every call but only advances its last-seen timestamp on
    SUCCESS - after a rejected forward call, the next call recomputes elapsed
    from the stale earlier timestamp and double-counts the refill already
    folded into the balance (a rate-limit bypass). test_rejection_deducts_
    nothing above rejects-then-accepts at the SAME `now` and so cannot expose
    this; it needs a forward `now` on the rejected call followed by a later
    call still behind the true balance."""
    b = TokenBucket(10, 1, now=0.0)
    assert b.allow(9, now=0.0) is True     # drain to 1
    assert b.allow(5, now=3.0) is False    # rejected at t=3 (have 1 + 3 = 4, need 5)
    assert b.allow(9, now=6.0) is False    # correct balance is 7, not the double-counted 10


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


def test_allow_rejects_negative_tokens():
    b = TokenBucket(10, 1, now=0.0)
    with pytest.raises(ValueError):
        b.allow(-1, now=0.0)


def test_negative_tokens_does_not_inflate_bucket():
    # The concrete FM-F bug: a negative tokens value must never be silently
    # "deducted" (i.e. added), which would push the level above capacity.
    b = TokenBucket(5, 1, now=0.0)
    b.allow(5, now=0.0)  # drain to 0
    with pytest.raises(ValueError):
        b.allow(-100, now=0.0)
    assert b.allow(1, now=0.0) is False  # still drained, not inflated past capacity


def test_constructor_rejects_nonpositive_capacity():
    with pytest.raises(ValueError):
        TokenBucket(0, 1, now=0.0)


def test_constructor_rejects_nonpositive_refill_rate():
    with pytest.raises(ValueError):
        TokenBucket(10, 0, now=0.0)
