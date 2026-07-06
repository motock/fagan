// Visible acceptance oracle for the LRU-cache Rust task. Cargo runs this
// as an integration test (the `tests/` dir is auto-discovered by
// `cargo test`). The agent is told in spec.json's agent_instructions to
// write tests/test_lru_cache.rs FIRST (TDD), so this file is
// "another set of acceptance cases" the agent's tests must also pass
// against - it is a visible oracle, not a hidden one. The hidden
// groundtruth (groundtruth.rs) is a separate, independent grader
// that the agent never sees; that's what the benchmark actually
// grades on.

use bench_task::LruCache;

#[test]
fn put_then_get_returns_value() {
    let mut c = LruCache::new(2);
    c.put(1, 100);
    assert_eq!(c.get(1), Some(100));
}

#[test]
fn get_missing_key_returns_none() {
    let mut c = LruCache::new(2);
    assert_eq!(c.get(99), None);
    assert_eq!(c.size(), 0);
}

#[test]
fn update_in_place_does_not_grow_size() {
    let mut c = LruCache::new(2);
    c.put(1, 100);
    c.put(1, 200);  // update, not insert
    assert_eq!(c.size(), 1);
    assert_eq!(c.get(1), Some(200));
}

#[test]
fn capacity_one_evicts_on_every_new_key() {
    let mut c = LruCache::new(1);
    c.put(1, 100);
    c.put(2, 200);
    assert_eq!(c.get(1), None);
    assert_eq!(c.get(2), Some(200));
    assert_eq!(c.size(), 1);
}

#[test]
fn get_promotes_to_most_recently_used() {
    let mut c = LruCache::new(2);
    c.put(1, 100);
    c.put(2, 200);
    // Touch key 1 so it becomes MRU; key 2 is now LRU and should be
    // evicted on the next insert.
    assert_eq!(c.get(1), Some(100));
    c.put(3, 300);
    assert_eq!(c.get(2), None);
    assert_eq!(c.get(1), Some(100));
    assert_eq!(c.get(3), Some(300));
}

#[test]
fn zero_capacity_panics() {
    let result = std::panic::catch_unwind(|| LruCache::new(0));
    assert!(result.is_err(), "LruCache::new(0) must panic");
}
