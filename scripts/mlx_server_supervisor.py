"""Ensure mlx_lm.server is running with the configured model.

Unlike Ollama (an always-on daemon that lazy-loads/swaps any requested tag)
or LM Studio (its own app, JIT-loads on first request), mlx_lm.server has no
daemon story at all - nothing in this codebase starts it, and nothing
restarts it if it crashes. This script closes that gap: run it periodically
(see launchd/com.claude.pipeline.mlx-supervisor.plist, mirroring the
advance-scheduler pattern) to start the server if it isn't reachable.

Config via env vars (no CLI flags, matching the advance-scheduler's plist
-c invocation style):
    MLX_SERVER_MODEL_PATH - required. Passed verbatim as `--model` to
        mlx_lm.server, so it must be the exact same string every dispatch
        request should omit ("model") to hit - see MLXProvider.chat()'s
        docstring for why the request body never sends "model" at all.
    MLX_SERVER_PORT - default "8080".
    MLX_SERVER_PYTHON - interpreter to launch the server with (must have
        mlx-lm installed); default "python3".
"""
import os
import subprocess
import sys

import httpx

MODEL_PATH = os.environ.get("MLX_SERVER_MODEL_PATH")
PORT = os.environ.get("MLX_SERVER_PORT", "8080")
PYTHON = os.environ.get("MLX_SERVER_PYTHON", "python3")


def is_reachable(endpoint: str, timeout: float = 5.0) -> bool:
    try:
        httpx.get(f"{endpoint}/v1/models", timeout=timeout).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def start_server(model_path: str, port: str) -> subprocess.Popen:
    """Launch mlx_lm.server detached, so it survives this script exiting -
    this is meant to be invoked as a short-lived periodic health check
    (e.g. every few minutes via launchd), not stay running itself."""
    return subprocess.Popen(
        [PYTHON, "-m", "mlx_lm", "server", "--model", model_path, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def ensure_running(endpoint: str, model_path: str | None, port: str) -> str:
    """Returns "already_running" or "started". Raises ValueError if
    model_path is unset - refuse to guess which model to launch rather than
    silently starting nothing or picking an arbitrary default."""
    if not model_path:
        raise ValueError(
            "MLX_SERVER_MODEL_PATH is not set - refusing to start mlx_lm.server "
            "without knowing which model to load"
        )
    if is_reachable(endpoint):
        return "already_running"
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
