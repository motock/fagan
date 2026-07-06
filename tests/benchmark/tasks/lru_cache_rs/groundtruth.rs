// Independent groundtruth grader for the LRU-cache Rust task. Never
// written into the agent's worktree; only the harness's run_groundtruth
// copy this into a scratch crate and runs `cargo test` against it. The
// agent's spec tells it to follow TDD, so it will also write its own
// tests/test_lru_cache.rs against the same LruCache public surface.
// This file adds cases the agent's tests typically miss:
//   - the recency-invariant after a sequence of get/put that the
//     acceptance.rs only spot-checks;
//   - the panic-on-zero with a specific message (some agents panic
//     with a different message; we don't care which, but the panic
//     must happen);
//   - a stress test that exercises every eviction-ordering edge.

use bench_task::LruCache;

#[test]
fn get_then_put_on_same_key_does_not_evict() {
    // Key 1 is touched (get) and is now MRU. Adding a third key when
    // capacity=2 should evict key 2 (LRU), not key 1.
    let mut c = LruCache::new(2);
    c.put(1, 10);
    c.put(2, 20);
    let _ = c.get(1);
    c.put(3, 30);
    assert_eq!(c.get(1), Some(10));
    assert_eq!(c.get(2), None);
    assert_eq!(c.get(3), Some(30));
}

#[test]
fn get_on_missing_key_does_not_change_recency() {
    let mut c = LruCache::new(2);
    c.put(1, 10);
    c.put(2, 20);
    let _ = c.get(999);  // miss
    c.put(3, 30);  // capacity exceeded; LRU is still key 1
    assert_eq!(c.get(1), None);
    assert_eq!(c.get(2), Some(20));
    assert_eq!(c.get(3), Some(30));
}

#[test]
fn stress_eviction_order_matches_recency() {
    let mut c = LruCache::new(3);
    c.put(1, 1);
    c.put(2, 2);
    c.put(3, 3);
    // recency: [1, 2, 3] (1=LRU, 3=MRU)
    let _ = c.get(1);
    // recency: [2, 3, 1] (2=LRU, 1=MRU)
    c.put(4, 4);
    // push 4, then evict LRU=2 → recency [3, 1, 4]
    assert_eq!(c.get(2), None);
    let _ = c.get(3);
    // recency: [1, 4, 3] (1=LRU, 3=MRU)
    c.put(5, 5);
    // push 5, then evict LRU=1 → recency [4, 3, 5]
    assert_eq!(c.get(1), None);
    assert_eq!(c.get(3), Some(3));
    assert_eq!(c.get(4), Some(4));
    assert_eq!(c.get(5), Some(5));
    assert_eq!(c.size(), 3);
}

#[test]
fn put_with_zero_capacity_constructs_but_first_put_panics() {
    // The acceptance.rs tests `LruCache::new(0)` panics. The agent
    // could choose to panic on construction (preferred) or on the
    // first operation; the spec says "panic if capacity < 1" at
    // construction time. We test the construction-time panic here
    // (the agent that defers it will fail).
    let result = std::panic::catch_unwind(|| LruCache::new(0));
    assert!(result.is_err());
}

#[test]
fn put_existing_key_after_eviction_works() {
    // capacity=3 (not 2) so the test's intent survives: a re-insert of
    // an evicted key must NOT cause an additional eviction on the same
    // put, and the recency ordering must put the re-inserted key as MRU.
    // With capacity=2 the test would require 4 keys to be present
    // after 4 puts, which is impossible regardless of LRU policy.
    let mut c = LruCache::new(3);
    c.put(1, 10);
    c.put(2, 20);
    c.put(3, 30);  // recency [1,2,3]
    // Evict key 1 by overflowing: get(1) misses, then put(4,4) bumps us
    // over capacity, evicting the LRU. But capacity=3 means we have
    // room for [1,2,3,4], evicting 1.
    c.put(4, 40);
    assert_eq!(c.get(1), None);
    // recency is [2,3,4]. Re-insert 1: recency=[2,3,4,1], evict 2.
    c.put(1, 11);
    // map = {3, 4, 1}, recency = [3, 4, 1]
    c.put(5, 50);
    // recency was [3,4,1], push 5 → [3,4,1,5], evict 3
    assert_eq!(c.get(3), None);
    // remaining: {4, 1, 5}
    assert_eq!(c.get(1), Some(11));
    assert_eq!(c.get(4), Some(40));
    assert_eq!(c.get(5), Some(50));
}
