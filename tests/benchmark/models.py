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


_ARM_NUM_CTX = "49152"

# Both local arms of the qwen-vs-gpt-oss comparison (tests/benchmark/_runs/
# qwen38_iq3s_*) run at an identical window and dispatch budget so that the
# model is the only variable between them. 49152 is the largest num_ctx that
# keeps this host's qwen35 KV cache 100% on GPU - 65536 already spills 256 MiB
# to CPU KV. Raising the window also means the local agent loop stops trimming
# its transcript, so the per-dispatch budget rises with it.
_ARM_ENV = {
    "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
}


def _local_arm(tag: str, *, temperature: str | None = None, extra: dict | None = None) -> dict:
    """A local comparison arm: the shared window and budget, plus per-model extras.

    Distinct from _local: only the entries built here opt into the wider
    window, so _local's own defaults are unchanged for every other cell.
    """
    env = {
        **_local(tag, temperature=temperature, num_ctx=_ARM_NUM_CTX)["env"],
        **_ARM_ENV,
        **(extra or {}),
    }
    return {"mock": False, "env": env}


# Roles a bench cell can invoke: the guided-decomposition planner, dispatch
# and test_author on the implementation path, review on the merge gate, and
# overlord if a story escalates. A cell's arm must pin all of them in its
# plan role_config, because role resolution priority is
# plan role_config -> registry["roles"][role] -> PIPELINE_BACKEND_<ROLE>
# (app/role_registry.resolve_role), so a registry that pins roles.* to
# claude/sonnet outranks every PIPELINE_BACKEND_*/PIPELINE_LOCAL_* env var
# the arm sets. Without this, an "ollama" arm silently runs Claude agents.
_BENCH_ROLES = ("planner", "dispatch", "test_author", "review", "overlord")

# The model every non-dispatch role is held at, so that a scorecard
# difference between arms is attributable to the implementing model alone.
# deepseek-v4.1-flash is what the 2026-09-27 gptoss_high baseline resolved
# for every non-dispatch role (its registry pinned dispatch=gpt-oss-20b-high
# and all other roles to this model -- see
# model_registry.local.json.ollama-backup-2026-09-30), so holding the gemma
# arm at the same reviewer reproduces the baseline's review gate exactly.
# It is also Ollama-cloud-served, which matters on a host where the arm's own
# 15 GB implementer trips the PIPELINE_LOCAL_MAX_MODEL_RAM_FRACTION gate in
# app/backend_ollama.py: review cannot then be gated behind the model under
# test being loadable twice.
_BENCH_OTHER_ROLE_MODEL = os.environ.get("BENCH_OTHER_ROLE_MODEL", "deepseek-v4.1-flash")


def _ollama_role_config(dispatch: str, *, others: str | None = None) -> dict:
    """Pin every role a bench cell can invoke to a declared ollama model.

    `dispatch` is the arm's variable (the model under test); `others` is
    held constant across arms and defaults to _BENCH_OTHER_ROLE_MODEL.

    Both are *friendly names* declared under providers.ollama.models in the
    registry in use (resolve_role resolves the tag and rejects a name that is
    not declared there), not Ollama tags.
    """
    others = others or _BENCH_OTHER_ROLE_MODEL
    return {
        role: {"provider": "ollama", "model": dispatch if role == "dispatch" else others}
        for role in _BENCH_ROLES
    }


def _published_role_config(dispatch: str | None = None) -> dict:
    """Role pins for the published matrix: the implementer is the arm's variable
    (an ollama friendly name), and every other role is held at Claude sonnet.
    dispatch=None pins every role to Claude (the all-Claude arm)."""
    return {
        role: (
            {"provider": "ollama", "model": dispatch}
            if dispatch and role == "dispatch"
            else {"provider": "claude", "model": "sonnet"}
        )
        for role in _BENCH_ROLES
    }


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

    num_ctx has no true equivalent on these OpenAI-compatible servers - both
    providers send it as `max_tokens` (an output-length budget; the actual
    context window is fixed at model-load time), per their chat() docstrings.
    """
    env = {**_LOCAL_AGENT_ENV, "PIPELINE_LOCAL_PROVIDER": provider,
           "PIPELINE_LOCAL_ENDPOINT": endpoint, "PIPELINE_LOCAL_MODEL_DEFAULT": tag}
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
            **_local(os.environ.get("BENCH_QWEN36_TAG", "batiai/qwen3.6-27b:q3"))["env"],
            "PIPELINE_LOCAL_THINK": "false",
        },
    },
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
        os.environ.get("BENCH_GPTOSS_HIGH_TAG", "gpt-oss-20b-high:latest")
    ),
    # Gemma 4 26B-A4B (MoE, QAT q4_0 GGUF) pulled straight from Hugging Face.
    # Identical window/budget to gptoss_high so the model is the only
    # variable in the comparison; no PIPELINE_LOCAL_THINK override for the
    # same reason as gptoss_high. The role pins name the registry's
    # gemma4-26b-qat entry, whose tag must be the GGUF actually on disk
    # (checked with `ollama show`; it pointed at an unpulled `gemma4:...`
    # tag until 2026-10-01, which is why this arm silently ran Claude).
    # Only dispatch is gemma -- every other role stays at the baseline's
    # reviewer, both to isolate the implementer as the variable and because
    # gemma's 14909 MB weights trip the RAM gate when it is asked to review.
    "gemma4_26b": {
        **_local_arm(
            os.environ.get(
                "BENCH_GEMMA4_TAG",
                "hf.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf:latest",
            )
        ),
        "role_config": _ollama_role_config("gemma4-26b-qat"),
        "story_model": "gemma4-26b-qat",
    },
    # Qwen3.6-35B-A3B (MoE, 3B active, Unsloth UD-IQ3_XXS GGUF, 14 GB) pulled
    # from Hugging Face. Same window/budget/role pins as gemma4_26b so the
    # implementer is the only variable against both gptoss_high and the gemma
    # arm. PIPELINE_LOCAL_THINK=false is required, not a symmetry call: this
    # model advertises the `thinking` capability (`ollama show` lists it, and
    #  thinking is a stop token), and the qwen36 arms above document that an
    # un-suppressed thinking block makes the driver see no native tool_call
    # and spin "no tool call" until the step cap.
    # Its 14 GB of weights sit under the PIPELINE_LOCAL_MAX_MODEL_RAM_FRACTION
    # threshold (14746 MB on this 24 GB host) where gemma's 14909 MB do not, so
    # unlike gemma this arm could self-review; review is still held at the
    # baseline reviewer to keep the two arms comparable.
    "qwen36_35b_a3b": {
        **_local_arm(
            os.environ.get(
                "BENCH_QWEN36_35B_TAG",
                "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS",
            ),
            extra={"PIPELINE_LOCAL_THINK": "false"},
        ),
        "role_config": _ollama_role_config("qwen36-35b-a3b"),
        "story_model": "qwen36-35b-a3b",
    },
    # The same arm at the temperature this model's own card recommends for
    # non-thinking use (0.7), against the shared bench default of 0.3. The
    # 0.3 run parked 4 of 12 cells on the read-heavy repetition guard: the
    # model emitted the identical bare `view_file` three times, took the
    # nudge, re-emitted it, and was parked -- 64-75s in, impl file untouched,
    # on exactly the two tasks whose first useful move is "read the spec's
    # test file". A degenerate fixpoint like that is a low-entropy sampling
    # artifact, so temperature is the knob; everything else (model, window,
    # budget, think flag, role pins) is held identical so the probe isolates
    # it.
    "qwen36_35b_a3b_temp07": {
        **_local_arm(
            os.environ.get(
                "BENCH_QWEN36_35B_TAG",
                "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS",
            ),
            temperature="0.7",
            extra={"PIPELINE_LOCAL_THINK": "false"},
        ),
        "role_config": _ollama_role_config("qwen36-35b-a3b"),
        "story_model": "qwen36-35b-a3b",
    },
    # The third point on the same sweep, one step PAST the 0.7 the model's own
    # card recommends. 0.7 cleared every repetition park but not the reading
    # fixation behind them: the one cell that stopped re-emitting the identical
    # `view_file` switched to `cat`-ing the same file and tripped the separate
    # read-heavy guard instead. The open question this arm answers is whether
    # that residual fixation is temperature-sensitive at all, or whether it is
    # a capability floor that hotter sampling only perturbs -- which is why the
    # 0.7 write-up argued for raising the read-heavy guard rather than pushing
    # temperature further. Running it is the only way to tell those apart.
    #
    # 0.9 is ABOVE the card's recommended 0.7 for non-thinking use, so this is
    # deliberately outside the tuned range; a degradation here is a legitimate
    # finding about the recommendation, not a bug in the arm. Everything except
    # temperature is held identical to qwen36_35b_a3b_temp07 (model, 49152
    # window, 1800s budget, think=false, all five role pins), so the three arms
    # 0.3/0.7/0.9 read as a series.
    "qwen36_35b_a3b_temp09": {
        **_local_arm(
            os.environ.get(
                "BENCH_QWEN36_35B_TAG",
                "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS",
            ),
            temperature="0.9",
            extra={"PIPELINE_LOCAL_THINK": "false"},
        ),
        "role_config": _ollama_role_config("qwen36-35b-a3b"),
        "story_model": "qwen36-35b-a3b",
    },
    # Same 0.7 arm as above, with thinking turned back ON at the graded level
    # `high` instead of suppressed. Every other qwen36 arm here runs
    # think=false, and the reason is documented on `qwen36_35b_a3b`: this model
    # advertises the `thinking` capability, and an un-suppressed block makes
    # the driver see no native tool_call. That is a claim about what happens
    # when thinking is *left alone*, not a measurement of what a graded level
    # does -- Ollama's `think: "high"` is a different request from an absent
    # `think` key, and `high` is the level the surrounding arms' suppression
    # was actually trading against. Running it is the only way to see whether
    # the suppression was load-bearing or merely cheap insurance, and it is
    # the natural control for the temperature sweep: if reasoning depth buys
    # the `lru_cache_rs` cell that neither 0.7 nor 0.9 could, the bottleneck
    # was never sampling entropy.
    #
    # Everything except PIPELINE_LOCAL_THINK is held identical to
    # qwen36_35b_a3b_temp07 (model, 0.7, 49152 window, 1800s budget, all five
    # role pins), so a scorecard difference against that arm is attributable
    # to reasoning depth alone.
    "qwen36_35b_a3b_temp07_think_high": {
        **_local_arm(
            os.environ.get(
                "BENCH_QWEN36_35B_TAG",
                "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS",
            ),
            temperature="0.7",
            extra={"PIPELINE_LOCAL_THINK": "high"},
        ),
        "role_config": _ollama_role_config("qwen36-35b-a3b"),
        "story_model": "qwen36-35b-a3b",
    },
    # The lower dose of the probe above. `high` cost three cells against
    # qwen36_35b_a3b_temp07 and, more tellingly, re-introduced the per-target
    # repetition park that 0.7 had driven to zero -- while producing no
    # read-heavy parks at all, so both ends of the temperature sweep and the
    # thinking arm land on the same guard from opposite directions. That leaves
    # the dose-response shape unresolved: it is consistent with "any thinking at
    # all is harmful" (monotone) and equally with an inverted U whose peak sits
    # below `high`. `medium` is the midpoint that discriminates the two, and it
    # is the only single point that does -- `low` sits adjacent to `false`, so a
    # `low` result close to the control cannot separate "flat" from "peak at
    # off".
    #
    # Everything except PIPELINE_LOCAL_THINK is held identical to
    # qwen36_35b_a3b_temp07 (model, 0.7, 49152 window, 1800s budget, all five
    # role pins), and two unit tests assert it, one of which resolves the token
    # through `_tuned_think` so a typo cannot silently run a different
    # experiment.
    "qwen36_35b_a3b_temp07_think_medium": {
        **_local_arm(
            os.environ.get(
                "BENCH_QWEN36_35B_TAG",
                "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS",
            ),
            temperature="0.7",
            extra={"PIPELINE_LOCAL_THINK": "medium"},
        ),
        "role_config": _ollama_role_config("qwen36-35b-a3b"),
        "story_model": "qwen36-35b-a3b",
    },
    # Qwen3.8-27B (dense 27.3B, Unsloth UD-IQ3_XXS GGUF, 11 GB) pulled from
    # Hugging Face, at the temperature the model's own card recommends for
    # non-thinking mode. Qwen3.8 is a hybrid thinking model and its card gives
    # two sets: 0.7/top_p 0.80/presence_penalty 1.5 for instruct (non-thinking)
    # and 1.0/top_p 0.95/presence_penalty 0.0 for thinking -- the same numbers
    # on the Unsloth GGUF card, the Unsloth guide, and Qwen's upstream card.
    # This arm is the non-thinking one (think=false, as for every qwen hybrid
    # here), so 0.7 it is; note that is NOT the shared bench default of 0.3,
    # which is what the earlier qwen38 run used.
    #
    # Same window/budget/role pins as qwen36_35b_a3b_temp07, so the two arms
    # differ only in the model tag -- but note its nearest like-for-like
    # comparison is the existing qwen38_wide run, which measured a DIFFERENT
    # quant of this same base model (ISTA-DASLab GSQ-RCO IQ3_S) at 0.3 on the
    # same 4 tasks: 11/12 success, 12/12 GT-pass, 410s mean, 37.5 ticks. So a
    # difference between that run and this arm confounds quant with
    # temperature; only the model tag differs from qwen36_35b_a3b_temp07.
    #
    # The card's recommended non-thinking set also includes top_p 0.80 and
    # presence_penalty 1.5, and explicitly suggests tuning presence_penalty
    # "to reduce endless repetition" -- the exact failure mode the qwen36 0.3
    # arm showed. Neither is settable: the driver sends only num_ctx and
    # temperature (OllamaDriver._chat), and no PIPELINE_LOCAL_* env var
    # exposes them. Wiring them is a driver change, deliberately not smuggled
    # into this arm.
    #
    # Its 11 GB of weights sit under the PIPELINE_LOCAL_MAX_MODEL_RAM_FRACTION
    # threshold (14746 MB on this 24 GB host), so unlike gemma it could
    # self-review; review stays at the baseline reviewer to keep the arms
    # comparable.
    "qwen38_27b": {
        **_local_arm(
            os.environ.get(
                "BENCH_QWEN38_27B_TAG",
                "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS",
            ),
            temperature="0.7",
            extra={"PIPELINE_LOCAL_THINK": "false"},
        ),
        "role_config": _ollama_role_config("qwen38-27b"),
        "story_model": "qwen38-27b",
    },
    # qwen3-coder:30b (MoE, non-thinking, proven Metal-stable) implementing
    # AND self-reviewing - the direct qwen counterpart to gptoss_temp03,
    # same temperature/num_ctx, for a like-for-like rework-cycle comparison
    # against the gpt-oss self-review baseline established this session.
    "qwen3coder": _local(os.environ.get("BENCH_QWEN36CODER_TAG", "qwen3-coder:30b"),
                          temperature="0.3", num_ctx="32768"),
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
    # Asymmetric review on qwen3-coder:30b (MoE, non-thinking, proven Metal-
    # stable across 7 runs this session - unlike the dense Qwen3.6-27B, which
    # crashes/hangs on both LM Studio and Ollama). Same dispatch settings as
    # gptoss_temp03 (gpt-oss:20b implements locally); review routed to
    # qwen3-coder:30b instead of gpt-oss reviewing its own weights. Tests
    # whether a 30B MoE model can catch the kind of subtle correctness bug
    # Claude caught in the token_bucket trials (refill double-counting on a
    # rejected request) that the acceptance oracle itself missed.
    "gptoss_qwen36coder_review": {
        "mock": False,
        "env": {
            **_local(os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b"),
                     temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_BACKEND_REVIEW": "local",
            "PIPELINE_LOCAL_REVIEW_MODEL": os.environ.get(
                "BENCH_QWEN36CODER_TAG", "qwen3-coder:30b"
            ),
        },
    },
    # --- local (LM Studio) ---
    # gemma-4-e4b is the same model LMStudioProvider's docstring was
    # live-validated against (basic complete(), a tool-calling round trip,
    # and a review loop that converged to a real VERDICT: APPROVE). Requires
    # `lms server start` running locally with the model downloaded
    # (`lms ps` should list it) - the harness does not start LM Studio itself.
    "lmstudio_gemma4": _local_provider(
        "lmstudio",
        os.environ.get("BENCH_LMSTUDIO_TAG", "google/gemma-4-e4b"),
        endpoint=os.environ.get("BENCH_LMSTUDIO_ENDPOINT", "http://localhost:1234"),
    ),
    # --- local (mlx_lm.server) ---
    # Default tag is the tiny 1.5B model MLXProvider's docstring was
    # validated against - it proved the wire protocol works (tool calls,
    # complete()) but did NOT converge in a 5-step review loop, a
    # model-quality limit not a wiring bug. Expect weak dispatch signal at
    # this size; override BENCH_MLX_TAG with a larger MLX-served model for a
    # real benchmark run. Requires mlx_lm.server already running locally.
    "mlx": _local_provider(
        "mlx",
        os.environ.get("BENCH_MLX_TAG", "mlx-community/Qwen2.5-1.5B-Instruct-4bit"),
        endpoint=os.environ.get("BENCH_MLX_ENDPOINT", "http://localhost:8080"),
    ),
    # --- published matrix arms (tests/benchmark/run_published_matrix.sh) ---
    # Arm B: a cloud open-weight model implements, Claude plays every other role.
    "glm_claude_review": {
        "mock": False,
        "env": {
            **_local("glm-5.3-flash:cloud", temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
        },
        "role_config": _published_role_config("glm"),
        "story_model": "glm",
    },
    # Arm C at the shipped step budget (60) and at double it (120). Same
    # timeout in both so the step budget is the only variable.
    "gptoss_claude_review_s60": {
        "mock": False,
        "env": {
            **_local("gpt-oss:20b", temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
            "PIPELINE_LOCAL_MAX_STEPS": "60",
        },
        "role_config": _published_role_config("gpt-oss"),
        "story_model": "gpt-oss",
    },
    "gptoss_claude_review_s120": {
        "mock": False,
        "env": {
            **_local("gpt-oss:20b", temperature="0.3", num_ctx="32768")["env"],
            "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
            "PIPELINE_LOCAL_MAX_STEPS": "120",
        },
        "role_config": _published_role_config("gpt-oss"),
        "story_model": "gpt-oss",
    },
    # --- cloud (claude CLI) ---
    # Arm A: Claude implements and reviews. role_config pins every role so a
    # host registry that routes a role elsewhere cannot change the arm.
    "sonnet": {
        "mock": False,
        "env": {
            "PIPELINE_BACKEND_DISPATCH": "claude",
        },
        "role_config": _published_role_config(),
    },
    # --- offline self-test of the harness plumbing (no model/network) ---
    "mock": {
        "mock": True,
        "env": {
            "PIPELINE_BACKEND_DISPATCH": "local",
        },
    },
}
