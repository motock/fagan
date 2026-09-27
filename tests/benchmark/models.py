"""Model configurations for the pipeline benchmark matrix.

Each entry maps a benchmark model name to the environment the cell runs under.
The dispatch backend and concrete model are the only things that change between
cells; everything else (autonomy, review backend, isolation) is fixed by the
harness so cross-model rows are apples-to-apples.
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

# Helper to build a local Ollama cell.

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

# Helper to build a non-Ollama local provider cell.

def _local_provider(
    provider: str, tag: str, *, endpoint: str,
    temperature: str | None = None, num_ctx: str | None = None,
) -> dict:
    """Like _local, but for a non-Ollama LocalInferenceProvider (lmstudio,
    mlx) - see inference_providers.py and MODEL_PROVIDER_ABSTRACTION_PLAN.md
    S3. Sets PIPELINE_LOCAL_ENDPOINT explicitly (each provider's default port
    differs from Ollama's :11434 and OllamaDriver.__init__ always reads
    PIPELINE_LOCAL_ENDPOINT, never self.provider.default_endpoint) and
    PIPELINE_LOCAL_MODEL_DEFAULT to the provider's own model id - an MLX/LM
    Studio Hugging Face repo id (e.g. "google/gemma-4-e4b") has no ':', so
    _resolve_local_model's tag-vs-tier heuristic is a non-issue here (only
    the *tier* string, always "sonnet", is checked for ':' - the resolved
    default value is passed through as-is regardless of its shape).
    """
    env = {
        **_LOCAL_AGENT_ENV,
        "PIPELINE_LOCAL_PROVIDER": provider,
        "PIPELINE_LOCAL_ENDPOINT": endpoint,
        "PIPELINE_LOCAL_MODEL_DEFAULT": tag,
    }
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

# Shared window and dispatch timeout for the qwen-vs-gpt-oss comparison.
_ARM_NUM_CTX = "49152"
_ARM_ENV = {
    "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
}

# Helper for the comparison arms.

def _local_arm(tag: str, *, extra: dict | None = None) -> dict:
    """A local comparison arm: the shared window and budget, plus per-model extras.

    Distinct from _local: only the entries built here opt into the wider
    window, so _local's own defaults are unchanged for every other cell.
    """
    env = {**_local(tag, num_ctx=_ARM_NUM_CTX)["env"], **_ARM_ENV, **(extra or {})}
    return {"mock": False, "env": env}

# Model definitions.
MODELS: dict[str, dict] = {
    # --- local (Ollama) ---
    "devstral": _local(os.environ.get("BENCH_DEVSTRAL_TAG", "devstral:24b")),
    "minimax": _local(os.environ.get("BENCH_MINIMAX_TAG", "minimax-m3:cloud")),
    # 27.3B, Q4_K_M, native tool-calling + MTP (speculative decoding), 17GB on
    # disk. Modelfile default num_ctx is 105000 but the shared harness default
    # (16384) is kept here for a first, comparable-cost run; override via
    # BENCH_QWEN36CODER_TAG / rerun with num_ctx= if it needs more context to
    # complete real tasks.
    "qwen36coder": _local(os.environ.get(
        "BENCH_QWEN36CODER_TAG",
        "SetneufPT/Qwen3.6-27B-CODER-MTP_Q4_105k_24GB-GPU:latest",
    )),
    # Qwen3.6-27B base (dense, hybrid thinking) via Ollama, in NON-thinking
    # mode. Unlike qwen3-coder (MoE, inherently non-thinking), the dense 27B
    # is a hybrid model that emits  Mattis... Mattis blocks by default — which
    # break the tool-calling loop (the driver sees no native tool_call and
    # spins "no tool call" until the step cap). PIPELINE_LOCAL_THINK=false
    # passes "think": false to /api/chat, suppressing the block at the source
    # so the model emits clean native tool calls. The Q3_K_M quant (13 GB)
    # fits the ~17.3 GiB Ollama ceiling on a 24 GB Mac with ~4 GB headroom
    # (comfortable for 16k KV cache); override BENCH_QWEN36_TAG for a different
    # quant — q4 (16 GB, tight) or iq3 (11 GB, very safe). Tool calling uses
    # Ollama's qwen3_coder parser; all quants emit valid tool-call JSON when
    # "think": false is passed (per batiai/qwen3.6-27b model docs).
    "qwen36": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_QWEN36_TAG", "batiai/qwen3.6-27b:q3"))[
                "env",
            ],
            "PIPELINE_LOCAL_THINK": "false",
        },
    },
    # qwen3-coder:30b (MoE, non-thinking, proven Metal-stable) implementing
    # AND self-reviewing - the direct qwen counterpart to gptoss_temp03,
    # same temperature/num_ctx, for a like-for-like rework-cycle comparison
    # against the gpt-oss self-review baseline established this session.
    "qwen3coder": _local(
        os.environ.get("BENCH_QWEN36CODER_TAG", "qwen3-coder:30b"),
        temperature="0.3",
        num_ctx="32768",
    ),
    "gptoss": _local(
        os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
        temperature="1.0",
        num_ctx="32768",
    ),
    "gptoss_temp03": _local(
        os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
        temperature="0.3",
        num_ctx="32768",
    ),
    # The qwen-vs-gpt-oss comparison arms. Same quant this model's other runs
    # used, widened to the shared _ARM_NUM_CTX window; think stays suppressed
    # because this model's thinking blocks break the tool-calling loop.
    "qwen36_wide": _local_arm(
        os.environ.get("BENCH_QWEN36_TAG", "batiai/qwen3.6-27b:q3"),
        extra={"PIPELINE_LOCAL_THINK": "false"},
    ),
    # The gpt-oss counterpart, on the tag production actually dispatches.
    # PIPELINE_LOCAL_THINK is deliberately NOT set: gpt-oss-20b-high's own
    # default (medium) is what production runs, and forcing "false" here would
    # handicap the arm for the sake of symmetry.
    "gptoss_high": _local_arm(
        os.environ.get("BENCH_GPTOSS_HIGH_TAG", "gpt-oss-20b-high:latest"),
    ),
}
