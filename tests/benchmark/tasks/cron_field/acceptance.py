"""Hidden acceptance oracle for the cron_field task (read-only to the agent)."""
import pytest
from cron_field import match_field


def test_star():
    assert match_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}


def test_single():
    assert match_field("5", 0, 59) == {5}


def test_range():
    assert match_field("1-3", 0, 59) == {1, 2, 3}


def test_star_step():
    assert match_field("*/3", 0, 10) == {0, 3, 6, 9}


def test_range_step():
    assert match_field("1-9/3", 0, 59) == {1, 4, 7}


def test_comma_list():
    assert match_field("1,3,5-7", 0, 59) == {1, 3, 5, 6, 7}


def test_out_of_range_raises():
    with pytest.raises(ValueError):
        match_field("60", 0, 59)


def test_zero_step_raises():
    with pytest.raises(ValueError):
        match_field("*/0", 0, 59)
