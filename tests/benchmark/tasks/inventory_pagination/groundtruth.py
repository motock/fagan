"""Hidden ground truth for the inventory_pagination task.

Stricter than the visible acceptance oracle: it re-derives the limit bounds,
the cursor error cases, the last-page sentinel and the page boundaries, and it
re-runs count_all() against catalogs of 0, 1, 100 and 101 records so a report
that hard-codes the shipped 250 (or captures the total at import time) cannot
pass. The implementing model never sees this file.
"""
import pytest
from inventory import api, report


def _walk(limit=50):
    """Walk every page, returning (ids, page_sizes, final_cursor)."""
    ids, sizes = [], []
    cursor = None
    while True:
        items, cursor = api.list_items(cursor=cursor, limit=limit)
        ids.extend(item["id"] for item in items)
        sizes.append(len(items))
        if cursor is None:
            return ids, sizes, cursor


def test_count_all_counts_the_whole_catalog():
    assert report.count_all() == 250


def test_first_page_has_the_documented_size():
    items, _next_cursor = api.list_items()
    assert len(items) == 50


def test_full_walk_returns_every_item_exactly_once():
    ids, _sizes, _cursor = _walk()
    assert ids == [item["id"] for item in api.ITEMS]


def test_pages_are_disjoint_and_ordered():
    ids, sizes, _cursor = _walk(limit=30)
    assert ids == [item["id"] for item in api.ITEMS]
    assert len(set(ids)) == len(ids)
    assert sizes == [30] * 8 + [10]


def test_next_cursor_is_none_on_the_last_page():
    _ids, _sizes, cursor = _walk(limit=30)
    assert cursor is None


def test_limit_is_clamped_to_one_hundred():
    items, _next_cursor = api.list_items(limit=101)
    assert len(items) == 100
    items, _next_cursor = api.list_items(limit=500)
    assert len(items) == 100


def test_limit_of_one_works():
    items, next_cursor = api.list_items(limit=1)
    assert len(items) == 1
    assert next_cursor is not None


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_raises_value_error(limit):
    with pytest.raises(ValueError):
        api.list_items(limit=limit)


@pytest.mark.parametrize("cursor", ["not-a-cursor", "%%%", "999999"])
def test_unknown_or_malformed_cursor_raises_value_error(cursor):
    with pytest.raises(ValueError):
        api.list_items(cursor=cursor)


@pytest.mark.parametrize("size", [0, 1, 100, 101])
def test_count_all_pages_a_monkeypatched_catalog(monkeypatch, size):
    monkeypatch.setattr(api, "ITEMS", list(api.ITEMS[:size]))
    assert report.count_all() == size
