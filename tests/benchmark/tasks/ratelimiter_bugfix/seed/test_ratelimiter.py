import pytest

from ratelimiter import RateLimiter


def test_starts_full():
    rl = RateLimiter(5, 1, now=0.0)
    for _ in range(5):
        assert rl.allow(1, now=0.0) is True
    assert rl.allow(1, now=0.0) is False


def test_single_refill_after_time_passes():
    rl = RateLimiter(1, 1, now=0.0)
    assert rl.allow(1, now=0.0) is True
    assert rl.allow(1, now=0.5) is False
    assert rl.allow(1, now=1.0) is True


def test_rejects_negative_cost():
    rl = RateLimiter(5, 1, now=0.0)
    with pytest.raises(ValueError):
        rl.allow(-1, now=0.0)


def test_rejects_nonpositive_capacity_or_rate():
    with pytest.raises(ValueError):
        RateLimiter(0, 1)
    with pytest.raises(ValueError):
        RateLimiter(5, 0)
