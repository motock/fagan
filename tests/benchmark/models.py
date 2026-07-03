"""Model configurations for the pipeline benchmark matrix.

Each entry maps a benchmark model name to the environment the cell runs under.
The dispatch backend and concrete model are the only things that change between
cells; everything else (autonomy, review backend, isolation) is fixed by the
harness so cross-model rows are apples-to-apples.

Local cells set PIPELINE_LOCAL_MODEL_DEFAULT and leave the per-tier overrides
unset, so the story's "sonnet" tier falls through to this default (see
backend._resolve_local_model). Cloud cells pin the dispatch backend to claude
and let the story's tier ("sonnet") select the model.

`endpoint`/`tag` for local models must match what `ollama list` actually serves
on this host -- verify before a full matrix run.
"""
from __future__ import annotations

import os

# Local Ollama endpoint shared by all local cells.
_OLLAMA = os.environ.get("BENCH_OLLAMA_ENDPOINT", "http://localhost:11434")

# Shared local-agent knobs: bound each cell so a stuck model can't run forever.
#
# These use the PIPELINE_LOCAL_* names (not LOCAL_AGENT_*) because that's what
# OllamaDriver actually reads from os.environ (backend.py's __init__ and its
# per-dispatch/per-call re-reads) — LOCAL_AGENT_* are the driver's own names
# for the *child* subprocess env it constructs, not env vars it consumes, so
# setting them here was a silent no-op that only happened to look right
# because the defaults matched.
_LOCAL_AGENT_ENV = {
    "PIPELINE_LOCAL_ENDPOINT": _OLLAMA,
    "PIPELINE_LOCAL_NUM_CTX": "16384",
    "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "900",
    "PIPELINE_LOCAL_TEMPERATURE": "0.3",
    "PIPELINE_LOCAL_MAX_STEPS": "60",
}


def _local(tag: str, *, temperature: str | None = None, num_ctx: str | None = None) -> dict:
    env = {**_LOCAL_AGENT_ENV, "PIPELINE_LOCAL_MODEL_DEFAULT": tag}
    if temperature is not None:
        env["PIPELINE_LOCAL_TEMPERATURE"] = str(temperature)
    if num_ctx is not None:
        env["PIPELINE_LOCAL_NUM_CTX"] = str(num_ctx)
    return {
        "mock": False,
        "env": {
            "PIPELINE_BACKEND_DISPATCH": "local",
            **env,
        },
    }


MODELS: dict[str, dict] = {
    # --- local (Ollama) ---
    "devstral": _local(os.environ.get("BENCH_DEVSTRAL_TAG", "devstral:24b")),
    "minimax": _local(os.environ.get("BENCH_MINIMAX_TAG", "minimax-m3:cloud")),
    "gptoss": _local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"), temperature="1.0", num_ctx="32768"),
    # --- cloud (claude CLI) ---
    "sonnet": {
        "mock": False,
        "env": {
            "PIPELINE_BACKEND_DISPATCH": "claude",
        },
    },
    # --- offline self-test of the harness plumbing (no model/network) ---
    "mock": {
        "mock": True,
        "env": {
            "PIPELINE_BACKEND_DISPATCH": "local",
        },
    },
}
