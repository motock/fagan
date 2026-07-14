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

MLX_MEMORY_LIMIT_MB - default "22528" (22 GiB). Passed to mx.set_memory_limit()
    before mlx_lm.server.main() runs. Derived from this host's 25.77GB
    physical RAM minus ~3GB OS/other-process headroom - tune per host, this is
    not a general-purpose default.

MLX_WRAPPER_LOG_PATH - where per-request start/end + peak-memory instrumentation
    goes; default mlx-server-wrapper.log alongside this repo's other MLX logs.
"""
import logging
import os
import sys
import threading
import time
from pathlib import Path

MEMORY_LIMIT_MB = os.environ.get("MLX_MEMORY_LIMIT_MB", "22528")
LOG_PATH = os.environ.get(
    "MLX_WRAPPER_LOG_PATH", str(Path(__file__).resolve().parent.parent / "mlx-server-wrapper.log")
)

_logger = logging.getLogger("mlx_server_wrapper")


def apply_memory_limit(mx_module) -> int:
    """Sets mx's soft memory ceiling from MEMORY_LIMIT_MB, returns the byte
    value applied. Takes the mx module as a parameter (rather than importing
    mlx.core at module scope) so tests can pass a fake without mlx-lm
    installed - this project's own venv doesn't have it, only .venv-mlx does."""
    limit_bytes = int(MEMORY_LIMIT_MB) * 1024 * 1024
    mx_module.set_memory_limit(limit_bytes)
    return limit_bytes


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
    import mlx_lm.server as server_mod

    logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(message)s")

    if mx.metal.is_available():
        applied = apply_memory_limit(mx)
        _logger.info("memory limit set bytes=%d", applied)

    instrument_handler(server_mod.APIHandler, mx)

    server_mod.main()


if __name__ == "__main__":
    sys.exit(main())
