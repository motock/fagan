"""Hidden acceptance oracle for the interval_merge task (read-only to the agent)."""
import pytest

from intervals import merge


def test_overlap():
    assert merge([(1, 3), (2, 6)]) == [(1, 6)]


def test_adjacent_touching_merge():
    assert merge([(1, 2), (3, 4)]) == [(1, 4)]


def test_disjoint_stays_separate():
    assert merge([(1, 2), (5, 6)]) == [(1, 2), (5, 6)]


def test_unsorted_input():
    assert merge([(8, 9), (1, 3), (2, 4)]) == [(1, 4), (8, 9)]


def test_empty():
    assert merge([]) == []


def test_invalid_interval_raises():
    with pytest.raises(ValueError):
        merge([(5, 1)])
