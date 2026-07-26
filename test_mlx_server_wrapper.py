"""Tests for the mlx_lm.server wrapper entrypoint (scripts/mlx_server_wrapper.py).

mlx/mlx_lm are not installed in this project's venv (they only exist in
.venv-mlx) - apply_memory_limit()/instrument_handler() take the mx module and
handler class as parameters rather than importing them, so these tests can
exercise the real logic against fakes with no dependency on mlx-lm being
importable here.
"""
import importlib.util
import itertools
import logging
import threading
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "mlx_server_wrapper", str(Path(__file__).parent / "scripts" / "mlx_server_wrapper.py")
)
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)


class _FakeMx:
    def __init__(self):
        self.limit_set_to = None
        self.peak_memory = 12345

    def set_memory_limit(self, limit_bytes):
        self.limit_set_to = limit_bytes

    def get_peak_memory(self):
        return self.peak_memory


def test_apply_memory_limit_converts_mb_to_bytes(monkeypatch):
    monkeypatch.setattr(wrapper, "MEMORY_LIMIT_MB", "22528")
    fake_mx = _FakeMx()

    applied = wrapper.apply_memory_limit(fake_mx)

    assert applied == 22528 * 1024 * 1024
    assert fake_mx.limit_set_to == 22528 * 1024 * 1024


def test_apply_memory_limit_honors_configured_value(monkeypatch):
    monkeypatch.setattr(wrapper, "MEMORY_LIMIT_MB", "1024")
    fake_mx = _FakeMx()

    applied = wrapper.apply_memory_limit(fake_mx)

    assert applied == 1024 * 1024 * 1024
    assert fake_mx.limit_set_to == 1024 * 1024 * 1024


def test_configure_logging_does_not_configure_root_logger(monkeypatch, tmp_path):
    """logging.basicConfig() configures the ROOT logger and is a no-op if the
    root logger already has handlers - mlx_lm.server.main() makes its own
    basicConfig() call right after this module hands off to it, and if we'd
    claimed the root logger first, that call would silently do nothing,
    rerouting mlx_lm's own Prompt Cache/progress logging into this wrapper's
    log file instead of mlx-server.log. Confirmed live 2026-07-14."""
    root = logging.getLogger()
    root_handlers_before = list(root.handlers)
    monkeypatch.setattr(wrapper, "LOG_PATH", str(tmp_path / "wrapper.log"))
    monkeypatch.setattr(wrapper._logger, "handlers", [])

    wrapper.configure_logging()

    assert root.handlers == root_handlers_before


def test_configure_logging_is_idempotent(monkeypatch, tmp_path):
    """Calling it twice must not accumulate duplicate handlers (which would
    duplicate every log line)."""
    monkeypatch.setattr(wrapper, "LOG_PATH", str(tmp_path / "wrapper.log"))
    monkeypatch.setattr(wrapper._logger, "handlers", [])

    wrapper.configure_logging()
    wrapper.configure_logging()

    assert len(wrapper._logger.handlers) == 1


class _FakeHandler:
    do_GET_calls = []  # noqa: RUF012 (existing test; not modified per workflow rule against touching tests without approval)
    do_POST_calls = []  # noqa: RUF012 (existing test; not modified per workflow rule against touching tests without approval)

    def __init__(self, path):
        self.path = path

    def do_GET(self):
        _FakeHandler.do_GET_calls.append(self.path)
        return "get-result"

    def do_POST(self):
        _FakeHandler.do_POST_calls.append(self.path)
        return "post-result"


def test_instrument_handler_preserves_return_value():
    _FakeHandler.do_GET_calls = []
    _FakeHandler.do_POST_calls = []
    fake_mx = _FakeMx()

    wrapper.instrument_handler(_FakeHandler, fake_mx)

    handler = _FakeHandler("/v1/models")
    assert handler.do_GET() == "get-result"
    assert handler.do_POST() == "post-result"


def test_instrument_handler_still_calls_original_method():
    _FakeHandler.do_GET_calls = []
    _FakeHandler.do_POST_calls = []
    fake_mx = _FakeMx()

    wrapper.instrument_handler(_FakeHandler, fake_mx)

    handler = _FakeHandler("/v1/chat/completions")
    handler.do_POST()

    assert _FakeHandler.do_POST_calls == ["/v1/chat/completions"]


def test_instrument_handler_logs_start_and_end_with_thread_name(caplog):
    _FakeHandler.do_GET_calls = []
    _FakeHandler.do_POST_calls = []
    fake_mx = _FakeMx()
    fake_mx.peak_memory = 999

    wrapper.instrument_handler(_FakeHandler, fake_mx)

    with caplog.at_level(logging.INFO, logger="mlx_server_wrapper"):
        handler = _FakeHandler("/v1/chat/completions")
        handler.do_POST()

    messages = [r.message for r in caplog.records]
    assert any("start" in m and "/v1/chat/completions" in m for m in messages)
    assert any("end" in m and "peak_bytes=999" in m for m in messages)
    assert any(threading.current_thread().name in m for m in messages)


def test_instrument_handler_propagates_exception_and_still_logs_end(caplog):
    class _FailingHandler:
        def do_GET(self):
            return "get-result"

        def do_POST(self):
            raise RuntimeError("boom")

    fake_mx = _FakeMx()
    wrapper.instrument_handler(_FailingHandler, fake_mx)

    handler = _FailingHandler()
    handler.path = "/v1/chat/completions"

    with caplog.at_level(logging.INFO, logger="mlx_server_wrapper"):
        try:
            handler.do_POST()
            raised = False
        except RuntimeError:
            raised = True

    assert raised is True
    messages = [r.message for r in caplog.records]
    assert any("end" in m for m in messages)


class _SlowHandler:
    """Simulates do_POST taking real wall-clock time inside the generation
    call, so concurrent invocations can be observed overlapping (or not)."""

    intervals = []  # noqa: RUF012 (existing test; not modified per workflow rule against touching tests without approval)

    def __init__(self, path="/v1/chat/completions"):
        self.path = path

    def do_GET(self):
        return "get-result"

    def do_POST(self):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        _SlowHandler.intervals.append((start, end))
        return "post-result"


def _intervals_overlap(intervals):
    ordered = sorted(intervals)
    for (start_a, end_a), (start_b, _end_b) in itertools.pairwise(ordered):
        if start_b < end_a:
            return True
    return False


def test_serialize_generation_prevents_concurrent_do_post(monkeypatch):
    """mlx_lm.server's ThreadingHTTPServer spawns one thread per connection
    with no lock anywhere in its own source (confirmed by reading
    mlx_lm/server.py) - --prompt-concurrency only controls its internal batch
    generator, not whether two HTTP threads can both be inside a generation
    call at once. Two concurrent do_POST calls must never overlap once
    serialize_generation has wrapped the handler."""
    monkeypatch.setattr(_SlowHandler, "intervals", [])
    wrapper.serialize_generation(_SlowHandler)

    threads = [threading.Thread(target=_SlowHandler().do_POST) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_SlowHandler.intervals) == 4
    assert _intervals_overlap(_SlowHandler.intervals) is False


def test_serialize_generation_does_not_lock_do_get(monkeypatch):
    """do_GET (e.g. /v1/models) never touches the GPU - locking it too would
    needlessly serialize cheap health-check polling behind a real generation
    in flight."""
    monkeypatch.setattr(_SlowHandler, "intervals", [])
    wrapper.serialize_generation(_SlowHandler)

    handler = _SlowHandler("/v1/models")
    assert handler.do_GET() == "get-result"


def test_serialize_generation_preserves_do_post_return_value(monkeypatch):
    monkeypatch.setattr(_SlowHandler, "intervals", [])
    wrapper.serialize_generation(_SlowHandler)

    assert _SlowHandler().do_POST() == "post-result"


# --- patch_lru_prompt_cache_evict_before_insert -----------------------------
#
# Fakes mirror the real mlx_lm.models.cache.LRUPromptCache structure closely
# enough to reproduce its actual bug (verified 2026-07-14 by reading
# .venv-mlx/lib/python3.14/site-packages/mlx_lm/models/cache.py directly):
# insert_cache() adds the new entry to the trie, THEN checks whether the LRU
# is over max_size and evicts - so a new, never-seen key briefly coexists in
# memory with the entry it's about to displace. The patch reorders this to
# evict first. Only the ordering is under test here, not the real library's
# trie/byte-accounting, which the patch deliberately leaves untouched by
# delegating to the original method.


class _FakeCacheEntry:
    def __init__(self, nbytes, cache_type="assistant"):
        self.nbytes = nbytes
        self.cache_type = cache_type


class _FakeSearchResult:
    def __init__(self, exact):
        self.exact = exact


class _FakeTrie:
    """Tracks which keys are currently "alive" (GPU-backed cache data still
    referenced) at the trie level, not inside insert_cache - add()/pop() are
    the only two operations that actually create/destroy an entry, and both
    the patch's pre-eviction and the original method's post-eviction path
    call these same two methods, so tracking here observes the true
    concurrent-alive count regardless of which one evicts."""

    def __init__(self):
        self._store = {}
        self.alive_keys = set()
        self.max_concurrent_alive = 0

    def search(self, model, tokens):
        key = (model, tuple(tokens))
        return _FakeSearchResult(exact=tokens if key in self._store else None)

    def add(self, model, tokens, entry):
        key = (model, tuple(tokens))
        prev = self._store.get(key)
        self._store[key] = entry
        if prev is None:
            self.alive_keys.add(key)
            self.max_concurrent_alive = max(self.max_concurrent_alive, len(self.alive_keys))
        return prev

    def pop(self, model, tokens):
        key = (model, tuple(tokens))
        self.alive_keys.discard(key)
        return self._store.pop(key)

    def pop_prefixes(self, model, tokens):
        return []


class _FakeCacheOrder:
    def __init__(self):
        self._order = []

    def __len__(self):
        return len(self._order)

    def push(self, model, tokens, cache_type="assistant"):
        self._order.append((model, tuple(tokens)))

    def remove(self, model, tokens):
        self._order.remove((model, tuple(tokens)))

    def pop(self):
        return self._order.pop(0)


class _FakeLRUPromptCache:
    """Reproduces the real library's evict-AFTER-insert ordering exactly
    (mirrors mlx_lm/models/cache.py's insert_cache), including tracking which
    keys currently hold "GPU-backed" cache data so tests can observe how many
    are simultaneously alive - the real-world quantity a kernel panic at the
    wired-memory ceiling actually cares about."""

    def __init__(self, max_size=1, max_bytes=1 << 63):
        self.max_size = max_size
        self.max_bytes = max_bytes
        self._trie = _FakeTrie()
        self._lru = _FakeCacheOrder()
        self._n_bytes = 0
        self._n_bytes_by_type = {"assistant": 0, "user": 0, "system": 0}

    @property
    def max_concurrent_alive(self):
        return self._trie.max_concurrent_alive

    def insert_cache(self, model, tokens, prompt_cache, *, cache_type="assistant"):
        entry = _FakeCacheEntry(nbytes=1, cache_type=cache_type)

        self._n_bytes += entry.nbytes
        self._n_bytes_by_type[cache_type] += entry.nbytes
        prev = self._trie.add(model, tokens, entry)
        if prev is not None:
            self._n_bytes -= prev.nbytes
            self._n_bytes_by_type[prev.cache_type] -= prev.nbytes
            self._lru.remove(model, tokens)
        self._lru.push(model, tokens, cache_type)

        if len(self._lru) > self.max_size:
            evict_model, evict_tokens = self._lru.pop()
            evict_entry = self._trie.pop(evict_model, evict_tokens)
            self._n_bytes -= evict_entry.nbytes
            self._n_bytes_by_type[evict_entry.cache_type] -= evict_entry.nbytes


def test_fake_lru_prompt_cache_reproduces_the_real_bug_unpatched():
    """Sanity check that the fake is faithful: with max_size=1 and two
    distinct keys inserted, the shipped (unpatched) ordering briefly holds 2
    entries alive at once, not 1."""
    cache = _FakeLRUPromptCache(max_size=1)

    cache.insert_cache("m", [1, 2, 3], [])
    cache.insert_cache("m", [4, 5, 6], [])

    assert cache.max_concurrent_alive == 2


def test_patch_lru_prompt_cache_evict_before_insert_caps_concurrent_alive():
    class _FakeCacheModule:
        LRUPromptCache = _FakeLRUPromptCache

    wrapper.patch_lru_prompt_cache_evict_before_insert(_FakeCacheModule)
    cache = _FakeCacheModule.LRUPromptCache(max_size=1)

    cache.insert_cache("m", [1, 2, 3], [])
    cache.insert_cache("m", [4, 5, 6], [])
    cache.insert_cache("m", [7, 8, 9], [])

    assert cache.max_concurrent_alive == 1


def test_patch_lru_prompt_cache_evict_before_insert_preserves_existing_key_update():
    """Re-inserting the SAME key (e.g. a longer continuation of the same
    conversation) is an update, not a new entry - must not trigger a spurious
    eviction of the entry being updated."""
    class _FakeCacheModule:
        LRUPromptCache = _FakeLRUPromptCache

    wrapper.patch_lru_prompt_cache_evict_before_insert(_FakeCacheModule)
    cache = _FakeCacheModule.LRUPromptCache(max_size=1)

    cache.insert_cache("m", [1, 2, 3], [])
    cache.insert_cache("m", [1, 2, 3], [])  # same key again

    assert len(cache._lru) == 1
    assert cache.max_concurrent_alive == 1
