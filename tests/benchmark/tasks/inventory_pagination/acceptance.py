"""Visible acceptance oracle for the inventory_pagination task.

Materialized read-only into the agent's worktree, so the agent can read it and
run it. It checks only the CORE contract: the report counts the whole catalog,
the first page is the documented size, and a full walk visits every record
exactly once. The stricter bounds, error cases and monkeypatched-catalog
checks live in the hidden ground truth.
"""
from inventory import api, report


def test_count_all_counts_the_whole_catalog():
    assert report.count_all() == 250


def test_first_page_has_the_documented_size():
    items, _next_cursor = api.list_items()
    assert len(items) == 50


def test_full_walk_returns_every_item_exactly_once():
    seen = []
    cursor = None
    while True:
        items, cursor = api.list_items(cursor=cursor)
        seen.extend(item["id"] for item in items)
        if cursor is None:
            break
    assert seen == [item["id"] for item in api.ITEMS]
