"""Mutant corpus for the test-author benchmark (story TA-1).

A mutant is a minimal, requirement-targeted edit to a task's correct
reference implementation (tests/benchmark/mock_impls.py). The grader
(test_author_grade.py) runs an authored suite against each mutant: a
suite that still passes has failed to test that requirement.

The corpus is DATA plus one tiny pure helper. Each mutant's
``mutated_source`` is the reference source with exactly ONE semantic
change, produced by a single literal substring replacement so the
one-edit property is checkable by inspection and by the unit tests.
"""

from dataclasses import dataclass

from tests.benchmark.mock_impls import _MOCK_IMPLS as _REFERENCE_IMPLS

TASKS = ("token_bucket", "ratelimiter_inspect", "lru_cache", "ratelimiter_bugfix")


@dataclass(frozen=True)
class Mutant:
    """One requirement-targeted mutation of a task's reference impl."""

    name: str  # stable slug, e.g. 'token_bucket/refill_not_capped'
    task: str
    requirement: str  # the requirement it violates, e.g. 'req 2'
    kind: str  # 'positive' | 'negative' | 'boundary' | 'edge'
    mutated_source: str  # full replacement source for the task's impl file


def _mutant(task: str, slug: str, requirement: str, kind: str, old: str, new: str) -> Mutant:
    """Build a mutant by replacing the FIRST occurrence of ``old`` in the
    task's reference source. Raises if the anchor is missing so a typo can
    never silently produce a no-op mutant."""
    base = _REFERENCE_IMPLS[task]
    if old not in base:
        raise AssertionError(f"mutant {task}/{slug}: anchor not found in reference source")
    return Mutant(
        name=f"{task}/{slug}",
        task=task,
        requirement=requirement,
        kind=kind,
        mutated_source=base.replace(old, new, 1),
    )


# --- token_bucket -----------------------------------------------------------
_TOKEN_BUCKET = [
    _mutant("token_bucket", "init_accepts_nonpositive", "req 1", "negative",
            '        if capacity <= 0 or refill_rate <= 0:\n'
            '            raise ValueError("capacity and refill_rate must be > 0")\n',
            ""),
    _mutant("token_bucket", "starts_empty", "req 2", "boundary",
            "        self.tokens = float(capacity)",
            "        self.tokens = 0.0"),
    _mutant("token_bucket", "refill_not_capped", "req 3", "boundary",
            "            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)",
            "            self.tokens = self.tokens + elapsed * self.refill_rate"),
    _mutant("token_bucket", "refill_frozen", "req 3", "positive",
            "        elapsed = now - self.last",
            "        elapsed = 0.0"),
    _mutant("token_bucket", "grant_does_not_consume", "req 4", "positive",
            "            self.tokens -= tokens",
            "            pass"),
    _mutant("token_bucket", "exact_request_denied", "req 4", "boundary",
            "        if tokens <= self.tokens:",
            "        if tokens < self.tokens:"),
    _mutant("token_bucket", "deny_grants", "req 5", "negative",
            "        if tokens <= self.tokens:",
            "        if True:"),
    _mutant("token_bucket", "negative_tokens_accepted", "req 6", "negative",
            "        if tokens < 0:",
            "        if False:"),
]

# --- ratelimiter_inspect ----------------------------------------------------
_RATELIMITER_INSPECT = [
    _mutant("ratelimiter_inspect", "init_accepts_nonpositive", "req 1", "negative",
            '        if capacity <= 0 or refill_rate <= 0:\n'
            '            raise ValueError("capacity and refill_rate must be > 0")\n',
            ""),
    _mutant("ratelimiter_inspect", "starts_empty", "req 2", "boundary",
            "        self.tokens = float(capacity)",
            "        self.tokens = 0.0"),
    _mutant("ratelimiter_inspect", "refill_not_capped", "req 3", "boundary",
            "            return min(self.capacity, self.tokens + elapsed * self.refill_rate), now",
            "            return self.tokens + elapsed * self.refill_rate, now"),
    _mutant("ratelimiter_inspect", "refill_frozen", "req 3", "positive",
            "        elapsed = now - self.last",
            "        elapsed = 0.0"),
    _mutant("ratelimiter_inspect", "grant_does_not_consume", "req 4", "positive",
            "            self.tokens -= tokens",
            "            pass"),
    _mutant("ratelimiter_inspect", "exact_request_denied", "req 4", "boundary",
            "        if tokens <= self.tokens:",
            "        if tokens < self.tokens:"),
    _mutant("ratelimiter_inspect", "deny_grants", "req 5", "negative",
            "        if tokens <= self.tokens:",
            "        if True:"),
    _mutant("ratelimiter_inspect", "negative_tokens_accepted", "req 6", "negative",
            "        if tokens < 0:",
            "        if False:"),
    _mutant("ratelimiter_inspect", "available_tokens_ignores_refill", "req 7", "positive",
            "        level, _ = self._refilled_level(now)\n        return level",
            "        return self.tokens"),
]

# --- lru_cache --------------------------------------------------------------
_LRU_CACHE = [
    _mutant("lru_cache", "zero_capacity_allowed", "req 1", "boundary",
            "        if capacity < 1:",
            "        if capacity < 0:"),
    _mutant("lru_cache", "get_missing_raises", "req 2", "negative",
            "            return None",
            "            raise KeyError(key)"),
    _mutant("lru_cache", "get_no_recency_update", "req 3", "positive",
            "        self._d.move_to_end(key)\n        return self._d[key]",
            "        return self._d[key]"),
    _mutant("lru_cache", "put_update_no_recency", "req 4", "positive",
            "            self._d[key] = value\n            self._d.move_to_end(key)\n            return",
            "            self._d[key] = value\n            return"),
    _mutant("lru_cache", "put_update_dropped", "req 4", "negative",
            "        if key in self._d:\n            self._d[key] = value",
            "        if key in self._d:\n            pass"),
    _mutant("lru_cache", "evict_newest", "req 5", "negative",
            "            self._d.popitem(last=False)",
            "            self._d.popitem(last=True)"),
    _mutant("lru_cache", "no_eviction", "req 5", "negative",
            "        if len(self._d) > self.capacity:",
            "        if False:"),
    _mutant("lru_cache", "evict_early", "req 5", "boundary",
            "        if len(self._d) > self.capacity:",
            "        if len(self._d) >= self.capacity:"),
    _mutant("lru_cache", "size_returns_capacity", "req 6", "positive",
            "        return len(self._d)",
            "        return self.capacity"),
]

# --- ratelimiter_bugfix -----------------------------------------------------
_RATELIMITER_BUGFIX = [
    _mutant("ratelimiter_bugfix", "init_accepts_nonpositive", "req 1", "negative",
            '        if capacity <= 0 or refill_rate <= 0:\n'
            '            raise ValueError("capacity and refill_rate must be positive")\n',
            ""),
    _mutant("ratelimiter_bugfix", "starts_empty", "req 2", "boundary",
            "        self.tokens = float(capacity)",
            "        self.tokens = 0.0"),
    _mutant("ratelimiter_bugfix", "refill_not_capped", "req 3", "boundary",
            "        self.tokens = min(self.capacity, self.tokens + refill)",
            "        self.tokens = self.tokens + refill"),
    _mutant("ratelimiter_bugfix", "refill_frozen", "req 3", "positive",
            "        refill = elapsed * self.refill_rate",
            "        refill = 0.0"),
    _mutant("ratelimiter_bugfix", "grant_does_not_consume", "req 4", "positive",
            "            self.tokens -= cost",
            "            pass"),
    _mutant("ratelimiter_bugfix", "exact_request_denied", "req 4", "boundary",
            "        if cost <= self.tokens:",
            "        if cost < self.tokens:"),
    _mutant("ratelimiter_bugfix", "deny_grants", "req 5", "negative",
            "        if cost <= self.tokens:",
            "        if True:"),
    _mutant("ratelimiter_bugfix", "negative_cost_accepted", "req 6", "negative",
            "        if cost < 0:",
            "        if False:"),
    _mutant("ratelimiter_bugfix", "elapsed_unclamped", "req 7", "boundary",
            "        elapsed = max(0.0, current - self.last_time)",
            "        elapsed = current - self.last_time"),
]

MUTANTS: dict[str, list[Mutant]] = {
    "token_bucket": _TOKEN_BUCKET,
    "ratelimiter_inspect": _RATELIMITER_INSPECT,
    "lru_cache": _LRU_CACHE,
    "ratelimiter_bugfix": _RATELIMITER_BUGFIX,
}