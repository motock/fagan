"""Independent ground-truth tests for the cron_field task.

Investigator-authored; run against the merged code. Exercises the step
arithmetic and every documented ValueError path harder than the visible oracle.

API under test:
    match_field(field: str, lo: int, hi: int) -> set[int]
"""
import pytest

from cron_field import match_field


def test_star_full_range():
    assert match_field("*", 1, 4) == {1, 2, 3, 4}


def test_single_value_at_bounds():
    assert match_field("0", 0, 59) == {0}
    assert match_field("59", 0, 59) == {59}


def test_range_inclusive():
    assert match_field("10-12", 0, 59) == {10, 11, 12}


def test_star_step_from_lo_not_zero():
    # stepping starts at lo, not at 0
    assert match_field("*/15", 1, 60) == {1, 16, 31, 46}


def test_range_step():
    assert match_field("0-20/5", 0, 59) == {0, 5, 10, 15, 20}


def test_step_one_is_identity():
    assert match_field("2-6/1", 0, 59) == {2, 3, 4, 5, 6}


def test_comma_union_dedupes():
    assert match_field("1-3,2-4", 0, 59) == {1, 2, 3, 4}


def test_comma_mixed_forms():
    assert match_field("0,*/20", 0, 59) == {0, 20, 40}


def test_out_of_range_single():
    with pytest.raises(ValueError):
        match_field("60", 0, 59)
    with pytest.raises(ValueError):
        match_field("-1", 0, 59)


def test_out_of_range_endpoint():
    with pytest.raises(ValueError):
        match_field("50-70", 0, 59)


def test_reversed_range():
    with pytest.raises(ValueError):
        match_field("5-1", 0, 59)


def test_zero_and_negative_step():
    with pytest.raises(ValueError):
        match_field("*/0", 0, 59)
    with pytest.raises(ValueError):
        match_field("0-10/-2", 0, 59)


def test_empty_token_from_stray_comma():
    with pytest.raises(ValueError):
        match_field("1,,3", 0, 59)


def test_non_integer_token():
    with pytest.raises(ValueError):
        match_field("a", 0, 59)


def test_double_slash():
    with pytest.raises(ValueError):
        match_field("*/2/2", 0, 59)


def test_caller_lo_greater_than_hi():
    with pytest.raises(ValueError):
        match_field("*", 10, 0)
