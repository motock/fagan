"""Wrapper entrypoint for mlx_lm.server that adds a soft in-process memory
ceiling and per-request instrumentation. mlx_server_supervisor.py launches
this in place of `python -m mlx_lm server`.

Why: mlx_lm.server hardcodes mx.set_wired_limit(max_recommended_working_set_size)
(server.py:1889) with no CLI flag to lower it, and a ~19GB model resident on a
~25.77GB host leaves almost no headroom before the kernel's IOGPUGroupMemory
bookkeeping panics the whole host (incidents 2026-07-13 and 2026-07-14, see
MLX_DEFAULT_PROVIDER_PLAN.md). mx.set_memory_limit() is a softer ceiling MLX
enforces itself before handing more work to the GPU driver - crossing it fails
the allocation in-process (a crash the supervisor can restart) instead of
reaching the kernel panic path. This is a mitigation, not a proven fix - the
per-request instrumentation below exists to find out, from real logs, whether
memory pressure, concurrent requests, or both are the actual trigger.

MLX_MEMORY_LIMIT_MB - default "14336" (14 GiB). Passed to mx.set_memory_limit()
    before mlx_lm.server.main() runs. The prior 22528 (22 GiB) default on this
    host's 25.77GB physical RAM left only ~3GB headroom, which still panicked
    (2026-07-16 IOGPUGroupMemory panic, ~700MB-2GB free for 6 minutes before
    the crash) - the soft limit failed to trip before the kernel's GPU driver
    bookkeeping bug did. 14336 keeps wired memory in the ~15-16GB range this
    host has run stable at for hours (see MLX_DEFAULT_PROVIDER_PLAN.md /
    project_mlx_24gb_footprint_ceiling memory) - tune per host, this is not a
    general-purpose default.

MLX_WRAPPER_LOG_PATH - where per-request start/end + peak-memory instrumentation
    goes; default mlx-server-wrapper.log alongside this repo's other MLX logs.
"""
import logging
import os
import sys
import threading
import time
from pathlib import Path

MEMORY_LIMIT_MB = os.environ.get("MLX_MEMORY_LIMIT_MB", "14336")
LOG_PATH = os.environ.get(
    "MLX_WRAPPER_LOG_PATH", str(Path(__file__).resolve().parent.parent / "mlx-server-wrapper.log")
)

_logger = logging.getLogger("mlx_server_wrapper")


def configure_logging() -> None:
    """Gives _logger its own file handler instead of calling
    logging.basicConfig() - basicConfig() configures the ROOT logger, and
    mlx_lm.server.main() makes its own basicConfig() call right after this
    module hands off to it (server.py ~1892). logging.basicConfig() is a
    no-op if the root logger already has handlers, so claiming the root
    logger here would silently swallow mlx_lm's own structured logging
    (Prompt Cache/Prompt processing progress lines) into this wrapper's log
    instead of mlx-server.log where every prior incident's analysis expects
    to find them. Idempotent - safe to call more than once (e.g. from
    tests) without accumulating duplicate handlers/duplicate log lines."""
    if not _logger.handlers:
        handler = logging.FileHandler(LOG_PATH)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False


def apply_memory_limit(mx_module) -> int:
    """Sets mx's soft memory ceiling from MEMORY_LIMIT_MB, returns the byte
    value applied. Takes the mx module as a parameter (rather than importing
    mlx.core at module scope) so tests can pass a fake without mlx-lm
    installed - this project's own venv doesn't have it, only .venv-mlx does."""
    limit_bytes = int(MEMORY_LIMIT_MB) * 1024 * 1024
    mx_module.set_memory_limit(limit_bytes)
    return limit_bytes


def serialize_generation(handler_class) -> None:
    """Wraps handler_class.do_POST in a shared threading.Lock so at most one
    generation request executes at a time, independent of mlx_lm.server's own
    --prompt-concurrency flag. mlx_lm.server runs on a ThreadingHTTPServer
    with no lock anywhere in its own source (confirmed by reading
    mlx_lm/server.py) - --prompt-concurrency only bounds how many prompts its
    internal batch generator merges into one forward pass, it does not stop
    two separate HTTP threads from both being inside a generation call
    simultaneously. MLX's own issue tracker documents concurrent graph
    evaluation as unsafe (ml-explore/mlx#2133); this closes that gap at the
    HTTP-handler level rather than trusting the server's internal batching
    knob to do it. do_GET is left untouched - it never touches the GPU
    (e.g. /v1/models), so serializing it too would needlessly queue cheap
    health-check polling behind a real generation in flight."""
    lock = threading.Lock()
    original_do_post = handler_class.do_POST

    def locked_do_post(self, _original=original_do_post):
        with lock:
            return _original(self)

    handler_class.do_POST = locked_do_post


def patch_lru_prompt_cache_evict_before_insert(cache_module) -> None:
    """Monkeypatches mlx_lm.models.cache.LRUPromptCache.insert_cache so the
    LRU-oldest entry is evicted BEFORE a new key is added, not after.

    The shipped ordering (confirmed by reading
    .venv-mlx/lib/python3.14/site-packages/mlx_lm/models/cache.py directly)
    adds the new entry to the trie first and only then checks whether the
    LRU is over max_size - so any never-before-seen key briefly holds
    max_size+1 entries' GPU-backed cache arrays alive at once, regardless of
    what max_size is configured to. Lowering MLX_PROMPT_CACHE_SIZE 2->1
    (2026-07-14) shrank that window's absolute size but could not close it -
    a cap of 1 still transiently holds 2. That transient spike, sitting right
    at mlx_lm.server's wired-memory ceiling, is the leading hypothesis for
    the repeated IOGPUGroupMemory kernel panics on this host (see
    MLX_DEFAULT_PROVIDER_PLAN.md, Phase 4 item 1).

    Only reorders WHEN eviction happens for a new key; delegates to the
    original method for everything else (existing-key updates, prefix
    trimming, the trailing max_bytes safety net), so the library's own
    trie/byte-accounting is reused unchanged rather than reimplemented."""
    cls = cache_module.LRUPromptCache
    original_insert_cache = cls.insert_cache

    def patched_insert_cache(self, model, tokens, prompt_cache, *, cache_type="assistant"):
        is_new_key = self._trie.search(model, tokens).exact is None
        if is_new_key and self.max_size > 0 and len(self._lru) >= self.max_size:
            evict_model, evict_tokens = self._lru.pop()
            evict_entry = self._trie.pop(evict_model, evict_tokens)
            self._n_bytes -= evict_entry.nbytes
            self._n_bytes_by_type[evict_entry.cache_type] -= evict_entry.nbytes
        return original_insert_cache(self, model, tokens, prompt_cache, cache_type=cache_type)

    cls.insert_cache = patched_insert_cache


def instrument_handler(handler_class, mx_module) -> None:
    """Wraps handler_class's do_GET/do_POST in place to log start/end + peak
    memory around every request. Purely observational - does not change
    response behavior or swallow exceptions. The handler methods run per HTTP
    request on whichever thread ThreadingHTTPServer assigns it, so start/end
    log lines (with thread name) are enough to tell, after the fact, whether
    two requests ever overlapped in flight, and the peak-memory line shows how
    close each request got to MEMORY_LIMIT_MB."""
    for method_name in ("do_GET", "do_POST"):
        original = getattr(handler_class, method_name)

        def wrapped(self, _original=original, _method_name=method_name):
            thread = threading.current_thread().name
            started = time.monotonic()
            _logger.info("start method=%s path=%s thread=%s", _method_name, self.path, thread)
            try:
                return _original(self)
            finally:
                elapsed = time.monotonic() - started
                peak = mx_module.get_peak_memory()
                _logger.info(
                    "end method=%s path=%s thread=%s elapsed=%.3f peak_bytes=%d",
                    _method_name, self.path, thread, elapsed, peak,
                )

        setattr(handler_class, method_name, wrapped)


def main() -> None:
    import mlx.core as mx
    import mlx_lm.models.cache as cache_mod
    import mlx_lm.server as server_mod

    configure_logging()

    if mx.metal.is_available():
        applied = apply_memory_limit(mx)
        _logger.info("memory limit set bytes=%d", applied)

    patch_lru_prompt_cache_evict_before_insert(cache_mod)

    # instrument_handler() and serialize_generation() are deliberately NOT wired
    # in here anymore (2026-07-14). They were Phase 3 stopgaps against the kernel
    # panics, but the panics were actually resolved by right-sizing to a model
    # whose footprint (~9-15GB) sits well under the wired-memory ceiling - not by
    # the lock. Worse, they were the direct cause of ~950s dispatch stalls: the
    # instrumentation's per-request mx.get_peak_memory() call blocks for minutes
    # while serialize_generation()'s lock is held across it, wedging every
    # subsequent request until the harness kills the cell (see
    # MLX_DEFAULT_PROVIDER_PLAN.md, run4 diagnosis). The functions are kept
    # defined (and unit-tested) for reference, but must stay out of the hot path.

    server_mod.main()


if __name__ == "__main__":
    sys.exit(main())
