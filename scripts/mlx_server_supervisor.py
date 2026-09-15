"""Ensure mlx_lm.server is running with the configured model.

Unlike Ollama (an always-on daemon that lazy-loads/swaps any requested tag)
or LM Studio (its own app, JIT-loads on first request), mlx_lm.server has no
daemon story at all - nothing in this codebase starts it, and nothing
restarts it if it crashes. This script closes that gap: run it periodically
(see launchd/com.fagan.pipeline.mlx-supervisor.plist, mirroring the
advance-scheduler pattern) to start the server if it isn't reachable.

Config via env vars (no CLI flags, matching the advance-scheduler's plist
-c invocation style):
    MLX_SERVER_MODEL_PATH - required. Passed verbatim as `--model` to
        mlx_lm.server, so it must be the exact same string every dispatch
        request should omit ("model") to hit - see MLXProvider.chat()'s
        docstring for why the request body never sends "model" at all.
    MLX_SERVER_PORT - default "8080".
    MLX_SERVER_PYTHON - required. Interpreter to launch the server with
        (must have mlx-lm installed). No default: a bare "python3" resolves
        to whatever's first on PATH, which is not guaranteed to have mlx-lm
        installed - see the repo's `.venv-mlx` (created via `uv venv
        --python 3.14 .venv-mlx && uv pip install --python
        .venv-mlx/bin/python3 mlx-lm==0.31.3`) for a self-contained,
        version-pinned interpreter to point this at, instead of borrowing
        an unrelated project's venv.
    MLX_SERVER_LOG_PATH - where mlx_lm.server's own stdout/stderr goes;
        default "<repo root>/mlx-server.log". Not to be confused with this
        supervisor script's own StandardOutPath/StandardErrorPath in the
        launchd plist - this is the log of the server subprocess it starts.
    MLX_PROMPT_CACHE_SIZE - default "1". Passed as mlx_lm.server's
        --prompt-cache-size (max distinct KV caches held at once). The
        server's own default is 10 with no byte ceiling
        (MLX_PROMPT_CACHE_BYTES below) - on a 24GB host running a ~16GB
        model, 10 held caches at a few GB each is what turned an
        unattended multi-trial benchmark run into a real kernel panic
        (IOGPUGroupMemory.cpp:528, 2026-07-14 incident, see
        MLX_DEFAULT_PROVIDER_PLAN.md) - the LRU had room to accumulate far
        past physical memory before it ever evicted anything. Lowered from
        2 to 1 after a second, distinct panic (IOGPUGroupMemory.cpp:323,
        "remove_memory_object() memory object not found", 2026-07-14):
        LRUPromptCache.insert_cache() allocates the new (Nth) cache before
        evicting the oldest one once the LRU is over its cap, so a cap of 2
        still let a transient 3rd cache exist at the moment of eviction - a
        memory spike right at the wired-memory ceiling. A cap of 1 removes
        that transient window entirely, at the cost of one conversation's
        cache never surviving a second concurrent one.
    MLX_PROMPT_CACHE_BYTES - default "4G". Passed as mlx_lm.server's
        --prompt-cache-bytes (byte ceiling across all held caches,
        mlx_lm.utils._parse_size format, e.g. "4G"/"512M"). Belt-and-braces
        alongside MLX_PROMPT_CACHE_SIZE: bounds memory even if a single
        conversation's own KV cache is unusually large.
    MLX_PROMPT_CONCURRENCY - default "1". Passed as mlx_lm.server's
        --prompt-concurrency (mlx_lm.server's own default is 8: "when a
        request is batchable then process that many prompts in parallel").
        MLX's own issue tracker documents independent graph evaluations as
        not thread-safe (ml-explore/mlx#2133), and mlx-lm's tracker already
        has crashes tied to server-side concurrency (KV-cache
        cross-contamination in ml-explore/mlx-lm#965, a batch-merge crash in
        #754) - forcing this to 1 serializes GPU graph evaluation inside the
        server as a stopgap against the same class of bug, pending an
        upstream fix. Not proven to be this project's exact trigger; see
        MLX_DEFAULT_PROVIDER_PLAN.md for what's confirmed vs. hypothesized.
    MLX_HEALTHCHECK_TIMEOUT_SECONDS - default "30". Timeout for is_serving()'s
        real completion probe (see below) - long enough that a slow-but-alive
        server isn't mistaken for wedged, short enough that a genuinely
        wedged one (observed live 2026-07-14: hangs indefinitely, no
        response ever) is caught and restarted within one supervisor tick
        rather than blocking it.
"""
import os
import subprocess
import sys
from pathlib import Path

import httpx

MODEL_PATH = os.environ.get("MLX_SERVER_MODEL_PATH")
PORT = os.environ.get("MLX_SERVER_PORT", "8080")
PYTHON = os.environ.get("MLX_SERVER_PYTHON")
LOG_PATH = os.environ.get(
    "MLX_SERVER_LOG_PATH", str(Path(__file__).resolve().parent.parent / "mlx-server.log")
)
PROMPT_CACHE_SIZE = os.environ.get("MLX_PROMPT_CACHE_SIZE", "1")
PROMPT_CACHE_BYTES = os.environ.get("MLX_PROMPT_CACHE_BYTES", "4G")
PROMPT_CONCURRENCY = os.environ.get("MLX_PROMPT_CONCURRENCY", "1")
HEALTHCHECK_TIMEOUT = float(os.environ.get("MLX_HEALTHCHECK_TIMEOUT_SECONDS", "30"))
WRAPPER_PATH = Path(__file__).resolve().parent / "mlx_server_wrapper.py"


def is_reachable(endpoint: str, timeout: float = 5.0) -> bool:
    try:
        httpx.get(f"{endpoint}/v1/models", timeout=timeout).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def is_serving(endpoint: str, timeout: float = HEALTHCHECK_TIMEOUT) -> bool:
    """A real completion probe, not just reachability - mlx_lm.server can
    stay reachable (/v1/models answers instantly, on its own request thread)
    while every /v1/chat/completions call hangs forever. Observed live
    2026-07-14: a client disconnecting mid-generation left the server in
    exactly this state - alive, listening, permanently unable to complete a
    real request, with no crash and no log line to notice by. is_reachable()
    alone cannot catch this; only actually asking it to generate can."""
    try:
        httpx.post(
            f"{endpoint}/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=timeout,
        ).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def _kill_listening_process(port: str) -> None:
    """Best-effort: find and terminate whatever's listening on PORT so
    start_server() can bind a fresh one. Used only when is_serving() has
    already proven the current process is wedged, not merely slow - so a
    firm SIGTERM (not a negotiated shutdown mlx_lm.server has no API for
    anyway) is appropriate here. Swallows failures (no lsof, process already
    gone, permission issue): a restart that fails to free the port surfaces
    as the ensuing start_server() failing to bind, which is diagnosable from
    MLX_SERVER_LOG_PATH - better than this cleanup step itself crashing the
    supervisor tick."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], check=False, capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.split():
            try:
                os.kill(int(pid_str), 15)
            except (ProcessLookupError, ValueError):
                pass
    except (OSError, subprocess.SubprocessError):
        pass


def start_server(model_path: str, port: str) -> subprocess.Popen:
    """Launch mlx_lm.server detached, so it survives this script exiting -
    this is meant to be invoked as a short-lived periodic health check
    (e.g. every few minutes via launchd), not stay running itself.

    stdout/stderr go to LOG_PATH, not DEVNULL - a wrong interpreter, a bad
    model path, or an import error inside mlx_lm must be diagnosable from
    disk, not silently discarded (this made a real failed launch
    indistinguishable from a slow cold-load with nothing to grep).

    Launches scripts/mlx_server_wrapper.py rather than `-m mlx_lm server`
    directly, so its soft in-process memory ceiling and per-request
    instrumentation (see that module's docstring) are always in effect - the
    wrapper forwards these same CLI args on to mlx_lm.server.main() itself.

    Always passes --prompt-concurrency (PROMPT_CONCURRENCY) ahead of the
    cache flags - forced to 1 to serialize GPU graph evaluation inside the
    server (see PROMPT_CONCURRENCY's module-docstring entry for why).

    Always passes --prompt-cache-size/--prompt-cache-bytes (PROMPT_CACHE_SIZE/
    PROMPT_CACHE_BYTES) - mlx_lm.server's own defaults (10 caches, no byte
    ceiling) let its LRU accumulate well past physical memory across many
    dispatch trials with nothing evicting it, which is what preceded the
    2026-07-14 kernel panic. Bounding it here means the server self-evicts;
    no periodic restart or manual flush is needed to keep memory in check."""
    with open(LOG_PATH, "a") as log_file:
        proc = subprocess.Popen(
            [
                PYTHON, str(WRAPPER_PATH), "--model", model_path, "--port", str(port),
                "--prompt-concurrency", PROMPT_CONCURRENCY,
                "--prompt-cache-size", PROMPT_CACHE_SIZE, "--prompt-cache-bytes", PROMPT_CACHE_BYTES,
            ],
            stdout=log_file, stderr=log_file, start_new_session=True,
        )
    return proc


def ensure_running(endpoint: str, model_path: str | None, port: str) -> str:
    """Returns "already_running", "started", or "restarted_wedged". Raises
    ValueError if model_path or the interpreter is unset - refuse to guess
    which model or which python to launch rather than silently starting
    nothing or picking an arbitrary default (a bare "python3" would resolve
    to whatever's first on PATH, not necessarily one with mlx-lm installed).

    Reachable is necessary but not sufficient: a wedged server (see
    is_serving()'s docstring) stays reachable forever, so a reachability-only
    check would report "already_running" indefinitely with no real dispatch
    ever completing again. is_serving() is only worth its cost (a real
    generation, not a cheap GET) once is_reachable() has already confirmed
    there's a server there to probe."""
    if not model_path:
        raise ValueError(
            "MLX_SERVER_MODEL_PATH is not set - refusing to start mlx_lm.server "
            "without knowing which model to load"
        )
    if not PYTHON:
        raise ValueError(
            "MLX_SERVER_PYTHON is not set - refusing to start mlx_lm.server "
            "without knowing which interpreter has mlx-lm installed"
        )
    if is_reachable(endpoint):
        if is_serving(endpoint):
            return "already_running"
        _kill_listening_process(port)
        start_server(model_path, port)
        return "restarted_wedged"
    start_server(model_path, port)
    return "started"


def main() -> int:
    endpoint = f"http://localhost:{PORT}"
    try:
        result = ensure_running(endpoint, MODEL_PATH, PORT)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
