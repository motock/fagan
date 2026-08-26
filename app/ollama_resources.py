"""Host/server resource probes OllamaDriver.resource_status() uses: total
physical RAM, a model tag's on-disk weight size, currently-loaded models,
and Ollama's runtime serving parallelism. Split out of app/backend_ollama.py
purely to keep that file under the project's line-count target;
OllamaDriver itself stays in backend_ollama.py and re-exports these names,
since resource_status() references them as bare names.
"""
import re
import subprocess

import httpx

from app import inference_providers


def _total_memory_mb() -> int | None:
    """Total physical RAM in MB via macOS's `sysctl hw.memsize`.

    Used by `resource_status()`'s model-size gate as the denominator for
    "is this model simply too big for this machine" - a stable figure, unlike
    free memory, which fluctuates with whatever else is running.

    Returns None (never raises) on any subprocess/parse failure, including a
    non-macOS host without this sysctl key - the caller then skips the gate
    rather than guessing.
    """
    try:
        result = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see _ollama_loaded_models)
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip()) // (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# Cache for _ollama_model_weights_mb, keyed (endpoint, model_tag). A tag's
# on-disk size is effectively immutable, and resource_status() runs on every
# scheduler tick, so re-probing /api/tags each time is pure waste. Staleness
# tradeoff: re-pulling the SAME tag with a different quantization would keep
# the old figure until the process restarts. That is acceptable for a
# fail-open advisory gate and is the normal cost of caching here; None
# results are deliberately NOT cached so a transient probe failure (or a
# model pulled after startup) is retried on the next tick.
_OLLAMA_MODEL_WEIGHTS_CACHE: dict[tuple[str, str], int] = {}


def _ollama_model_weights_mb(endpoint: str, model_tag: str) -> int | None:
    """Best-effort on-disk weight size (MB) for a specific Ollama model tag,
    read from `/api/tags` (every locally-pulled model, each with a `size` in
    bytes - a close proxy for its resident footprint once loaded, since
    Ollama's GGUF/safetensors weights are already quantized on disk).

    Used by `resource_status()`'s model-size gate. Ollama-specific: LM Studio
    JIT-loads with no equivalent listing, and mlx_lm.server pins one model for
    the server's whole lifetime (already covered by its own provider-scoped
    floor override) - never called for another provider.

    A cloud-served tag (e.g. `glm-5.2:cloud`) legitimately reports size 0 and
    is returned as 0, not None: it has no local footprint to gate on.

    Returns None (never raises) on any network/parse failure or when the tag
    isn't in the local model list - the caller then skips the gate, matching
    every other component's fail-open contract.
    """
    cached = _OLLAMA_MODEL_WEIGHTS_CACHE.get((endpoint, model_tag))
    if cached is not None:
        return cached
    try:
        resp = httpx.get(f"{endpoint}/api/tags", timeout=5)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    for entry in payload.get("models", []) or []:
        name = entry.get("name") or entry.get("model")
        if name != model_tag:
            continue
        size = entry.get("size")
        if isinstance(size, (int, float)):
            mb = int(size // (1024 * 1024))
            _OLLAMA_MODEL_WEIGHTS_CACHE[(endpoint, model_tag)] = mb
            return mb
    return None


def _ollama_loaded_models(endpoint: str) -> set[str]:
    """Return the set of model names currently loaded in Ollama's memory.

    Used by `dispatch_story` to warn when a multi-model concurrent dispatch
    is about to force Ollama to swap a different model into VRAM. Always
    checks Ollama specifically (not self.provider / PIPELINE_LOCAL_PROVIDER):
    this is a VRAM-swap warning, a concept specific to Ollama's one-process-
    many-models memory model (LM Studio JIT-loads instead, and mlx_lm.server
    is one model per process with nothing to swap) - not a leftover from
    dispatch() itself being non-provider-aware (see
    MODEL_PROVIDER_ABSTRACTION_PLAN.md S3).

    Network / parse failures are swallowed: the function is a
    observability hook, not a gate. Returning an empty set is fine; the
    caller will then see "nothing loaded" and skip the mismatch warning
    (a same-model dispatch is safe regardless of what's loaded).
    """
    return inference_providers.OllamaProvider().loaded_models(endpoint)


def _ollama_serving_parallelism() -> int | None:
    """Detect Ollama's actual serving parallelism at runtime.

    Ollama spawns one ``llama-server`` runner process per loaded model and
    passes it ``-np N`` (the ``OLLAMA_NUM_PARALLEL`` value the server was
    started with). ``N`` is the number of concurrent decode slots on that
    runner: with ``N=1`` a second in-flight request queues behind the
    first, and with ``MAX_CONCURRENT_AGENTS>1`` the pipeline will dispatch
    a second agent that then blocks on that queue until the 180s
    read-silence timeout fires ("LLM call failed: timed out" - Mode 2).

    The value is read from the live process table rather than trusted to
    stay in sync with ``OLLAMA_NUM_PARALLEL``: that env var is set via
    ``launchctl setenv`` and is silently dropped whenever Ollama.app
    auto-updates and relaunches (observed 2026-07-25, v0.32.4). A runtime
    probe is the only signal that survives an upgrade the user didn't
    initiate.

    Returns the smallest ``-np`` across all running ``llama-server``
    processes (the binding constraint on concurrent dispatch when multiple
    models are loaded), or ``None`` if no runner is running, ``ps`` fails,
    or no ``-np`` flag can be parsed. ``None`` means "unknown" - callers
    must NOT treat it as zero, which would false-warn on every first
    dispatch; they skip the warning instead. This is an observability
    hook, never a gate: any failure returns ``None`` rather than raising.
    """
    try:
        result = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see _ollama_loaded_models)
            ["ps", "-axo", "pid,command"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # `-np N` is whitespace-delimited; anchor on a leading boundary so we
    # don't match a longer flag like `-npX` or a substring of another arg.
    np_re = re.compile(r"(?:^|\s)-np\s+(\d+)(?:\s|$)")
    smallest: int | None = None
    for line in result.stdout.splitlines():
        if "llama-server" not in line:
            continue
        m = np_re.search(line)
        if not m:
            continue
        val = int(m.group(1))
        if smallest is None or val < smallest:
            smallest = val
    return smallest


__all__ = [
    "_OLLAMA_MODEL_WEIGHTS_CACHE",
    "_ollama_loaded_models",
    "_ollama_model_weights_mb",
    "_ollama_serving_parallelism",
    "_total_memory_mb",
]
