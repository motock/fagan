"""Existing suite for the inventory catalog.

These tests pass today. They pin the current, single-page API surface, so a
change to that surface (or to the count it reports) must update the tests it
genuinely invalidates - and say why - rather than deleting them.
"""
from inventory import api, report


def test_catalog_is_deterministic():
    assert len(api.ITEMS) == 250
    assert [item["id"] for item in api.ITEMS] == list(range(1, 251))


def test_list_items_returns_the_first_limit_records():
    assert api.list_items(limit=5) == api.ITEMS[:5]


def test_list_items_defaults_to_one_hundred_records():
    # Pins the current default page size; the pagination contract changes it.
    assert len(api.list_items()) == 100


def test_count_all_reports_the_catalog_size():
    # Pins the current single-page count; it under-counts the 250-record
    # catalog and must be updated once count_all() pages through every item.
    assert report.count_all() == 100
