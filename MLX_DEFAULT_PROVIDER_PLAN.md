# Plan: Make MLX the default local inference provider

> **Status (2026-07-14):** S1 live-validated. `mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit`
> (17.2GB, manually downloaded — `huggingface_hub`'s own multi-connection download path stalled
> indefinitely twice in a row on this host for both this repo and the earlier Devstral attempt;
> a plain sequential `curl` per-file download worked reliably instead, worth carrying forward as
> the default download method here) served via `mlx_lm.server`, dispatched through the existing
> `LOCAL_AGENT_PROVIDER=mlx` plumbing with **zero new code** — first real MLX dispatch success on
> this host. `token_bucket` benchmark cell: model wrote a correct `TokenBucket` implementation +
> its own tests in 2 steps (~191s total), hidden acceptance oracle passed (`groundtruth_passed:
> true`), and Claude's review caught a genuine double-refill/rate-limit-bypass bug the acceptance
> suite didn't exercise (`REQUEST_CHANGES`, verified against the actual diff - not a review-gate
> false positive). This closes G1/G2 for this specific model: the earlier Devstral (missing tool
> template) and Qwen3-Coder (incompatible XML tool format) dead ends are now bypassed by picking
> a model with a complete, standard tool-calling template out of the box - see the corrected G1
> section below for the full model-compatibility research trail.
>
> **3 trials total on this model, 3/3 GT-correct:** `token_bucket` t0 (parked on the real
> reviewer finding above), `token_bucket` t1 (`done`/`merged`/`APPROVE`, 179.5s, 6 ticks),
> `ratelimiter_inspect` t0 (`done`/`merged`/`APPROVE`, 204.3s, 6 ticks) - 2/3 merged cleanly, 1/3
> correctly blocked pre-merge. Consistent, fast (~180-205s/trial) across two different T1 task
> types. Strongest local-dispatch result of any model tried on this host to date (higher and
> more consistent than devstral:24b/gpt-oss:20b/qwen3-coder:30b's historical Ollama numbers -
> see `[[project_provider_dispatch_s3]]` for those baselines; a controlled Ollama-side re-run
> for a true head-to-head is deferred, not done this session).
>
> **New operational gap found and worked around, not yet fixed in code:** `mlx_lm.server`'s
> request `"model"` field must **exactly** match its `--model` launch argument (or be omitted)
> to reuse the preloaded weights — any other string (including the model's own metadata id, the
> same string `/v1/models` reports) makes the server treat it as an unrecognized model and
> attempt a fresh resolution/fetch, which is what caused two apparent "stalled/failed load"
> incidents that were actually this mismatch, not a broken download. Confirmed via `ModelProvider
> .load()`'s source: it only skips reloading when the request's model string maps (via
> `default_model_map`) to the exact value passed at server launch. **Practical requirement for
> S5 (flip the default):** `PIPELINE_LOCAL_MODEL_DEFAULT` for the `mlx` provider must be set to
> the exact same string used to launch `mlx_lm.server --model <X>` (a local path in this
> validation), not an HF repo id, or every real dispatch would silently hang on this same
> mismatch. Worth a defensive fix in `MLXProvider`/docs before S5, tracked as follow-up.
>
> **Also confirmed via direct model-file inspection (research, not yet acted on):** `gpt-oss`
> MLX builds use OpenAI's Harmony format (`<|channel|>...to=functions.X...<|call|>`) for tool
> calls — verified by reading the actual chat template's tool-call rendering block — which
> neither `mlx_lm.server`'s native parser nor `local_agent.py`'s `recover_tool_calls()` fallback
> recognizes. Confirmed incompatible without a dedicated Harmony parser (a bigger, gpt-oss-family-
> only lift); not pursued this session given a working alternative (Qwen3) was available.

## Goal
`PIPELINE_LOCAL_PROVIDER` currently defaults to `ollama`, and `devstral:24b` (Ollama) is the
only local model with a proven, reliable dispatch track record. The user's own comparison of
Ollama / LM Studio / MLX concluded MLX is the most promising path on paper (native Apple
Silicon support, no llama.cpp translation layer). This plan identifies why MLX isn't there yet
and sequences the work to make it the default `PIPELINE_LOCAL_PROVIDER` for dispatch — without
regressing Ollama, which stays a fully-supported explicit opt-out (no model lock-in is an
existing hard requirement, see `project_local_llm_port`). Scope is **non-thinking models only**
— hybrid-reasoning models (Qwen3.6-27B, any "Thinking" release) are deliberately out of scope,
both because Ollama's proven baseline (`devstral:24b`) is non-thinking and because the
OpenAI-compatible path has no per-request thinking toggle (see G1b).

## Current state (assessment)
MLX is architecturally wired in, not aspirational:
- `inference_providers.py`'s `MLXProvider` is real and live-validated (basic `complete()`,
  tool-calling round trip, a review loop) against `mlx_lm.server` — see
  `MODEL_PROVIDER_ABSTRACTION_PLAN.md` status header and `[[project_provider_dispatch_s3]]`.
- S3 of the original abstraction plan (provider-ized dispatch subprocess) is done:
  `scripts/local_agent.py` routes through `_provider_chat_turn` →
  `get_local_provider("mlx").chat()` when `LOCAL_AGENT_PROVIDER=mlx`.
- A `mlx` benchmark cell exists in `tests/benchmark/models.py` (`BENCH_MLX_TAG`/`_ENDPOINT`).

But **MLX has zero successful end-to-end dispatch runs on this host to date.** Two live
attempts are already sitting in `tests/benchmark/_runs/`:
- `mlx_qwen25_1.5b_live_validation`: the tiny 1.5B model ran 150 ticks / 1502s, never produced
  a usable edit, and the harness **timed out without killing the subprocess** — it was found
  orphaned and running minutes later (previously known, see `[[project_provider_dispatch_s3]]`).
- `mlx_qwen3coder30b_live_validation` and `_retry`: `mlx-community/Qwen3-Coder-30B-A3B-
  Instruct-4bit` failed **immediately**, both times — `agent.log` shows step 0 as pure prose
  with no tool call, then an empty step 1. `final_status: failed`, zero impl/test changes.
  This result was not yet in memory; found by reading the run artifacts directly this session.

## Gaps

**G1 — Tool-call wire-format compatibility is the real gate on which MLX model can work at
all, and it varies per model family in ways that aren't obvious from benchmark reputation
alone.** Verified directly against each model's real tokenizer/chat-template files (not
assumed) plus `mlx_lm/server.py`'s actual parser source (read in a throwaway venv):

- `mlx_lm/server.py`'s native tool-call extraction only understands **one** convention: text
  between `<tool_call>`/`</tool_call>` tokens must be `json.loads`-able
  (`{"name": ..., "arguments": {...}}`, the common Hermes/Qwen shape). Anything else falls
  through as plain `content` with empty `tool_calls`, deferring to `local_agent.py`'s
  `recover_tool_calls()` fallback (a regex-based JSON scraper).
- **`Qwen3-Coder-30B-A3B-Instruct` is confirmed incompatible** — its real chat template emits a
  bespoke XML dialect (`<tool_call>\n<function=NAME>\n<parameter=ARG>\nvalue\n</parameter>\n
  </function>\n</tool_call>`), which is neither valid JSON for the server's native parser nor a
  shape `recover_tool_calls()` recognizes. This — not "thinking" — is the actual, confirmed
  cause of both failed MLX runs on this model. It's a structural incompatibility, not a
  prompting/temperature/context issue; no per-request fix exists. **Rule this model family out.**
- **`gpt-oss-20b` (MLX) is very likely incompatible for the same class of reason, despite being
  the strongest Ollama performer.** Its real chat template uses OpenAI's "Harmony" format
  (`<|channel|>analysis/commentary/final`), confirmed present; neither `mlx_lm/server.py` nor
  `mlx_lm/tokenizer_utils.py` contains any Harmony/channel-aware parsing, and
  `recover_tool_calls()` doesn't either. Ollama's strong gpt-oss results rely on Ollama's own
  built-in Harmony translation, which `mlx_lm.server` has no equivalent for. **Treat as a
  likely land-mine — do not assume gpt-oss's Ollama track record transfers to MLX.**
- **`Devstral-Small-2505`/`-2-24B-Instruct-2512` (MLX, `mlx-community` builds) is the strong
  candidate.** Same model family as the proven `devstral:24b` Ollama baseline. Its real
  tokenizer (`special_tokens_map.json`) has `[TOOL_CALLS]` as a genuine vocabulary token, and
  the model's native convention is `[TOOL_CALLS]` followed by JSON — which is **exactly** what
  `local_agent.py`'s `recover_tool_calls()` already strips before parsing
  (`text.strip().replace("[TOOL_CALLS]", "")`), a fallback that plausibly exists *because of*
  this exact model family's behavior on Ollama already. No thinking tokens present either. Even
  if `mlx_lm.server`'s native parser doesn't recognize the bracket convention as its start/end
  markers (its native detection is written around `<tool_call>`), the existing content-recovery
  fallback should catch it. **This is the recommended first S1 candidate.**

**G1b — Qwen3 hybrid-thinking suppression is still Ollama-only, but out of scope per Goal.**
Commit `4ac633c`'s `LOCAL_AGENT_THINK` → `think: false` only touches `_ollama_payload()`;
`_provider_chat_turn`/`MLXProvider.chat`/`LMStudioProvider.chat` have no equivalent, and
`mlx_lm.server`'s `chat_template_args` (the only thinking-control knob it exposes) is a
server-*launch*-time CLI flag, not a per-request body field (confirmed in `mlx_lm/server.py`)
— so there's no way to toggle it per-request the way Ollama does. Irrelevant as long as this
plan sticks to non-thinking candidates (Devstral, Qwen-Instruct-non-thinking variants); tracked
as a follow-up only for if a thinking model is ever deliberately chosen later.

**G2 — No coding-capable model has completed one successful dispatch tick via `mlx_lm.server`
on this host.** Direct consequence of G1 (both attempted models were format-incompatible or too
weak). Core gap this plan exists to close: Ollama has a proven daily-driver (`devstral:24b`);
MLX has none yet.

**G3 — Standalone `mlx-lm` can't load newer/larger Qwen weights.** `Qwen3.6-27B` fails on two
independent repos and two `mlx-lm` versions with `Tokenizer class TokenizersBackend does not
exist` (`transformers>=5` incompatibility in `mlx-lm`'s own tokenizer registration). Moot for
the recommended Devstral path (different tokenizer stack entirely), but rules out that specific
model if ever reconsidered.

**G4 — Harness doesn't reap the dispatch subprocess on timeout.** Confirmed directly (the 1.5B
run above: orphaned past the harness's 1500s timeout, found still running, killed manually).
Not MLX-specific, but MLX's blocking, non-streaming `_provider_chat_turn` (flat
`LOCAL_AGENT_TIMEOUT`, no per-chunk stall detection the way Ollama's streaming path has) makes
MLX dispatch materially more exposed to silent hangs than Ollama.

**G5 — No per-request context control and no server lifecycle management.** `num_ctx` is faked
as `max_tokens`; true context is fixed at `mlx_lm.server` launch. Unlike Ollama (one always-on
daemon, swaps any requested tag automatically) or LM Studio (JIT-loads, has its own
guardrails/daemon), `mlx_lm.server` is one-model-per-process with nothing in this codebase to
keep the right model loaded, restart it on crash, or reflect a model mismatch in
`resource_status()`. This directly threatens the user's explicit #1 priority — autonomous,
unattended overnight continuity — if MLX becomes default.

**G6 — No apples-to-apples benchmark vs. the Ollama baseline.** Devstral MLX vs. `devstral:24b`
Ollama is actually a clean experiment design — same weights family, only the serving runtime
differs — so this is more tractable than it looked before G1's research. Still needs a real
head-to-head (`token_bucket`, `ratelimiter_inspect`) before the "MLX is more performant"
claim is anything but a hardware-spec inference.

**G7 — Mechanical default-flip work**, gated on G1-G6: `PIPELINE_LOCAL_PROVIDER` default
(`ollama` → `mlx` in `backend.py`), populate `PIPELINE_LOCAL_MODEL_DEFAULT`/`_OPUS`/`_SONNET`/
`_HAIKU` with the validated MLX model id(s), update README/CLAUDE.md. Low-risk but must come
last — flipping the default before G1-G6 land would make MLX the default with a *worse* track
record than Ollama's proven baseline.

## Stories (TDD, in order)

**S1 — DONE (2026-07-14).** Devstral MLX turned out not to be viable (see status header):
every conversion checked — `mlx-community` and LM Studio's own official upload — ships a chat
template missing the `[AVAILABLE_TOOLS]` tool-rendering block entirely, a structural gap in
how Mistral distributes these weights, not fixable by trying another upload. Validated
`mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit` instead (complete, standard tool-calling
template out of the box) — real `token_bucket` dispatch succeeded: correct implementation +
tests in 2 steps/~191s, oracle green, a real (not false-positive) reviewer finding. No new code
was needed; the existing provider plumbing worked as designed once a compatible model was
picked.

**S2 — DONE (2026-07-13).** `check_story_status` now kills+checkpoints a dispatch past a
configurable watchdog ceiling (`PIPELINE_DISPATCH_WATCHDOG_SECONDS`, default 3600s); the
benchmark harness's `drive()`/`drive_plan()` reap any still-outstanding dispatch when they give
up. Commits `6436d93`/`937fb79`.

**S3 — IN PROGRESS.** 3 trials of Qwen3-30B-A3B-Instruct-2507-MLX run so far (see status
header): 3/3 GT-correct, 2/3 merged, 1/3 correctly blocked pre-merge, ~180-205s/trial. A
controlled same-session `devstral:24b`-Ollama re-run for a true head-to-head has **not** been
done yet (deferred by user request, 2026-07-14) — `devstral:24b` isn't currently pulled on this
host. Remaining: either pull it and run the same task set for direct comparison, or continue
building up the MLX-side trial count first and compare against the historical Ollama numbers
already in `[[project_provider_dispatch_s3]]`/`[[project_gptoss_run_learnings]]`.

**S4 — DONE (2026-07-14).** `MLXProvider.chat()` no longer sends a `"model"` field in the
request body at all (mlx_lm.server serves exactly one model per process, so there's nothing to
select — and any string that doesn't exactly match the server's `--model` launch argument
triggers a hang, per the operational gap found during S1). New
`scripts/mlx_server_supervisor.py`: checks reachability, starts `mlx_lm.server` with the
configured model (`MLX_SERVER_MODEL_PATH`) if not already running; refuses to guess a model if
unset. `launchd/com.claude.pipeline.mlx-supervisor.plist` template added (mirrors the
advance-scheduler pattern, `StartInterval` 120s), not installed. `resource_status()` reporting
a model *mismatch* (as opposed to mere unreachability) was scoped out — with the model field
now never sent, MLX has no per-request model-selection concept left to mismatch on; "is the
right model loaded" is now purely a deployment-time concern the supervisor's config addresses,
not a runtime API check. 9 new tests (`test_mlx_server_supervisor.py`) + 1
(`test_mlx_provider_chat_omits_model_field`); 925/925 passing.

**S5 — Flip the default + docs (gated on S1-S4 clean).** `PIPELINE_LOCAL_PROVIDER` default →
`mlx` in `backend.py`; populate model-tier env vars with S1/S3's validated model; update
README/CLAUDE.md; keep `PIPELINE_LOCAL_PROVIDER=ollama` (or `PIPELINE_BACKEND_DISPATCH=ollama`)
a documented one-line opt-out. Tests: default-unset resolution now returns `MLXProvider`;
explicit `ollama` override is an unchanged regression gate.

## Back-compat & risk
- Ollama stays fully supported and explicitly selectable throughout — never remove it, only
  change which provider wins when nothing is set.
- Do not flip the default (S5) until S3 has produced at least one clean GT-pass on the same
  task class `devstral:24b` already clears — the point of this plan is to raise MLX to
  Ollama's proven bar, not to ship a regression under a more attractive label.
- S1/S3 involve downloading multi-GB model weights and running a real `mlx_lm.server` process
  for extended benchmark trials (800-1500s each per prior sessions' timing) — confirm before
  kicking off downloads/long runs, since that's a heavier, longer-running action than a code
  edit.

## Follow-up (separate plans)
- Qwen3-family non-thinking Instruct variants confirmed format-compatible
  (`Qwen2.5-Coder-32B-Instruct`, `Qwen3-30B-A3B-Instruct-2507`) as a second/third data point if
  Devstral MLX needs company for S3's comparison — both verified against real chat templates
  this session (standard JSON tool-call format, no thinking tokens).
- True SSE streaming for MLX/LM Studio (deferred per the original abstraction plan) — would
  close part of G4/G5 more thoroughly but is materially higher risk; revisit only if S2/S4
  don't sufficiently reduce timeout exposure.
- Patching `mlx-lm`'s tokenizer registration for `transformers>=5` (G3) — only worth it if a
  Qwen3.6-class model is ever specifically wanted.
- G1b's per-request thinking suppression for the OpenAI-compatible path — only needed if a
  hybrid-thinking model is deliberately chosen later.
