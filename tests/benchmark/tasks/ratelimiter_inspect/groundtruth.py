"""Independent ground-truth tests for the ratelimiter_inspect task.

Authored by the investigator (not the model), never placed in the worktree.
Run by the benchmark harness against the MERGED code to judge whether the
implementation is actually correct, regardless of what tests the model wrote
for itself or what the visible acceptance oracle checked. Harder than
acceptance.py, per the benchmark's own authoring convention -- more edge and
negative cases, especially around available_tokens()'s no-mutation contract
(the FM-G-shaped bug class: a read-only query that has side effects).

API under test:
    TokenBucket(capacity, refill_rate, now=0.0)
    .allow(tokens=1.0, now=None) -> bool
    .available_tokens(now=None) -> float
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
    assert b.allow(1, now=0.5) is False
    assert b.allow(1, now=1.0) is True


def test_cap_at_capacity():
    b = TokenBucket(5, 100, now=0.0)
    assert b.allow(5, now=0.0) is True
    assert b.allow(6, now=10.0) is False
    assert b.allow(5, now=10.0) is True


def test_rejection_deducts_nothing():
    b = TokenBucket(5, 1, now=0.0)
    assert b.allow(5, now=0.0) is True
    assert b.allow(2, now=1.0) is False
    assert b.allow(1, now=1.0) is True


def test_time_moving_backwards_is_no_refill():
    b = TokenBucket(5, 1, now=10.0)
    assert b.allow(5, now=10.0) is True
    assert b.allow(1, now=5.0) is False


def test_allow_rejects_negative_tokens():
    b = TokenBucket(10, 1, now=0.0)
    with pytest.raises(ValueError):
        b.allow(-1, now=0.0)


def test_negative_tokens_does_not_inflate_bucket():
    b = TokenBucket(5, 1, now=0.0)
    b.allow(5, now=0.0)
    with pytest.raises(ValueError):
        b.allow(-100, now=0.0)
    assert b.allow(1, now=0.0) is False


def test_constructor_rejects_nonpositive_capacity():
    with pytest.raises(ValueError):
        TokenBucket(0, 1, now=0.0)


def test_constructor_rejects_nonpositive_refill_rate():
    with pytest.raises(ValueError):
        TokenBucket(10, 0, now=0.0)


# --- available_tokens(): read-only inspector, no-mutation contract ---

def test_available_tokens_reports_full_at_start():
    b = TokenBucket(10, 1, now=0.0)
    assert b.available_tokens(now=0.0) == 10


def test_available_tokens_partial_refill_matches_what_allow_would_see():
    b = TokenBucket(1, 1, now=0.0)
    assert b.allow(1, now=0.0) is True
    assert b.available_tokens(now=0.5) == pytest.approx(0.5)
    assert b.allow(1, now=0.5) is False  # confirms available_tokens agreed with allow


def test_available_tokens_capped_at_capacity():
    b = TokenBucket(5, 100, now=0.0)
    assert b.available_tokens(now=1000.0) == 5


def test_available_tokens_time_moving_backwards_no_error_no_negative_refill():
    b = TokenBucket(5, 1, now=10.0)
    assert b.allow(5, now=10.0) is True  # drain to 0
    assert b.available_tokens(now=5.0) == 0  # earlier time -> elapsed 0, no refill


def test_available_tokens_now_none_reuses_last_known_time():
    b = TokenBucket(1, 1000, now=0.0)
    assert b.allow(1, now=0.0) is True  # drain to 0
    # now=None reuses last-known time (0.0): no elapsed time despite the huge
    # refill_rate, so the drained bucket reports 0, not "instantly refilled".
    assert b.available_tokens() == 0


def test_repeated_available_tokens_calls_are_idempotent():
    b = TokenBucket(3, 1, now=0.0)
    first = b.available_tokens(now=2.0)
    second = b.available_tokens(now=2.0)
    third = b.available_tokens(now=2.0)
    assert first == second == third == 3  # capped, and stable across repeats


def test_available_tokens_speculative_future_peek_does_not_lock_in_early_refill():
    """The concrete bug this catches: an implementation of available_tokens()
    that reuses allow()'s refill-AND-STORE logic (rather than a pure read), so
    peeking far into the future writes that speculative, fully-refilled level
    (and the peek's `now`) back onto the bucket. A later real call at an
    earlier `now` than the peek would then see "time moving backwards" and
    keep the speculative value, instead of the correct, smaller, real
    partial-refill amount."""
    b = TokenBucket(2, 1, now=0.0)
    assert b.allow(2, now=0.0) is True  # drain to 0
    # Speculative peek far in the future: hypothetically the bucket would be
    # fully refilled by then.
    assert b.available_tokens(now=1000.0) == 2
    # But real elapsed time only actually reached 0.5s: only half a token has
    # really refilled. The peek must not have locked that in as real state.
    assert b.available_tokens(now=0.5) == pytest.approx(0.5)
    assert b.allow(1, now=0.5) is False  # not enough real elapsed time yet
