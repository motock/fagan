"""Independent ground-truth tests for the lru_cache task.

Investigator-authored; run by the harness against the merged code. Probes the
recency and eviction edge cases more aggressively than the visible oracle.

API under test:
    LRUCache(capacity)
    .get(key) -> value | None    # counts as a use
    .put(key, value) -> None     # counts as a use; evicts LRU when over capacity
    .size -> int                 # read-only property
"""
import pytest
from lru_cache import LRUCache


def test_eviction_is_strictly_lru():
    c = LRUCache(3)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    c.get("a")            # order now: b (LRU), c, a (MRU)
    c.put("d", 4)         # evict b
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3
    assert c.get("d") == 4
    assert c.size == 3


def test_put_existing_refreshes_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 10)        # update "a" -> "a" MRU, "b" LRU
    c.put("c", 3)         # evict "b"
    assert c.get("b") is None
    assert c.get("a") == 10
    assert c.get("c") == 3


def test_capacity_one():
    c = LRUCache(1)
    c.put("a", 1)
    assert c.get("a") == 1
    c.put("b", 2)
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.size == 1


def test_miss_does_not_change_order_or_size():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("z") is None     # miss
    assert c.size == 2
    c.put("c", 3)                 # "a" is still LRU -> evicted
    assert c.get("a") is None
    assert c.get("b") == 2


def test_size_tracks_inserts_and_evictions():
    c = LRUCache(2)
    assert c.size == 0
    c.put("a", 1)
    assert c.size == 1
    c.put("b", 2)
    assert c.size == 2
    c.put("c", 3)
    assert c.size == 2


@pytest.mark.parametrize("bad", [0, -1, -5])
def test_non_positive_capacity_rejected(bad):
    with pytest.raises(ValueError):
        LRUCache(bad)
