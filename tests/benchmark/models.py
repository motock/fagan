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
    # 27.3B, Q4_K_M, native tool-calling + MTP (speculative decoding), 17GB on
    # disk. Modelfile default num_ctx is 105000 but the shared harness default
    # (16384) is kept here for a first, comparable-cost run; override via
    # BENCH_QWEN36CODER_TAG / rerun with num_ctx= if it needs more context to
    # complete real tasks.
    "qwen36coder": _local(os.environ.get(
        "BENCH_QWEN36CODER_TAG",
        "SetneufPT/Qwen3.6-27B-CODER-MTP_Q4_105k_24GB-GPU:latest",
    )),
    "gptoss": _local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"), temperature="1.0", num_ctx="32768"),
    "gptoss_temp03": _local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"), temperature="0.3", num_ctx="32768"),
    # Same dispatch/review settings as gptoss_temp03, but PIPELINE_BACKEND_
    # DISPATCH=auto instead of "local" - so a story whose local dispatch
    # fails, or whose local review exhausts its rework/inconclusive budget,
    # escalates to Claude (real, non-mocked Claude usage) instead of parking
    # for a human. Validates the escalate-to-Claude mechanism live; expect
    # meaningfully higher "done" counts than gptoss_temp03's pure-local run
    # at the cost of consuming Claude usage on escalated cells.
    "gptoss_temp03_auto": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
                     temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_BACKEND_DISPATCH": "auto",
        },
    },
    # Asymmetric review: same dispatch settings as gptoss_temp03, but review
    # runs on devstral:24b instead of gpt-oss reviewing its own work with
    # identical weights (both software-engineer.md and code-reviewer.md
    # declare model: sonnet, so without PIPELINE_LOCAL_REVIEW_MODEL the two
    # roles resolve to the same concrete model - see _run_reviewer).
    # PIPELINE_BACKEND_REVIEW=local is baked in here (not left to the
    # invoking shell) so this config can't be run mis-set the way the
    # 2026-07-03 temp=0.3 experiment's first attempt was.
    "gptoss_devstral_review": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
                     temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_BACKEND_REVIEW": "local",
            "PIPELINE_LOCAL_REVIEW_MODEL": os.environ.get("BENCH_DEVSTRAL_TAG", "devstral:24b"),
        },
    },
    # Asymmetric review on minimax-m3:cloud (the existing minimax cell on its
    # own has a known history: read-loop-parks on one story, trips the
    # per-target guard on another - so the implementation role was never
    # fully green; we're testing it in the reviewer role instead, where the
    # failure mode is safe (no clean verdict -> UNKNOWN -> never false
    # auto-merge). This validates the gpt-oss-implements / minimax-reviews
    # combination live. Same dispatch settings as gptoss_temp03, with
    # PIPELINE_BACKEND_REVIEW=local and the review model pinned here so
    # neither is left to the invoking shell.
    "gptoss_minimax_review": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
                     temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_BACKEND_REVIEW": "local",
            "PIPELINE_LOCAL_REVIEW_MODEL": os.environ.get("BENCH_MINIMAX_TAG", "minimax-m3:cloud"),
        },
    },
    # Asymmetric review on glm-5.2:cloud. Same dispatch as gptoss_temp03
    # (gpt-oss:20b implements locally, no Claude usage consumed), with
    # PIPELINE_BACKEND_REVIEW=local and the review model pinned to
    # glm-5.2:cloud (an Ollama cloud-hosted model comparable to Claude in
    # capability per the user's read of the model card; also capable of
    # native tool-calling, verified live). Used to measure the cost impact
    # of the driver-level token-spend fixes (memory: user dropped on
    # reviewers, --max-tokens cap, tightened persona prose) when the
    # reviewer model is a cloud model the user is NOT rate-limited on -
    # glm-5.2:cloud's /api/chat response carries the same
    # prompt_eval_count / eval_count fields as Anthropic's API, so the
    # measurement transfers 1:1 to a real Claude review run once the
    # weekly limit resets.
    "gptoss_glm_review": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
                     temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_BACKEND_REVIEW": "local",
            "PIPELINE_LOCAL_REVIEW_MODEL": os.environ.get("BENCH_GLM_TAG", "glm-5.2:cloud"),
        },
    },
    # Cloud-review counterpart to gptoss_glm_review. Same dispatch path
    # (gpt-oss:20b implements locally, no Claude usage consumed) but the
    # review role runs on Claude itself (PIPELINE_BACKEND_REVIEW=claude,
    # no PIPELINE_LOCAL_REVIEW_MODEL). Used to measure the cloud review's
    # real per-call token cost after the driver-level fixes (drop
    # memory: user on reviewers, --max-tokens 4096 cap, tightened
    # persona prose) - the headline number for the token-cost comparison
    # report at docs/benchmarks/2026-07-06-token-cost-comparison.md.
    "gptoss_claude_review": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
                     temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_BACKEND_REVIEW": "claude",
        },
    },
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
