# Plan: Make MLX the default local inference provider

> **CURRENT STATE (2026-07-14, latest):** MLX dispatch *quality* is proven (fast, correct,
> oracle-passing) but the 30B model repeatedly kernel-panics this 24GB host — **7 MLX-attributed
> panics**, the latest (14:52:56, `@IOGPUMemory.cpp:550`) surviving every application-layer
> mitigation on a single serialized generation. **Decision (with user): stop hardening the 30B /
> don't fork mlx-lm; right-size to `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit` (~8.3GB, ~10GB
> headroom under the ~19GB GPU wired ceiling) and re-validate on the same task set.** Full
> analysis + benchmark research under **Phase 3 decision — REVISED** below. The S1 header that
> follows documents the earlier 30B validation and remains accurate for that model.
>
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
> **7 trials total on this model, ~48.3 min cumulative wall time:** `token_bucket` t0 (190.8s,
> parked, real reviewer finding), t1 (179.5s, `done`/`merged`/`APPROVE`), t2 (207.7s, parked,
> **same bug as t0** - rejected calls don't advance the clock, found independently by the
> reviewer on a separate implementation - a reproducible-but-not-deterministic model blind spot,
> ~50% hit rate on this specific edge case), t3 (116.4s, `done`/`merged`/`APPROVE`);
> `ratelimiter_inspect` t0 (204.3s) and t1 (159.7s), both `done`/`merged`/`APPROVE` - 2/2 clean.
> `lru_cache` t0: **first non-clean result** - `interrupted`/`timed_out` at 1838.3s/42 ticks, a
> read-heavy loop (5x identical `view_file` calls on the same test file, never progressing to an
> edit), the same failure mode already characterized for other local models on this host (see
> `[[project_dispatch_failure_modes]]` Mode 1). **This is also the first live confirmation that
> the S2 harness-reaping fix works correctly**: no orphaned subprocess was left behind (verified
> via `ps`) - the harness's own 1800s deadline triggered `interrupt_story`, checkpointed cleanly,
> and marked the story `interrupted` rather than leaving a zombie process like the pre-S2 MLX
> incidents. Tally across all 7: 6/6 completed-and-reviewed trials GT-correct (4 merged, 2
> correctly blocked on the same real bug), 1/7 timed out on task-specific read-looping unrelated
> to the MLX wiring itself. The 6 completed trials averaged 176.4s each. `lru_cache` exposed a
> genuine model-capability limit at 30B on a harder task, not an MLX-specific problem. Strongest
> local-dispatch result of any model tried on this host to date on the tasks it does handle
> (higher and more consistent than devstral:24b/gpt-oss:20b/qwen3-coder:30b's historical Ollama
> numbers - see `[[project_provider_dispatch_s3]]` for those baselines; a controlled Ollama-side
> re-run for a true head-to-head is deferred, not done this session).
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

**G5 confirmed the hard way (2026-07-14): 8 back-to-back S3 trials against one long-lived
`mlx_lm.server` process crashed the host with a real kernel panic**, not an app-level bug —
`panic-full-2026-07-13-230934.0002.panic`: `"pending memory object unexpectedly found in non
pending hash" @IOGPUGroupMemory.cpp:528`, a macOS/Metal GPU memory-object bookkeeping fault.
The panic's own process/memory snapshot showed ~21.3GB wired of 24GB total RAM with none of it
attributable to any single process's RSS (consistent with GPU-backed KV-cache buffers held via
IOSurface/IOKit, not counted against the server's own footprint) — i.e. sustained GPU-memory
churn across many trials without ever restarting the server, not any one trial's payload, is
the plausible trigger. The 8th trial's dispatch subprocess (`retry_backoff__mlx__t0`) is
captured mid-launch in the panic snapshot (`TH_WAIT`, ~230ms CPU used) — it didn't fail on its
own, the whole machine went down under it, which is also why its `agent.log` was 0 bytes
(indistinguishable from a genuine failed launch per `local_agent_oracle.py`'s documented
convention, until cross-referenced against `/Library/Logs/DiagnosticReports/`). Re-running the
same trial against a freshly-started server succeeded cleanly (171.0s, GT-pass, 1/1) —
confirming the trial itself was never the problem. Separately, the server for that whole S3
session had been started from `/Users/jessecarroll/git/loveline_search/.venv` — an unrelated
project's venv, built by a different session and not documented anywhere in this repo — which
cost real time to rediscover afterward. Both fixed in the same pass: (1) a dedicated,
version-pinned `.venv-mlx` in this repo (`uv venv --python 3.14 .venv-mlx && uv pip install
--python .venv-mlx/bin/python3 mlx-lm==0.31.3`) replaces the borrowed venv, wired into
`launchd/com.claude.pipeline.mlx-supervisor.plist`; (2) `scripts/mlx_server_supervisor.py` now
requires `MLX_SERVER_PYTHON` explicitly (previously defaulted to a bare `"python3"`, which
silently resolves to whichever interpreter is first on `PATH` — not guaranteed to have mlx-lm —
and `start_server`'s old `stdout=DEVNULL, stderr=DEVNULL` made that failure mode undiagnosable;
it now logs to `MLX_SERVER_LOG_PATH`, default `<repo root>/mlx-server.log`).

**Recurred (2026-07-14 09:35):** a third panic with the identical signature
(`panic-full-2026-07-14-093510.0002.panic`) hit while `mlx_lm.server` was idling after normal
request traffic (`mlx-server.log` shows routine `/v1/models` polling up to 09:34:09, nothing
unusual, then the panic 61s later) — this session's own reboot. The server that crashed was
running without any prompt-cache bound: the fix below was sitting uncommitted in the worktree
when the reboot hit, so it was never deployed to the process that panicked. Fixed now:
`scripts/mlx_server_supervisor.py` always passes `--prompt-cache-size`/`--prompt-cache-bytes`
(`MLX_PROMPT_CACHE_SIZE`/`MLX_PROMPT_CACHE_BYTES` env vars, default `2`/`4G`) so
`mlx_lm.server`'s own LRU self-evicts instead of accumulating GPU-backed KV-cache buffers past
physical memory — belt-and-braces over the "periodic restart" idea originally proposed here,
since bounding the cache at the source needs no restart-cadence judgment call at all. **Not yet
validated live:** this crash predates the fix's deployment, so it has not been observed to
actually prevent a repeat — worth watching the next several sessions' worth of trials before
treating G5 as closed.

**G5b — memory-floor livelock discovered validating the above (2026-07-14).** Re-running trials
against the cache-bound server (no crash, confirmed stable through 2 real cells) surfaced a
second, unrelated bug: `token_bucket` t0/t1 both finished `interrupted` at ~1806-1808s (the
matrix run's own per-cell timeout) instead of the historical ~180-210s. Traced via
`agent.log`/`mlx-server.log`/`.notifications.log`: the model actually finished real work fast
(t0 reached 9 steps with tests passing, matching baseline), then `advance_pipeline` started
logging `Dispatch backend gated (insufficient free memory (~1000-1900mb < 2048mb floor))` on
every tick - live free memory on this 24GB host settles in that range for as long as the 16GB
MLX model is resident and never once clears 2048mb. `backend.py`'s T13 memory floor (`resource_
status()`) and its Mode 18/T18 exception in `advance_pipeline` (never interrupt an in-progress
story on a memory-pressure gate, since that pressure is normally a transient cold-load spike
that clears on its own) both predate MLX: Ollama can evict a model under pressure so the
exception's "will clear" assumption holds, but mlx_lm.server pins one model's full footprint for
its entire process lifetime with nothing to evict - the assumption breaks, and dispatch is
gated *permanently*, not transiently, for the life of the server. Left as-is, this would silently
paralyze all future dispatch on any plan using MLX once a large-enough model is loaded - a direct
threat to the "autonomous, unattended overnight continuity" priority G5 already flagged.
**Fixed:** `resource_status()`'s floor is now per-provider
(`PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX`, falling back to the generic
`PIPELINE_LOCAL_MIN_FREE_MEMORY_MB` when unset) so an operator can give MLX a floor suited to its
own non-evictable memory model without loosening Ollama's. No default value is shipped for the
override - Ollama's 2048mb default is unchanged, and MLX keeps the same 2048mb floor until an
operator opts into a lower one, since the "safe" number depends on total host RAM vs. model size
and shouldn't be guessed generically. **Not yet fixed:** whether the actual stall in this
specific cell was *caused* by the outer gate (which only controls whether advance_pipeline
interrupts/redispatches, not whether the already-running dispatch subprocess itself makes
progress) or a separate hang inside `local_agent.py`'s own step loop is still unconfirmed - the
subprocess's pid stayed alive with no further mlx-server requests or transcript entries for the
whole stall window, which points at a hang independent of this gate. Re-running with the new
override set is the next step to see whether it resolves the stall in practice, not just the
theoretical permanent-gate risk.

**G5c — resolved: the actual cause of the G5b stalls was a wedged server, not the memory floor
(2026-07-14).** Re-running with the `PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX` override set did
*not* resolve the stall - `ratelimiter_inspect` t0 hit the same ~1807s timeout with zero
`agent.log` output and zero new `mlx-server.log` activity for the entire run, `dispatch_attempts:
0`. Root cause, confirmed directly: `curl -m 30 .../v1/chat/completions` hung the full 30s and
timed out (`exit 28`), while `GET /v1/models` kept answering instantly throughout. The server had
been left **wedged** - reachable, alive, passing every naive health check, but permanently unable
to complete a real generation - after an earlier matrix run's client process was killed
mid-request (`TaskStop` on the harness while a cell was in flight). `mlx_lm.server` is a
`ThreadingHTTPServer` with no request-level lock visible in its own source, so this isn't simple
serialization; a client disconnecting mid-generation most likely leaves some shared MLX/Metal
generation state (KV cache, command queue) in a state that blocks all subsequent generation
threads without touching the trivial GET handler. Nothing in this codebase could previously
detect or recover from this - `is_reachable()` (`GET /v1/models`) is exactly the check that stays
green through the whole failure. **Fixed:** `scripts/mlx_server_supervisor.py` adds `is_serving()`,
a real 1-token `/v1/chat/completions` probe (`MLX_HEALTHCHECK_TIMEOUT_SECONDS`, default 30s) run
whenever `is_reachable()` is already true; `ensure_running()` now kills whatever's listening on
`MLX_SERVER_PORT` (`_kill_listening_process`, via `lsof`) and starts a fresh server when reachable
but not serving, returning `"restarted_wedged"` (vs. `"already_running"`/`"started"`) so this is
distinguishable in logs. Live-validated: a fresh server answers the real probe in ~9s (first-
request overhead) and a second supervisor run against it correctly reports `"already_running"`
with no false-positive restart. 7 new tests (`test_mlx_server_supervisor.py`), 938/938 suite
green. **Not yet validated:** the wedge-recovery path itself (kill + restart on a genuinely wedged
server) has only been unit-tested with mocks, not exercised against a real wedged process live -
deliberately reproducing the exact disconnect-mid-generation condition on demand is unreliable;
treat this as proven-by-construction (the fix targets the exact confirmed symptom) rather than
live-validated until it's observed catching a real recurrence.

**Correction to G5c's trigger attribution:** closer review of the timeline shows `token_bucket`
t1 (the *first* run's own second cell, finished before any manual intervention) already showed
the identical "alive pid, zero server activity" symptom - the wedge predates the `TaskStop` kill
originally blamed above. It's now suspected the operator also ran two `matrix.py` processes
concurrently against the same `--workdir` and cell set for a period (a second `--resume` run was
launched without first stopping the first), which could independently corrupt a cell's worktree/
plan state - a distinct confound from the server-level wedge, not yet separated out. The
`is_serving()` fix stands regardless (directly curl-confirmed a real, server-level wedge
independent of either explanation), but the precise trigger is unconfirmed - treat "a client
timing out or disconnecting mid-generation can wedge the server" as the working hypothesis, not
a proven mechanism, and avoid running concurrent harness/matrix processes against the same
workdir as a separate operational hazard either way.

**G5d — a second, distinct kernel panic surfaced 7 minutes after the G5c/cache-bound fixes
shipped (2026-07-14, 12:18:23).** Panic string: `"IOGPUGroupMemory::remove_memory_object()
memory object not found" @IOGPUGroupMemory.cpp:323` - different from the `@528` panic
`--prompt-cache-size`/`--prompt-cache-bytes` targeted, and different from any prior incident here.
Timeline: `mlx_cachebound_20260714/ratelimiter_inspect__mlx__t0` was mid-dispatch (2 of N steps
done, no `result.json`); `mlx-server.log`'s last logged activity was a `GET /v1/models` at
12:17:33, panic at 12:18:23 - no completions call logged in between, consistent with one in
flight and never reaching its completion log line. The supervisor's launchd job was not loaded
at the time (`launchctl list` showed nothing), so `is_serving()`'s periodic probe was not a
factor in this specific incident.

Two candidate mechanisms, both externally corroborated, not mutually exclusive:

1. **Eviction-triggered spike.** `LRUPromptCache.insert_cache()` (mlx_lm/models/cache.py)
   allocates the new cache entry *before* evicting the oldest one once the LRU is over
   `--prompt-cache-size`. With the cap at 2 (G5c's value), a transient 3rd cache could exist for
   one GPU-touching moment - right at the wired-memory ceiling (`mlx_lm.server` unconditionally
   calls `mx.set_wired_limit(mx.device_info()["max_recommended_working_set_size"])`, measured at
   19.07GB on this 25.77GB host; the ~16-21GB model resident set leaves almost no headroom). The
   log shows a first eviction succeeding cleanly (12:16:03→12:16:17); the panic lines up with
   where a second one would be needed.
2. **Concurrent GPU graph evaluation inside the server.** `mlx_lm.server` runs on a
   `ThreadingHTTPServer` with no lock anywhere in its source, and separately defaults
   `--prompt-concurrency 8` ("process that many prompts in parallel" via its internal batch
   generator) - parallel graph evaluation MLX's own tracker documents as unsafe
   ([ml-explore/mlx#2133](https://github.com/ml-explore/mlx/issues/2133)). mlx-lm's tracker has
   already tied server-side concurrency to real bugs on this exact version family: KV-cache
   cross-contamination between concurrent requests
   ([#965](https://github.com/ml-explore/mlx-lm/issues/965)) and a batch-merge crash
   ([#754](https://github.com/ml-explore/mlx-lm/issues/754)). No second GPU-touching request was
   confirmed in flight for *this* panic specifically (the concurrent request would have to be
   `--prompt-concurrency`'s own internal batching, not an external client - we dispatch serially),
   so this is a plausible contributor, not a confirmed one for this incident.

**Stopgap shipped (2026-07-14), Phase 1a+1b of the response plan:**
- `MLX_PROMPT_CACHE_SIZE` default lowered `2`→`1` (removes the transient N+1-cache eviction spike
  entirely, at the cost of one conversation's cache never surviving a second concurrent one).
- `MLX_PROMPT_CONCURRENCY` (new, default `1`) passed as `--prompt-concurrency`, overriding
  mlx_lm.server's own default of `8` - serializes internal batch GPU graph evaluation.
- New `scripts/mlx_server_wrapper.py`, launched by the supervisor in place of `-m mlx_lm server`:
  sets a soft `mx.set_memory_limit()` ceiling (`MLX_MEMORY_LIMIT_MB`, default `22528` = 22GiB,
  derived from this host's 25.77GB minus ~3GB OS headroom - not portable to other hosts as-is) so
  crossing it fails the allocation in-process (a crash the supervisor can restart) rather than
  reaching the kernel panic path, plus per-request start/end + peak-memory instrumentation
  (`MLX_WRAPPER_LOG_PATH`) to observe, from real logs, whether memory pressure and/or concurrency
  is the actual trigger on the next occurrence.
- 12 new tests (`test_mlx_server_wrapper.py` ×6, `test_mlx_server_supervisor.py` +2 new/updated),
  939/939 suite green.

**Not yet validated live**: none of the above has been exercised against a real dispatch run yet
- this is a mitigation grounded in code inspection + upstream issue research, not a proven fix.

**Phase 2 result: FAILED (2026-07-14, 14:29:40).** A 6th panic hit, same `@528` signature as the
original incident, **~4.5 minutes** after the server was restarted with Phase 1a/1b live
(`mlx-server-wrapper.log` confirms `memory limit set bytes=23622320128` applied at 14:25:20)
while a real benchmark trial (`mlx_stopgap_validation_20260714/ratelimiter_inspect__mlx__t0`) was
in flight. Two things this run adds beyond the existing hypotheses:
- The soft `mx.set_memory_limit()` ceiling never tripped or logged before the kernel panic -
  `mlx-server-wrapper.log`'s last line (14:28:29) shows peak resident climbing steadily (17.1GB
  -> 18.9GB over ~3 minutes) but still well under the 22GiB cap when the panic hit 70s later.
  Memory *volume* alone is looking less likely as the sole trigger; this is more consistent with
  the eviction-ordering race (mechanism 1) than sheer pressure.
- Direct inspection of `mlx_server_wrapper.py` confirms it adds **no actual lock** around
  generation - serialization relied entirely on `--prompt-concurrency 1`
  (`mlx_lm.server`'s *internal batch* concurrency knob), not a mutex on the
  `ThreadingHTTPServer`'s per-connection threads. The "no lock anywhere in its source" gap G5d
  already flagged was still open at the time of this panic.

**Phase 3 decision (2026-07-14, made with the user):** one more targeted fix, then a short
*attended* (not overnight/unattended) re-validation - not a full stop to hardening, but not
proceeding to unattended use either. Two fixes shipped in this pass, both in this repo's own
code (not a hand-edit inside `.venv-mlx/site-packages`, so they're git-tracked, reviewable, and
survive a venv rebuild):
1. **`serialize_generation()`** (`scripts/mlx_server_wrapper.py`) - wraps `do_POST` in a real
   `threading.Lock()`, independent of `--prompt-concurrency`, so at most one generation request
   executes at a time regardless of what the server's own internal batching does. `do_GET` (e.g.
   `/v1/models` health checks) is left unlocked since it never touches the GPU.
2. **`patch_lru_prompt_cache_evict_before_insert()`** (`scripts/mlx_server_wrapper.py`) -
   monkeypatches `LRUPromptCache.insert_cache` (Phase 4 item 1, pulled forward) so the LRU-oldest
   entry is evicted *before* a new key is added, not after. Confirmed by reading
   `mlx_lm/models/cache.py` directly: the shipped ordering adds the new entry to the trie first
   and only then evicts - so *any* configured `max_size` (not just the old cap of 2) transiently
   holds one extra entry's GPU-backed cache alive at the moment a new key arrives. Lowering the
   cap 2->1 earlier today shrank this window's absolute size but could not close it - a cap of 1
   still transiently holds 2, right at the wired-memory ceiling. The patch only reorders *when*
   eviction happens; it delegates to the original method for everything else (existing-key
   updates, prefix trimming, the trailing max_bytes safety net), so the library's own
   trie/byte-accounting is reused unchanged. Both new functions wired into `main()`. Verified
   directly against the real `.venv-mlx` `mlx_lm.models.cache.LRUPromptCache` (not just a fake) -
   3 distinct inserts at `max_size=1` leave `len(_lru) == 1`, confirmed live. 6 new tests in
   `test_mlx_server_wrapper.py`, 939/939 suite green.

**Not yet validated live**: same caveat as Phase 1 - these are two more targeted, individually
tested mitigations, not proof the panics stop. The next step is the short attended re-run in
Phase 2 (below), watched in person, not left running unattended overnight.

**Phase 2 revalidation result: FAILED again — 7th MLX-attributed panic (2026-07-14, 14:52:56).**
The short attended re-run panicked the host ~3.5 minutes after the Phase 3 fixes went live and the
server restarted (`mlx-server-wrapper.log`: `memory limit set` at 14:49:07, first real generation
at 14:50:05, panic at 14:52:56). This is the decisive data point, and it argues against continuing
down the application-layer path:
- **Third distinct panic signature**: `"completeMemory() prepare count underflow"
  @IOGPUMemory.cpp:550` (`panic-full-2026-07-14-145256.0002.panic`) — different from both the
  original `@IOGPUGroupMemory.cpp:528` and the G5d `@IOGPUGroupMemory.cpp:323`. Three different
  fault sites in Apple's `IOGPU.kext` in one day is the signature of a driver-level fault being
  *provoked* from different code paths, not one app-level bug with one fix.
- **Every mitigation was simultaneously live and none prevented it**: the real `threading.Lock`
  around `do_POST` (Phase 3), evict-before-insert (Phase 3, wrapper mtime 14:42 predates the
  14:49 server start), `MLX_PROMPT_CACHE_SIZE=1`, `--prompt-concurrency 1`, and the 22GiB soft
  `mx.set_memory_limit()` ceiling.
- **Only ONE generation was in flight** at the panic (wrapper log: `do_POST` Thread-21 started
  14:51:48, no `end` line before the crash) — so with the new lock actually serializing every
  `do_POST`, concurrent GPU graph evaluation (mechanism 2 / `--prompt-concurrency`) is ruled out
  as the trigger for *this* incident. It was a single, serialized generation.
- **Peak logged memory ~18.8GB** — under the 22GiB soft cap and under `mlx_lm.server`'s own
  ~19.07GB wired limit, so sheer allocation *volume* didn't trip anything catchable either. But
  ~18.8GB resident sits right against the ~19GB GPU working-set ceiling with almost no headroom —
  which is the through-line across every one of these panics.

**Read: the application-layer hardening is not converging.** Lock, cache-eviction reordering,
concurrency serialization, and a soft memory ceiling were all in place, on a single serialized
generation, and the host still panicked with a novel driver fault site. This is consistent with
the plan's own "scope boundary" caveat (the `IOGPUGroupMemory`/`IOGPUMemory` fault lives in
Apple's GPU driver, unreachable from a Python fork). The one factor that has been constant across
all 5+ MLX panics is a ~17-19GB-resident model pinned against this 24GB host's ~19GB wired ceiling
with near-zero headroom.

**Phase 3 decision — REVISED (2026-07-14, with the user): right-size the model, do not fork
mlx-lm.** The user's priority is to keep MLX (early results are genuinely strong — fast, correct,
oracle-passing; the *runtime* is not the problem). The panics track the 30B model's memory
footprint against the GPU ceiling, not the MLX runtime itself, and those two are separable. Two
observations make forking (Phase 4) the wrong next move:
1. Phase 4 item 1 (evict-before-insert) is **already shipped** in the wrapper and did not prevent
   the 7th panic. Phase 4 item 3 (catch Metal OOM → HTTP 503) cannot help — a *kernel panic* in
   the driver's memory-object bookkeeping is not a catchable userspace allocation failure.
2. Phase 4 item 2 (configurable wired-memory ceiling) and "use a smaller model" are the **same
   lever wearing two hats** — both just create headroom below the GPU working-set ceiling. A
   smaller model delivers that headroom today with zero fork to maintain; if headroom is *not*
   the fix, a fork of item 2 wouldn't save us either. Forking is dominated by the smaller-model
   experiment.

**Replacement model — researched against proven benchmarks (2026-07-14).** Requirements: (a)
tool-call wire-format compatible with `mlx_lm.server`'s native `<tool_call>`-JSON parser (G1, the
#1 gate), (b) non-thinking (G1b — no per-request thinking toggle on the OpenAI-compat path), (c)
coding-capable, (d) footprint well below the ~19GB ceiling. Recommended:
**`mlx-community/Qwen2.5-Coder-14B-Instruct-4bit`** (~8.3GB weights; `lmstudio-community`'s
MLX-4bit build measures 8.33GB) — leaving ~10GB of headroom under the wired ceiling vs the 30B's
~0-1GB.
- **Lowest wire-format risk of any candidate**: same Qwen Hermes-style `<tool_call>{"name":...,
  "arguments":...}</tool_call>` template family as the *proven-working* Qwen3-30B-A3B-Instruct-2507
  and the already format-verified `Qwen2.5-Coder-32B-Instruct` (see Follow-up). Non-thinking.
  (Still verify the actual chat-template file in `.venv-mlx`'s tokenizer before the run, per
  verify-before-assuming — but the family-level evidence is strong.)
- **Benchmarks (Qwen2.5-Coder Technical Report, arXiv:2409.12186v3)** — 14B-Instruct vs the 32B it
  under-cuts and the 7B fallback:

  | model (4bit MLX)        | ~size  | HumanEval | HumanEval+ | MBPP | LiveCodeBench | BigCodeBench(Full) | Aider Pass@2 |
  |-------------------------|--------|-----------|------------|------|---------------|--------------------|--------------|
  | Coder-7B-Instruct       | ~4.3GB | 88.4      | 84.1       | 83.5 | 18.2          | 41.0               | 68.4         |
  | **Coder-14B-Instruct**  | ~8.3GB | **89.6**  | **87.2**   | 86.2 | 23.4          | 48.4               | 69.2         |
  | Coder-32B-Instruct      | ~18GB  | 92.7      | 87.2       | 90.2 | 31.4          | 49.6               | 73.7         |

  The 14B lands only ~3 HumanEval points below the 32B and *ties* it on HumanEval+, while less
  than half the footprint. The 32B rules itself out for the stability goal — its ~18GB footprint
  reproduces the exact no-headroom condition that tracks the panics. The benchmark dispatch cells
  (`token_bucket`, `ratelimiter_inspect`) are HumanEval-class tasks the 30B already cleared in
  ~2 steps, comfortably within a 89.6-HumanEval coder's range.
- **Fallback ladder if the 14B still panics**: drop to `Qwen2.5-Coder-7B-Instruct-4bit` (~4.3GB,
  ~14GB headroom). If *that* still panics, the panic is proven independent of memory headroom →
  it's a genuine `IOGPU.kext` driver bug on this host, and the honest answer is hardware (a Mac
  with more unified memory for 30B-class MLX unattended), not more code. Either outcome is a
  decisive result rather than continued patch-and-drift.

**Confidence (my estimate, not a measurement):** a right-sized 14B MLX model gives a stable
overnight driver on this host at **~45-60%** (headroom is the single best-supported lever across
every panic); continuing to harden the 30B ~15%; forking mlx-lm ~20% (its one useful lever
overlaps the smaller-model test, its other two are shipped-and-failed or uncatchable). The
smaller-model run is the cheap, high-information experiment that either unblocks S5 today or
converts "keep patching" into a clean hardware decision.

**Right-size step 1 — DONE (2026-07-14).** Downloaded `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit`
(sequential-`curl` method, 8.31GB / 2 shards, to `/Users/jessecarroll/.cache/qwen2.5_coder_14b_manual`;
integrity-checked against the safetensors index). **G1 gate verified by direct template inspection,
PASS**: the chat template in `tokenizer_config.json` renders tool calls as `<tool_call>\n{"name":
..., "arguments": ...}\n</tool_call>` — the exact `json.loads`-able Hermes/Qwen shape
`mlx_lm.server`'s native parser handles, identical to the proven 30B and the verified Coder-32B;
no `<think>` block (confirmed non-thinking, no G1b problem). Started via the supervisor
(`MLX_SERVER_MODEL_PATH` repointed, plist updated); a real 1-token `/v1/chat/completions` probe
returned cleanly in ~2s. **Footprint confirms the headroom thesis**: `mlx-server-wrapper.log` peak
GPU allocation after a real generation was **8.42GB** (`peak_bytes=8424058837`) — ~10.6GB below the
~19GB wired ceiling, vs the ~0-1GB headroom that tracked every panic. Next: the attended validation
run (`token_bucket` + `ratelimiter_inspect`), watching for any panic across the window.

**Right-size step 2 — attended validation run (2026-07-14, `_runs/mlx_14b_validation_20260714`).**
4 cells (`ratelimiter_inspect` + `token_bucket`, 2 trials each), `--jobs 1`, against the running
14B server. Two clearly separable outcomes:

- **STABILITY: PASS — the panics stopped.** No host panic across the full 1680.9s (28-min) run
  (panic-report count held at 7, latest still `145256` from *before* the run). `mlx-server-wrapper.log`
  peak GPU allocation stayed **9.0-9.7GB the entire run** — never near the ~19GB wired ceiling or the
  22GiB soft cap, across all 4 cells and their cache evictions. This is the decisive result the
  right-size decision was built to get: the 14B's ~8GB footprint gives the driver enough headroom
  that the `IOGPU` fault does not trigger. On the memory/stability axis, MLX-on-this-host is viable.

- **DISPATCH CORRECTNESS: 0/4 GT-pass — but the cause is a specific, fixable tool-call malformation,
  NOT a coding-capability ceiling.** Diagnosed to root by reading the transcripts + reproducing
  against `recover_tool_calls()`: the 14B emits multi-line code arguments (`new_str`/`old_str` in
  `str_replace` edits) using **Python `"""triple-quoted"""` string syntax with literal newlines**,
  which is **invalid JSON**. `json.loads` rejects it (`Expecting ',' delimiter` at the `"""`), the
  harness logs "no tool call", and the edit is silently dropped. **12 of 12** dropped tool calls
  across all 4 cells carry this exact signature. Consequences: `token_bucket` t0/t1 never wrote
  `rate_limiter.py` at all (impl edits all dropped → tests fail on a missing file); `ratelimiter_inspect`
  t0/t1 wrote the impl but lost at least one edit each → incomplete code → both the reviewer
  (`REQUEST_CHANGES`) and the independent oracle (`groundtruth_passed: false`) rejected it. Simple
  single-line tool calls (`bash`, `create_file`) have valid JSON and executed fine throughout — the
  break is specific to multi-line code payloads.

  **This is distinct from G1's tag-format gate** (the model DOES use the right `<tool_call>`/fenced
  structure — the template is correct, confirmed step 1) and distinct from a raw-capability failure
  (the code it writes looks correct). It's a JSON-string-escaping discipline weakness — a known trait
  of smaller models that the proven 30B did not exhibit. So step 2 does **not** give a clean
  capability read on the 14B: a harness-recoverable malformation corrupted every cell before the
  model's actual coding could be graded.

**Harness fix — DONE (2026-07-14, direct edit per user request, not via the pipeline).**
`scripts/local_agent.py::recover_tool_calls()` now runs a tolerant loader (`_loads_tolerant` →
`_repair_triple_quoted_strings`): when a candidate fails a raw `json.loads`, it rewrites Python
triple-quoted string literals (`"""..."""` / `'''...'''`) as JSON-encoded strings (`json.dumps` of
the inner text) and retries. Valid JSON is never transformed (repair only runs after a raw-parse
failure), and it fails closed on prose (no phantom tool calls). 2 new tests in `test_local_agent.py`
(the real triple-quote case + a prose negative); `test_local_agent.py` 86/86 and
`test_local_agent_oracle.py` 76/76 green. **Verified end-to-end against the run's real transcripts:
all 12/12 previously-dropped `str_replace` calls now recover.** Helps any local model with this
malformation, not just the 14B. **Next: re-run the step-2 validation** — only now can the 14B's true
dispatch capability be graded, since its edits will actually land. If it still can't clear the tasks
with edits landing, that's a capability verdict; if it does, we have a stable MLX daily driver. The
stability win stands regardless: this host no longer panics at the ~8-9GB footprint.

**Right-size step 2, run 3 — tool-call fix live, but result CONFOUNDED by a broken host `pytest`
(2026-07-14, `_runs/mlx_14b_validation_20260714_run3`).** The triple-quote fix works: **0 dropped
tool calls** all run (vs 12/17 before), impl files now land (`token_bucket` wrote `rate_limiter.py`
for the first time), still no panic (peak 14.25GB under longer/heavier load — another stability data
point). But all 4 cells `interrupted` at timeout, 0/4 GT-pass, with wildly varying ticks (88/5/19/13)
and visible thrashing loops (`pytest` → `which python3` → `str_replace`, repeated). **Root cause of
the thrashing, found by reading the tool outputs (NOT a clean capability verdict):** the host's bare
`pytest` shim (`/opt/homebrew/bin/pytest`) has a stale shebang pointing at
`/opt/homebrew/opt/python@3.13/bin/python3.13`, a Homebrew Python that no longer exists (host now has
3.10/3.11/3.14, no 3.13) — so every `pytest ...` the model runs returns `bad interpreter: No such
file or directory`. The agent's shell PATH resolves bare `pytest` to this broken global shim rather
than the worktree's *working* `.venv/bin/pytest` (valid shebang, symlinked into every worktree). The
model gets a cryptic error instead of real test feedback, can't iterate, and thrashes (token_bucket
t0 literally left `pass`-body stubs because it never saw a working test tell it the impl was empty).
**The oracle grading is unaffected and trustworthy** — `detect_test_command` returns
`[.venv/bin/python, -m, pytest]` (the working venv path), verified live (correctly fails the stub
code 9/9); so `gt=False` is real, but the model never had a fair shot to reach passing code. **This
is a host-environment confound, not a measured 14B capability limit — a clean re-run requires the
agent to use a working pytest first.** Fix options: (a) repair the global `pytest` shim's shebang
(host mutation, unblocks bare `pytest` everywhere) or (b) put the worktree `.venv/bin` first on the
dispatched agent's PATH so its `pytest` matches the venv the oracle already grades in (harness code
change, more correct/isolated — agent and oracle then share one interpreter). Recommend (b).

**Right-size step 2, run 4 — clean read blocked by a self-inflicted dispatch stall
(`_runs/mlx_14b_validation_20260714_run4`).** With pytest fixed, **GT-pass jumped to 2/4** —
`token_bucket` both cells produced GT-correct code (t1 reached "ORACLE GREEN — committed & done"),
first real capability from the 14B. But all cells still `interrupted` at timeout because of ~950s
dead-time stalls. Root-caused precisely: the Phase 3 stopgap `mlx_server_wrapper.py` instrumentation
calls `mx.get_peak_memory()` in its per-request `finally` block, and that call blocks for ~950s while
`serialize_generation()`'s lock is held across it — wedging every following request until the harness
kills the cell (confirmed: 3 mega-stalls of 974/966/930s ↔ the 3 interrupted cells; the one cell that
finished had no stall). The lock never prevented panics in the first place — the right-sized footprint
did — so it was pure downside.

**Wrapper fix — DONE (2026-07-14, direct edit).** Removed the `instrument_handler()` and
`serialize_generation()` calls from `mlx_server_wrapper.py::main()` (functions kept defined +
unit-tested; 14/14 green), leaving only the cheap startup-only `mx.set_memory_limit()` and the
cache-evict patch in the live path.

**Right-size step 2, run 5 — the payoff (`_runs/mlx_14b_validation_20260714_run5`).** Stalls
**completely gone**: whole run 751.7s (was 4850s), per-cell 143-291s (matching the 30B's historical
speed), **0/4 interrupted** (was 3/4), 0 panics. The dispatch pipeline is now sound; the run also
surfaced a benchmark-oracle gap (below) that initially made the result look better than it is.

**Correction after inspecting the merged code (2026-07-14):** the headline "token_bucket 2/2
GT-correct" was WRONG. Both token_bucket cells' code carries the same double-refill / rate-limit-
bypass bug (`available_tokens` mutated on every call but `last_checked` advanced only on success);
verified by reproducing Claude's exact repro against both merged implementations. They passed only
because **neither the acceptance oracle nor `groundtruth.py` tested a rejected call at a FORWARD
`now`** followed by a later accept — both only held `now` constant or moved it backward on the
failing call. So `gt=True` was an oracle false-positive on this bug class, not correct code.
- `token_bucket`: **0/2 truly-correct** (2/2 oracle-pass). The review gate (Claude Sonnet, not the
  14B - the reviewer runs `backend=claude, model=sonnet`) caught the bug on **t0** (REQUEST_CHANGES,
  an *exemplary* review that reproduced it and named the exact oracle gap) but **missed the
  identical bug on t1** (APPROVE -> merged). So t1's "success" is really a hidden merged-but-wrong.
- `ratelimiter_inspect`: **0/2** - t1 APPROVE+`gt=False`, t0 parked. Genuinely beyond the 14B.

**Oracle gap FIXED (2026-07-14, direct edit).** Added
`test_rejected_forward_call_does_not_double_count_refill` to **both**
`tasks/token_bucket/acceptance.py` and `groundtruth.py` (a rejected forward-`now` call must still
advance the clock). Verified it FAILS the buggy 14B code and PASSES a correct impl; full suites green
(acceptance 10, groundtruth 15). Now buggy token_bucket code can no longer reach `gt=True`.

**Net verdict (2026-07-14):** on this 24GB host, `Qwen2.5-Coder-14B-Instruct-4bit` via MLX is now
**stable (0 panics across runs 1-5) and fast (~150-290s/cell)** - the dispatch/serving side is solid.
But once the oracle gap is closed, its **coding capability on these tasks is weaker than it first
appeared**: 0/2 truly-correct on token_bucket (subtle refill bug both times - though this same bug
also trips the 30B ~50% of the time, so it's a hard edge case) and 0/2 on ratelimiter_inspect. Four
genuine issues were found and fixed getting here: the tool-call triple-quote drop
(`recover_tool_calls`), the broken host `pytest` shim, the wrapper's lock/instrumentation stall, and
the token_bucket oracle's forward-reject gap. The review gate itself is **not the 14B and not broadly
noisy** - Claude Sonnet produced a genuinely excellent review on t0; its one real defect here was
inconsistency (missing on t1 the exact bug it caught on t0).

**Capability ceiling map + escalation proof (2026-07-14, all fixes in place).** Ran the 14B across
4 tasks (`_runs/mlx_14b_ceiling_20260714`) and gpt-oss:20b (Ollama) across the 14B's 3 failures
(`_runs/gptoss_escalation_20260714`), same harness + Claude review, 0 panics throughout:

| task | 14B-MLX (gt) | gpt-oss:20b Ollama (gt) |
|------|-------------|-------------------------|
| lru_cache | **True** | (not run — 14B already passed) |
| token_bucket | False (double-refill, now caught) | **True** |
| ratelimiter_inspect | False | **True** |
| retry_backoff | False* | False* |

14B: **1/4 truly correct**. gpt-oss: **2/3** on the exact tasks the 14B failed. (*retry_backoff:
both models reached `done` but gt=False — a likely third acceptance-vs-groundtruth oracle gap, like
token_bucket's, worth a separate fix.)

**Strategic conclusion (2026-07-14): the MLX effort succeeded technically but gpt-oss:20b on Ollama
is the better LOCAL driver on this host right now.** MLX-14B is stable (0 panics across 6 runs) and
fast (~150-290s/cell) but capability-limited (1/4). gpt-oss:20b runs fully local on this same 24GB
host with no panics (Ollama's evict-capable memory model sidesteps the IOGPU fault entirely) and is
materially more capable (handles the hard tasks the 14B can't). The two can't co-reside in 24GB, so
this is an either/or for the local driver, not a layered local pair. Practical options:
- **gpt-oss:20b (Ollama)** — best capability for a *fully local* driver; the recommended local
  daily-driver on this host if local capability is the priority. Slower than MLX, no panic.
- **14B-MLX** — best when speed matters and tasks are simple; pair with Claude escalation (not a
  second local model - memory) for anything harder, via the existing `PIPELINE_BACKEND_DISPATCH=auto`
  risk router.
- **glm-5.2:cloud (current live default)** — capable but *cloud*, so it doesn't advance the
  local-LLM goal; a Claude-usage-conscious stopgap.
The MLX-as-default-provider goal (this plan's original aim) is achievable and stable, but only worth
flipping to if its speed matters more than the capability gap vs gpt-oss-on-Ollama for a given
workload. Escalation to Claude for high-risk/failed stories is already wired (`auto` router).

**Original Phase 2/3/4 plan retained below for provenance** — Phase 4 (fork mlx-lm) is now gated
behind the smaller-model experiment failing, not the immediate next step.

**Phase 2 - validate.** Re-run an `mlx_cachebound_20260714`-style matrix (`ratelimiter_inspect` +
`token_bucket` cells) long enough to force at least 2-3 real cache-eviction cycles under the new
`MLX_PROMPT_CACHE_SIZE=1`/`MLX_PROMPT_CONCURRENCY=1` settings. Success is no host panic across
that window; read `mlx-server-wrapper.log` (start/end + peak-memory per request, thread name) to
see whether any two requests ever actually overlapped and how close peak memory got to
`MLX_MEMORY_LIMIT_MB`, so Phase 3's decision is made on data rather than the same guesswork this
incident started from.

**Phase 3 - decision gate (`request_decision`).** Feed Phase 2's data into an explicit,
recorded decision: continue hardening 30B-MLX-on-this-24GB-host, drop to a smaller/more-quantized
MLX model, or concede MLX-on-this-host and stay on Ollama for local dispatch. A panic every few
hours is a real operating cost that deserves a decision on record, not indefinite drift while we
patch around each new symptom.

**Phase 4 - fork mlx-lm (conditional on Phase 3, not abandoned).** If Phase 3 says keep going,
these are Python-only changes against the exact source already vendored in `.venv-mlx`
(mlx-lm==0.31.3), all plausibly upstreamable as PRs rather than a permanent divergent fork:
1. **Evict-before-insert in `LRUPromptCache.insert_cache()`** (`mlx_lm/models/cache.py`) - evict
   the oldest entry *before* inserting the new one, not after, eliminating the transient N+1-cache
   spike at its source instead of just shrinking the cap to 1 and giving up multi-conversation
   cache reuse entirely.
2. **Configurable wired-memory ceiling** - remove the hardcoded
   `mx.set_wired_limit(mx.device_info()["max_recommended_working_set_size"])` at `server.py:1889`
   in favor of a CLI flag/env var, so the wired limit itself (not just our own soft
   `mx.set_memory_limit()` ceiling on top of it) can be tuned per host.
3. **Catch Metal OOM and return HTTP 503** instead of letting the generation handler crash the
   process - the exact ask in upstream
   [ml-explore/mlx-lm#854](https://github.com/ml-explore/mlx-lm/issues/854) and
   [#1015](https://github.com/ml-explore/mlx-lm/issues/1015), both still open.

**Scope boundary, explicit:** the `IOGPUGroupMemory` fault itself lives in Apple's `IOGPU.kext`
GPU driver, not in mlx-lm - unreachable from a Python-level fork. Phase 4 can reduce how often the
kernel's bookkeeping gets pushed into the failure state and shrink the blast radius when it does;
it cannot eliminate the underlying kernel bug. Treat Phase 4 as risk *reduction*, not a fix, when
weighing it against simply not running a 30B model on a 24GB host in Phase 3.

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
