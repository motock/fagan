"""Hidden acceptance oracle for the retry_backoff task (read-only to the agent)."""
import pytest

from backoff import backoff_delays, should_retry


def test_basic_exponential_with_cap():
    assert backoff_delays(1, 2, 10, 6) == [1, 2, 4, 8, 10, 10]


def test_zero_attempts():
    assert backoff_delays(1, 2, 10, 0) == []


def test_invalid_base():
    with pytest.raises(ValueError):
        backoff_delays(0, 2, 10, 3)


def test_retry_on_429_and_5xx():
    assert should_retry(429, 0, 3) is True
    assert should_retry(503, 1, 3) is True


def test_no_retry_on_4xx_other_than_429():
    assert should_retry(404, 0, 3) is False
    assert should_retry(400, 0, 3) is False


def test_no_retry_when_budget_exhausted():
    assert should_retry(500, 3, 3) is False


def test_no_retry_on_success():
    assert should_retry(200, 0, 3) is False
