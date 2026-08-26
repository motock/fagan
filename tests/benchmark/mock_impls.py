"""Reference implementations for the benchmark harness's offline `mock`
backend (harness.py: MockBackend). Split out of harness.py to keep it
under the project's line-count target - _MOCK_IMPLS is pure string data
(one small correct reference program per benchmark task, never touched by
a real model run), imported by harness.py's MockBackend.
"""
# Correct reference implementations used ONLY by the offline `mock` backend to
# self-test the harness plumbing. Real model runs never see these; they are not
# copied into any worktree except by MockBackend. They double as executable
# documentation of each task's intended behavior.
_MOCK_IMPLS: dict[str, str] = {
    "token_bucket": '''
class TokenBucket:
    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last = float(now)

    def allow(self, tokens=1.0, now=None):
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        if now is None:
            now = self.last
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last = now
        if tokens <= self.tokens:
            self.tokens -= tokens
            return True
        return False
''',
    "ratelimiter_inspect": '''
class TokenBucket:
    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last = float(now)

    def _refilled_level(self, now):
        if now is None:
            now = self.last
        elapsed = now - self.last
        if elapsed > 0:
            return min(self.capacity, self.tokens + elapsed * self.refill_rate), now
        return self.tokens, self.last

    def allow(self, tokens=1.0, now=None):
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        level, effective_now = self._refilled_level(now)
        self.tokens = level
        self.last = effective_now
        if tokens <= self.tokens:
            self.tokens -= tokens
            return True
        return False

    def available_tokens(self, now=None):
        level, _ = self._refilled_level(now)
        return level
''',
    "ratelimiter_bugfix": '''
class RateLimiter:
    """A simple token-bucket rate limiter."""

    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last_time = float(now)

    def allow(self, cost=1.0, now=None):
        if cost < 0:
            raise ValueError("cost must be non-negative")
        current = self.last_time if now is None else float(now)
        elapsed = max(0.0, current - self.last_time)
        refill = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + refill)
        self.last_time = current
        if cost <= self.tokens:
            self.tokens -= cost
            return True
        return False
''',
    "lru_cache": '''
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._d = OrderedDict()

    def get(self, key):
        if key not in self._d:
            return None
        self._d.move_to_end(key)
        return self._d[key]

    def put(self, key, value):
        if key in self._d:
            self._d[key] = value
            self._d.move_to_end(key)
            return
        self._d[key] = value
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)

    @property
    def size(self):
        return len(self._d)
''',
    "cron_field": '''
def _parse_token(tok, lo, hi):
    if tok == "":
        raise ValueError("empty token")
    step = 1
    if "/" in tok:
        parts = tok.split("/")
        if len(parts) != 2:
            raise ValueError("bad step")
        tok, steptok = parts
        step = int(steptok)
        if step <= 0:
            raise ValueError("step must be > 0")
    if tok == "*":
        start, end = lo, hi
    elif "-" in tok[1:]:
        a, b = tok.split("-")
        start, end = int(a), int(b)
    else:
        v = int(tok)
        start = end = v
    if start > end:
        raise ValueError("reversed range")
    if start < lo or end > hi:
        raise ValueError("out of range")
    return set(range(start, end + 1, step))


def match_field(field, lo, hi):
    if lo > hi:
        raise ValueError("lo > hi")
    out = set()
    for tok in field.split(","):
        out |= _parse_token(tok, lo, hi)
    return out
''',
    "retry_backoff": '''
def backoff_delays(base, factor, cap, attempts):
    if base <= 0 or factor < 1 or cap < base or attempts < 0:
        raise ValueError("bad args")
    return [min(cap, base * factor ** i) for i in range(attempts)]


def should_retry(status_code, attempt, max_attempts):
    if max_attempts < 0 or attempt < 0:
        raise ValueError("bad args")
    retryable = status_code == 429 or 500 <= status_code <= 599
    return retryable and attempt < max_attempts
''',
    "interval_merge": '''
def merge(intervals):
    for s, e in intervals:
        if s > e:
            raise ValueError("start > end")
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
''',
    # Gap 3: ecosystem-tagged reference impls for the cargo + npm tasks.
    # Same shape as the python ones; the mock backend writes this into
    # the ecosystem's expected impl path (src/lib.rs for cargo, src/*.js
    # for npm) so the offline `mock` cell can self-test the new code
    # paths without a real model.
    "lru_cache_rs": '''
use std::collections::HashMap;

pub struct LruCache {
    capacity: usize,
    map: HashMap<i32, i32>,
    recency: Vec<i32>,  // least-recently-used first
}

impl LruCache {
    pub fn new(capacity: usize) -> Self {
        if capacity < 1 {
            panic!("capacity must be >= 1");
        }
        LruCache { capacity, map: HashMap::new(), recency: Vec::new() }
    }
    pub fn get(&mut self, key: i32) -> Option<i32> {
        let v = self.map.get(&key).copied()?;
        self.recency.retain(|k| k != &key);
        self.recency.push(key);
        Some(v)
    }
    pub fn put(&mut self, key: i32, value: i32) {
        if self.map.contains_key(&key) {
            self.map.insert(key, value);
            self.recency.retain(|k| k != &key);
            self.recency.push(key);
            return;
        }
        self.map.insert(key, value);
        self.recency.push(key);
        if self.map.len() > self.capacity {
            let evict = self.recency.remove(0);
            self.map.remove(&evict);
        }
    }
    pub fn size(&self) -> usize { self.map.len() }
}
''',
    "interval_merge_js": '''
function merge(intervals) {
  for (const [s, e] of intervals) {
    if (s > e) throw new Error("start > end");
  }
  const sorted = [...intervals].sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const [s, e] of sorted) {
    if (out.length && s <= out[out.length - 1][1] + 1) {
      out[out.length - 1][1] = Math.max(out[out.length - 1][1], e);
    } else {
      out.push([s, e]);
    }
  }
  return out;
}
module.exports = { merge };
''',
}
