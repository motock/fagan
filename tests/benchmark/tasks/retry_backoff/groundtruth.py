"""Independent ground-truth tests for the retry_backoff task.

Investigator-authored; run against the merged code. Pins the clamp boundary,
the full status matrix, and every validation path.

API under test:
    backoff_delays(base, factor, cap, attempts) -> list[float]
    should_retry(status_code, attempt, max_attempts) -> bool
"""
import pytest
from backoff import backoff_delays, should_retry


def test_exponential_growth_then_clamp():
    assert backoff_delays(1, 2, 10, 6) == [1, 2, 4, 8, 10, 10]


def test_factor_one_is_constant():
    assert backoff_delays(3, 1, 100, 4) == [3, 3, 3, 3]


def test_cap_equal_to_base():
    assert backoff_delays(5, 2, 5, 3) == [5, 5, 5]


def test_fractional_base_and_factor():
    assert backoff_delays(0.5, 2, 10, 4) == [0.5, 1.0, 2.0, 4.0]


def test_zero_attempts_is_empty():
    assert backoff_delays(1, 2, 10, 0) == []


@pytest.mark.parametrize("args", [
    (0, 2, 10, 3),     # base <= 0
    (-1, 2, 10, 3),    # base <= 0
    (1, 0.5, 10, 3),   # factor < 1
    (1, 2, 0.5, 3),    # cap < base
    (1, 2, 10, -1),    # attempts < 0
])
def test_backoff_validation(args):
    with pytest.raises(ValueError):
        backoff_delays(*args)


@pytest.mark.parametrize("code", [429, 500, 502, 503, 599])
def test_retryable_statuses(code):
    assert should_retry(code, 0, 3) is True


@pytest.mark.parametrize("code", [200, 204, 301, 400, 401, 403, 404, 418, 600])
def test_non_retryable_statuses(code):
    assert should_retry(code, 0, 3) is False


def test_budget_boundary():
    assert should_retry(500, 2, 3) is True    # attempt 2 < 3
    assert should_retry(500, 3, 3) is False   # attempt 3 == max
    assert should_retry(500, 4, 3) is False


def test_zero_max_attempts_never_retries():
    assert should_retry(500, 0, 0) is False


@pytest.mark.parametrize("args", [
    (500, -1, 3),    # attempt < 0
    (500, 0, -1),    # max_attempts < 0
])
def test_should_retry_validation(args):
    with pytest.raises(ValueError):
        should_retry(*args)
