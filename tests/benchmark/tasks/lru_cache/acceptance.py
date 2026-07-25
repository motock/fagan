"""Hidden acceptance oracle for the lru_cache task (read-only to the agent)."""
import pytest
from lru_cache import LRUCache


def test_basic_get_put():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1
    assert c.get("b") == 2


def test_evicts_least_recently_used():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)        # capacity 2 -> "a" (LRU) evicted
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_get_refreshes_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1   # "a" now MRU
    c.put("c", 3)            # "b" is LRU -> evicted
    assert c.get("b") is None
    assert c.get("a") == 1


def test_update_in_place_no_eviction():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 99)          # update, not insert
    assert c.size == 2
    assert c.get("a") == 99


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        LRUCache(0)
