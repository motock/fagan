"""Independent ground-truth tests for the interval_merge task.

Investigator-authored; run against the merged code. Pins the inclusive-adjacency
(+1) rule, the no-mutation guarantee, duplicates, and the ValueError path.

API under test:
    merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]
"""
import pytest
from intervals import merge


def test_overlap_merges():
    assert merge([(1, 3), (2, 6)]) == [(1, 6)]


def test_nested_interval():
    assert merge([(1, 10), (2, 3)]) == [(1, 10)]


def test_adjacent_integers_merge():
    # inclusive endpoints: 2 and 3 are consecutive, so these touch
    assert merge([(1, 2), (3, 4)]) == [(1, 4)]


def test_gap_of_two_stays_separate():
    # 2 and 4 are not consecutive (3 is missing) -> disjoint
    assert merge([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]


def test_unsorted_and_duplicates():
    assert merge([(8, 9), (1, 3), (2, 4), (1, 3)]) == [(1, 4), (8, 9)]


def test_single_interval():
    assert merge([(7, 9)]) == [(7, 9)]


def test_empty_input():
    assert merge([]) == []


def test_point_intervals():
    assert merge([(3, 3), (4, 4)]) == [(3, 4)]      # touching points merge
    assert merge([(3, 3), (5, 5)]) == [(3, 3), (5, 5)]  # gap stays


def test_chain_merge():
    assert merge([(1, 2), (2, 3), (3, 4), (4, 5)]) == [(1, 5)]


def test_does_not_mutate_input():
    data = [(5, 6), (1, 3), (2, 4)]
    snapshot = [tuple(iv) for iv in data]
    merge(data)
    assert data == snapshot


def test_negative_bounds():
    assert merge([(-5, -3), (-4, -1)]) == [(-5, -1)]


def test_start_after_end_raises():
    with pytest.raises(ValueError):
        merge([(1, 2), (5, 1)])
