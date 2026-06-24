# Pluggable LLM Backend Plan — Local Devstral + Claude Overlord

**Goal:** Stop burning the Claude subscription on the high-volume agent roles
(dispatch + review) by moving them to a swappable backend, defaulting to a local
**Devstral 24B** running headless on the M4 Mac mini, while keeping **Claude as
the rare, cheap single-shot overlord**. The design must avoid model lock-in: any
role can be re-pointed at a local model, an external GPU, or a cloud
open-weights endpoint by changing config only.

**Hardware target (discovered):** Mac mini, Apple M4 (base), 24 GB unified
memory, 10-core GPU, ~120 GB/s bandwidth. Ollama installed. Speed is explicitly
*not* a priority — overnight autonomous continuity is.

---

## Status (as of implementation)

**Done and verified end-to-end (real model, real subprocess, not just mocks):**
- Steps 0–2 (`backend.py` seam, `ClaudeCliDriver`, per-role config routing
  `PIPELINE_BACKEND_DISPATCH/REVIEW/OVERLORD`) — pure refactor, 159 tests green.
- Step 3 (`OllamaDriver.complete()`) — real overlord decision round-tripped
  through devstral:24b and parsed correctly by `_parse_ruling`. Found and fixed
  a real bug along the way: Ollama's default 131072 context made devstral's KV
  cache exceed 24GB unified memory, forcing a CPU/GPU split that timed out a
  single completion at 600s. Fixed by using Ollama's native `/api/chat` (only
  it accepts `options.num_ctx`) pinned to 8192 → confirmed 100% GPU.
- Step 4 (`OllamaDriver.dispatch()` via OpenHands) — real file edit + git
  commit verified end-to-end through the actual `dispatch_story` MCP tool.
  Two more real bugs found and fixed:
  - devstral errors on OpenHands' default `reasoning_effort` ("high"); needs
    `reasoning_effort="none"` (baked into a persisted settings file by
    `scripts/setup_openhands_local.py`, since OpenHands' env-var override only
    covers model/base_url/api_key, not reasoning_effort).
  - The same num_ctx problem recurs independently in OpenHands' LiteLLM path;
    fixed via `litellm_extra_body={"options": {"num_ctx": ...}}` in that same
    settings file.
  - **Safety issue found and fixed:** the dispatch prompt always instructs the
    agent to call the `checkpoint` tool (for resumability). Initially wired by
    registering this pipeline's *full* MCP server with OpenHands so that tool
    would exist — but that handed the dispatched agent direct access to all
    ~19 orchestration tools (`approve_merge`, `advance_pipeline`,
    `dispatch_story`, ...), a real privilege-escalation surface for a less
    reliable local model. Fixed by extracting checkpoint logic into
    `_checkpoint_impl` and building `scripts/checkpoint_mcp_server.py`, a
    minimal dedicated MCP server exposing *only* `checkpoint` (verified: lists
    exactly one tool). `backend.py`'s `OllamaDriver._write_mcp_config` now
    points at that minimal server, not the full one.

**Superseded finding — the original "MCP attached breaks tool-calling"
hypothesis above was wrong; the real causes are deeper and MCP-independent.**
A follow-up isolation session ran fresh dispatches (no MCP server at all) and
got the *opposite* of the "2/2 success without MCP" result recorded above —
3 of 4 failed outright. Digging in found two distinct, real, MCP-independent
problems:

1. **Fixed.** Every released `openhands` CLI version with `--headless` mode
   (checked 1.4.0 through the current 1.16.0, by downloading and grepping
   PyPI releases) hardcodes
   `conversation.set_security_analyzer(LLMSecurityAnalyzer())` in
   `openhands_cli/setup.py`, unconditionally — independent of
   `--always-approve`/`--llm-approve`. That makes a `security_risk` argument
   mandatory on *every* tool call; devstral omits it inconsistently, which
   fails real dispatches with `Error validating tool 'file_editor': Failed to
   provide security_risk field`. Pinning an older CLI version doesn't help —
   1.0.7 is the only release without this, and it predates `--headless`
   entirely, so no released version has both. Fixed without forking
   `openhands_cli`: `scripts/run_openhands_headless.py` monkeypatches just the
   `LLMSecurityAnalyzer` symbol `setup.py` imported into its own module
   namespace to a no-op before calling the real CLI entrypoint, so
   `set_security_analyzer(LLMSecurityAnalyzer())` effectively becomes
   `set_security_analyzer(None)` — every other CLI behavior (settings/MCP/
   hooks loading, argv parsing) stays identical to upstream.
   `backend.py`'s `OllamaDriver.dispatch()` now runs through this wrapper
   (via the OpenHands tool's own interpreter) instead of the bare `openhands`
   binary.
2. **Root cause now isolated (a second session drilled all the way down) —
   it is the *tool-schema surface*, and OpenHands' *strict* native-only
   parsing, not the model's capability, num_ctx, prompt size, the litellm
   prefix, or temperature.** Method: captured OpenHands' real request
   (monkeypatched `litellm.completion`) and replayed it directly against
   Ollama's `/api/chat`, varying one factor at a time. Results:
   - **System-prompt size is a red herring.** Full 30KB OpenHands system
     prompt + **one** clean tool → devstral emits a perfect native
     `tool_calls` every time. 76-char system prompt + OpenHands' **7** tools
     → fails. So size of the prompt doesn't matter; the *tool set* does.
   - **It is not the litellm `ollama/` vs `ollama_chat/` prefix.** `ollama/`
     routes to litellm's `/api/generate` (flatten-to-text, prompt-based
     function calling); `ollama_chat/` routes to `/api/chat` with native
     `tools`. Switching to `ollama_chat/` was tested end-to-end and did
     **not** fix it (model still emitted text).
   - **It is not temperature.** `temperature=0` (greedy, deterministic) made
     the full-tool case fail *identically* on repeat runs, and even made a
     3-tool case that passed at default temp fail — so low temp is not the
     lever (and is arguably worse).
   - **What actually happens:** given OpenHands' 7 bloated tool schemas
     (each ~0.7–6.6KB, every one carrying injected `security_risk` +
     `summary` params; ~21KB / ~5k tokens of tool JSON total), devstral
     stops emitting the Mistral-native `[TOOL_CALLS]` token and instead
     narrates a plan in prose, or emits the tool call as a markdown ```json
     block / a bare `[{"name":...}]` text array. Ollama therefore returns
     **empty** `tool_calls`, and OpenHands' native path discards the text and
     stalls. Devstral is *Mistral's OpenHands-trained coding agent*, so this
     is specifically a format-adherence collapse under schema load, not a
     blanket incapability.

**This is fixable, and the durable fix was verified end-to-end.** A minimal
native agent loop (Ollama `/api/chat`, 2 clean tools `bash`+`done`, no
`security_risk`/`summary` injection, short system prompt, ~80 lines, no
litellm, no `openhands_cli`) completed the create-file-and-commit task
**3/3** across repeated runs. Crucially it confirmed one more thing: devstral
*still* intermittently leaks a well-formed tool call as **text** instead of
native `tool_calls` (observed deterministically at one step) — so any real
harness needs a **tolerant parser** that recovers a tool call from message
content when the native field is empty. OpenHands has no such fallback; the
minimal loop's prose-nudge accidentally provided one. **The recommended true
solution is a purpose-built minimal native-tool-calling driver for local
dispatch, replacing the OpenHands CLI path for this role** (see §8). The
`run_openhands_headless.py` security_risk fix stays useful only if we keep
any OpenHands path at all; on the minimal-driver route it is moot.

**Not yet started:** Step 5 (per-backend resource gate — today's usage gate
is still Claude-only `/cost`; local dispatch is not yet decoupled from the
Claude usage pause). Routing `review` to local remains correctly blocked
(`OllamaDriver.dispatch()` raises `NotImplementedError` for any
`allowed_tools` that excludes Edit/Write) since OpenHands has no read-only
mode yet.

---

## 1. The core insight: two call types, not one

`pipeline_mcp_server.py` shells out to `claude` in four places, and they are not
the same kind of call. This split is the whole design:

| Call site | Type | Notes |
| :--- | :--- | :--- |
| `dispatch_story` → `_build_dispatch_command` (`:310`, `:899`) | **Agentic** — needs a tool loop (edit/git/test), runs in a worktree, calls back into the MCP (`checkpoint`). | High volume. The token sink. |
| `_run_reviewer` (`:416`) | **Agentic (read-only)** — runs tests, reads files, emits a `VERDICT:` line. | High volume. |
| `_invoke_overlord` (`:382`) | **Single-shot completion** — prompt in, `RULING:/TIER:/RISK:` text out. | Low volume, rare. |
| `_run_usage_probe` (`:591`) | **Not an LLM call** — reads Claude `/cost` subscription data. | Becomes a per-backend resource check. |

The doc's "replace `claude -p` with an HTTP call" only works for the overlord. A
bare Ollama endpoint gives you `generate(prompt)` — it is **not** an agent
runtime. The agentic roles need a harness (tool loop + file edits + git). That
harness, not the model, is the real engineering.

### Why this is safe to invert

The merge-safety gate is already **model-free**:
- `review_story` (`:1188`) produces a `VERDICT:` but explicitly *"does not merge —
  merge is the overlord's decision."*
- `_merge_decision` (`:477`) is a **pure function** (risk threshold + autonomy),
  no model call — see the comment at `:1380`: *"Adjudicate merges … (no model
  usage)."*

So a weaker local reviewer can only `REQUEST_CHANGES` (safe) or `APPROVE` →
which still passes through `_merge_decision`'s risk threshold. Keep
`PIPELINE_RISK_THRESHOLD` conservative and anything non-trivial **parks** for a
human or Claude instead of self-merging. The judgment that protects you stays on
Claude (cheap, rare); the bulk compute moves local.

---

## 2. Target architecture (role routing)

| Role | Default backend | Rationale |
| :--- | :--- | :--- |
| **Dispatch (coding)** | Local Devstral 24B via OpenHands | Biggest token sink → off Claude. Slow is fine; it runs overnight. |
| **Review (tests + VERDICT)** | Local Devstral 24B via OpenHands | Bounded blast radius (merge gate is model-free). |
| **Overlord / decisions** | Claude (`claude -p`, single-shot) | Rare, cheap, strongest judgment. Won't exhaust limits overnight. |
| **Resource gate** | Per-backend | Claude → `/cost`; local → Ollama health + concurrency cap. |

Every cell is **config**, not code — flip any role to a cloud open-weights
endpoint or an external GPU later without touching the orchestrator.

---

## 3. The abstraction (no lock-in)

A `Backend` protocol with three methods, mapping exactly to the call types:

```python
class Backend(Protocol):
    def complete(self, prompt, system, model, tools=None) -> str: ...
    def run_agent(self, prompt, system, model, tools, cwd, log_path) -> AgentHandle: ...
    def resource_status(self) -> dict: ...   # {"ok": bool, "reason": str}
```

**Drivers** (the swappable unit):

1. `ClaudeCliDriver` — today's `claude -p` code, moved verbatim. Implements all
   three (`complete`, `run_agent`, `resource_status` via `/cost`).
2. `OpenHandsDriver` — the local agentic harness. `run_agent` drives
   [OpenHands](https://github.com/All-Hands-AI/OpenHands) headless against an
   OpenAI-compatible endpoint; `complete` hits `/v1/chat/completions` directly;
   `resource_status` checks the Ollama endpoint + a concurrency cap.
3. `OpenAICompatDriver` — single-shot `complete` for cloud open-weights
   (DeepSeek, Qwen, Ollama Cloud). For agentic roles it reuses `OpenHandsDriver`
   pointed at the cloud endpoint.

**No-lock-in mechanism:** OpenHands speaks the OpenAI-compatible API, and so do
Ollama (local), Ollama Cloud, vLLM (external GPU), and most hosted open-weights
APIs. Switching local ↔ external GPU ↔ cloud is **changing an endpoint URL and a
model name** — nothing in the orchestrator changes.

### Config shape

```
# Per-role routing
PIPELINE_BACKEND_DISPATCH=local        # local | claude
PIPELINE_BACKEND_REVIEW=local
PIPELINE_BACKEND_OVERLORD=claude

# Backend "local" definition (swap these to retarget cloud/external GPU)
PIPELINE_LOCAL_DRIVER=openhands
PIPELINE_LOCAL_ENDPOINT=http://localhost:11434/v1   # Ollama → later vLLM/cloud
PIPELINE_LOCAL_MODEL_OPUS=devstral:24b              # tier → concrete model
PIPELINE_LOCAL_MODEL_SONNET=qwen2.5-coder:14b
```

### Model tiers

Personas (`agents/*.md`) and stories declare Claude names (`opus`, `sonnet`) —
treat those as **logical tiers**, not literal models. A `model_map` resolves
tier → concrete model per backend. Persona *bodies* (system prompts) port
verbatim via the existing `_persona_body`.

---

## 4. Implementation steps

**Step 0 — Backend seam.** New `backend.py` with the `Backend` protocol +
`AgentHandle`. A `get_backend(role)` factory reads the routing config.

**Step 1 — `ClaudeCliDriver` (pure refactor, zero behavior change).** Move the
subprocess bodies of `_invoke_overlord`, `_run_reviewer`, the `dispatch_story`
Popen, and `_run_usage_probe` into the driver. The four functions become thin
wrappers calling `get_backend(role)`. Re-point test mocks to the driver. *The
state machine — manifest/journal, `advance_pipeline`, locking, pause/resume,
worktrees, Plane, PR opening — is untouched.*

**Step 2 — Config & model map.** `PIPELINE_BACKEND_*` routing, `model_map`,
factory wiring.

**Step 3 — `OpenAICompatDriver.complete()`.** The easy 80%: overlord-style
single-shot via `/v1/chat/completions` (httpx, already a dep). Output contracts
(`RULING:`, `VERDICT:`) are already tag-based — add a **validate-and-retry**
wrapper since a 24B model occasionally drops a tag.

**Step 4 — `OpenHandsDriver.run_agent()` (the real work).** Drive OpenHands
headless: translate the existing dispatch/review prompt + persona system prompt
+ worktree `cwd` into an OpenHands run; stream to `agent.log`; return a handle
whose liveness `check_story_status` can poll (same pid/`ps` mechanism as today).
Map persona tool allow-lists (`_PERSONA_TOOLS`) to OpenHands tool config —
reviewer stays read-only (Bash+Read, no edits).

- **Checkpoint resumability:** dispatched Claude agents call the `checkpoint` MCP
  tool. OpenHands supports MCP servers — configure it to connect to this
  pipeline MCP so local agents keep checkpointing. (Fallback: rely on git
  commits as the resume signal.)

**Step 5 — Per-backend resource gate.** Today `advance_pipeline` pauses on the
global Claude usage state (`:1305`). Once dispatch is local, gating local
dispatch on the Claude limit is wrong — it would needlessly freeze overnight
runs. Make the gate consult the **dispatch backend's** `resource_status()`:
- Claude driver → `/cost` (unchanged).
- Local driver → Ollama reachable + `MAX_CONCURRENT_AGENTS` cap → effectively
  "always ok." **This is what unlocks true overnight autonomy** — local dispatch
  is no longer throttled by your Claude weekly limit. The overlord still spends
  Claude usage, but it's single-shot and rare.

**Step 6 — Tests.** A `FakeBackend` so state-machine tests run backend-agnostic;
contract-parse tests for messier local output; one integration smoke test
against a small local model.

---

## 5. Preserved (do not touch)

The original doc's "Step 2" list is correct and verified against the code:
`manifest.json` / journals as the single source of truth, `advance_pipeline`,
`_merge_decision`, locking, `pause_plan`/`resume_plan`, worktree mechanics, Plane
integration, PR opening. These are independent of which model runs the work.

---

## 6. Open risks / calibration

- **Devstral 24B quality ceiling** on agentic multi-file dispatch is unproven for
  your stories — start with low-risk/small stories routed local, keep
  `PIPELINE_RISK_THRESHOLD` conservative so anything bigger parks.
- **Context window** on a 24 GB box is tight for large diffs + test output;
  prompt templating must stay surgical.
- **OpenHands ↔ Ollama integration** is the critical-path dependency; Step 4 is
  where schedule risk lives. Hybrid (local review, Claude dispatch) is the
  fallback if local dispatch proves too weak.

---

## 7. Suggested order of delivery

1. Steps 0–2 (seam + ClaudeCliDriver + config) — pure refactor, tests green, no
   behavior change. **Low risk, do first.**
2. Step 3 (`complete`) + route **overlord-style single-shot** to local as a
   trial — cheapest way to validate the local endpoint end-to-end.
3. Step 5 (per-backend gate) — required before local dispatch can run unattended.
4. Step 4 (`run_agent` via OpenHands) — the real project; start with **review**
   (lower bar, bounded by merge gate), then **dispatch**.
5. Step 6 tests throughout.

---

## 8. Revised local-dispatch harness (supersedes "via OpenHands" for the
local backend)

Evidence (§Status, item 2) shows OpenHands' CLI is the wrong harness for a
24B local model: its heavy tool surface collapses devstral's native
tool-call format adherence, and its strict native-only parsing has no
recovery when the model leaks a tool call as text. The verified-working
shape is a small purpose-built driver:

- **Endpoint:** Ollama native `/api/chat` directly (we already use it for
  `OllamaDriver.complete()`), `options.num_ctx` pinned (16384 fits 100% GPU
  on the 24GB M4). No litellm, no `openhands_cli`.
- **Tools:** a *minimal, clean* schema set — `bash` (covers file
  create/edit via the shell, git, tests) and `done`; optionally a dedicated
  `str_replace` editor later. **No** `security_risk`/`summary` param
  injection, **no** `task`/`task_tracker`/`invoke_skill`, **no** skills
  auto-injection. Keeping the tool JSON small is load-bearing for format
  adherence.
- **Tolerant parsing (the critical robustness feature):** after each
  response, if the native `tool_calls` field is empty, attempt to recover a
  tool call from the message *content* — match `[TOOL_CALLS]`/`[{...}]`
  arrays and ```json fences, parse, and execute. Only treat it as "no
  action" if nothing parses, then nudge once.
- **Loop:** system prompt (short, "call a tool, don't narrate; commit when
  done") → `/api/chat` → execute tool in the worktree `cwd` → append a
  `role:"tool"` result → repeat until `done` or a step cap. Returns the same
  pid/`AgentHandle` contract (run as a subprocess) so `check_story_status`
  polling is unchanged.
- **Checkpoint/resumability:** the loop can call `_checkpoint_impl` directly
  in-process after each committed step (no MCP server needed at all — that
  whole MCP-attachment line of investigation is mooted by owning the loop).
- **Read-only review role:** trivially supported by giving the loop a
  read-only tool set (no write/commit) — removes today's
  `NotImplementedError` block on routing `review` local.

This keeps the `Backend`/`AgentHandle` seam intact (no orchestrator change);
it swaps only the *body* of `OllamaDriver.dispatch()`. Risk that remains is
the **model quality ceiling** on real multi-file stories (§6), which is
orthogonal to the format-adherence problem solved here — gate it behind a
conservative `PIPELINE_RISK_THRESHOLD` and start on small stories.

### Validation on a realistic story (the quality ceiling is real)

Ran the minimal loop (with tolerant parser, `bash`+`done`, temp 0.3, 30-step
cap) on a genuine TDD story: implement `roman.py` (`int_to_roman` /
`roman_to_int`, 1..3999) + a pytest `test_roman.py`, run pytest, iterate to
green, commit. Two-sided result:

- **Tool-format adherence: fully validated.** Across all 30 turns devstral
  emitted valid tool calls (native or text-recovered) — *zero* reversions to
  prose. The core thesis (minimal clean tools + tolerant parser = reliable
  tool-calling for this model) holds even over a long multi-turn run.
- **Reasoning / self-correction: hit a hard ceiling, task failed.** The
  algorithm is trivial (the model surely knows roman numerals); it failed on
  *mechanics*. The generated `test_roman.py` was good but omitted its import;
  the model correctly inferred an import was needed but wrote it into the
  *wrong file* (`roman.py`, the implementation), **destroying its own
  implementation** via a `cat > file << EOF` overwrite, then looped ~25 steps
  flipping `from roman` ↔ `from .roman` — never reading the traceback's
  `test_roman.py:26` location and never inspecting the clobbered file.

Two distinct levers this exposes for a production driver, beyond §8's base
design:
1. **A non-destructive edit primitive is load-bearing, not optional.** Raw
   `bash` + heredoc let the model silently erase its own work. A
   `str_replace`/`create`-style editor (targeted edits, no blind whole-file
   overwrite) prevents the clobber.
2. **Loop/repetition detection** (abort or nudge on N near-identical
   commands) is needed so a confused run fails fast/parks instead of burning
   the step cap.

**Follow-up A/B — the v2 failure was mostly the harness, not the model.**
Re-ran the *same* roman.py story with the *same* model (devstral:24b, temp
0.3) but a v3 harness: a `create_file` that refuses to overwrite a non-empty
file, a unique-match `str_replace`, a `view_file`, `bash` restricted to
running commands, plus the loop guard. devstral **passed cleanly in 7 steps**
— create both files → pytest (red) → one `str_replace` → pytest (green) →
commit → done, no oscillation. The committed code was verified genuinely
correct (canonical implementation; the test really imports from `roman` and
round-trips all of 1..3999; independently re-checked outside the agent). So
the earlier "hard reasoning ceiling" read was overstated: with a proper
editor + guard, devstral 24B *can* carry a real (modest) multi-file TDD
story. The remaining unknowns are **breadth** (one success on a well-known
algorithm is not a reliability distribution — needs a few varied small tasks)
and **headroom** (whether a stronger local coder like `qwen2.5-coder` clears
harder stories) — but the architecture itself (§8) is now validated
end-to-end on a real task, and the editor/guard are confirmed mandatory parts
of it.

### Breadth result (3 varied stories vs. committed baselines)

Ran the v3 harness (devstral, temp 0.3) on three task *types* against an
existing committed baseline (the realistic shape — modify existing code, not
greenfield), each verified independently:
- **Refactor** (extract a `_greet` helper, keep behavior + tests): ✅ fully
  correct, committed — even improved concatenation to an f-string.
- **Feature add** (add a `rectangle` case to `area()` + a test, keep circle/
  square): ✅ fully correct, committed.
- **Bugfix** (diagnose + fix a `median` even-length bug, don't touch tests):
  ⚠️ **code correct (tests pass, tests untouched) but the agent called `done`
  without committing** — left the fix uncommitted in the worktree. It also had
  a messy middle (a non-matching `str_replace`, a `create_file` overwrite
  attempt correctly *blocked* by the no-clobber guard, two prose lapses the
  nudge recovered) before reaching correct code.

Net across 4 realistic tasks (roman + these 3): **3 fully correct end-to-end,
1 correct-but-uncommitted.** Takeaway: devstral 24B + the v3 harness reliably
*produces* correct small changes, but completion-signal discipline is shaky.
This adds a third mandatory driver guard:
3. **Commit enforcement.** Since the pipeline treats git commits as the
   done-signal (`check_story_status`), the driver must not accept a `done`
   call while the worktree is dirty / has no new commit — reject it back to
   the agent ("commit your work first") or auto-create a WIP commit. Without
   this, a correct change can be silently lost.

### Headroom test — `qwen2.5-coder:14b` is *worse* here, not better

Ran the identical v3 harness + same 4 tasks with `qwen2.5-coder:14b` (the
largest qwen-coder that fits: only ~21GB disk free and 24GB RAM rule out the
32b, which would also force a CPU/GPU split). Result: **1/4 fully correct vs.
devstral's 3/4.** Qwen churned — repetitive view/edit cycles that tripped the
loop-repetition guard on 3 of 4 tasks — and on the refactor it actively
*broke* working code (`NameError: _greet not defined`) before parking; on the
feature it never implemented the change. Devstral was decisive and tripped the
guard on none of them. (Qwen also emits tool calls as bare JSON without its
`<tool_call>` tags and renamed a tool `bash`→`run` in a probe — extra friction
the tolerant parser mostly absorbs, but a tell.) Single-run, temp 0.3, and the
guard is deliberately aggressive — but the gap is wide enough to conclude:
**on this hardware, devstral:24b is the best available local model for this
role; switching to a smaller-but-strong coder does not lift the ceiling.**
Genuine headroom would require a larger model (e.g. 32b) that this box can't
host. Net recommendation: **build the driver around devstral:24b + the three
guards, accept the small/mechanical-stories-only ceiling (park the rest), and
keep Claude as the dispatch fallback for anything bigger.**

### Implemented (BUILT into backend.py)

The driver is now in the codebase, replacing the OpenHands path for local
dispatch:
- **`scripts/local_agent.py`** — the agent loop: native Ollama `/api/chat`,
  tools `create_file`/`str_replace`/`view_file`/`bash`/`checkpoint`/`done`,
  tolerant parser, loop-repetition guard, commit enforcement, and runtime-
  artifact exclusion (keeps `agent.log`/`__pycache__`/`*.pyc` out of git via
  `git rev-parse --git-path info/exclude`, so they don't dirty the tree or
  pollute commits/merges — handles the `git worktree` case where `.git` is a
  file). `checkpoint` calls `pipeline_mcp_server._checkpoint_impl` in-process.
- **`backend.py` `OllamaDriver.dispatch()`** — now just launches
  `local_agent.py` as a subprocess (this project's venv python) with config
  via `LOCAL_AGENT_*` env vars, returning the same `AgentHandle(pid)`. The
  read-only guard (review) is retained. New env knobs:
  `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS`, `PIPELINE_LOCAL_MAX_STEPS`,
  `PIPELINE_LOCAL_TEMPERATURE`; `PIPELINE_LOCAL_NUM_CTX` default raised to
  16384. The three OpenHands scripts (`run_openhands_headless.py`,
  `setup_openhands_local.py`, `checkpoint_mcp_server.py`) were removed.
- **Tests:** 158 green; the OpenHands-settings/MCP unit tests were replaced
  with subprocess-launch + tier-resolution tests.
- **Verified end-to-end through the real `backend.get_backend("dispatch")`
  path** (not mocks), polling the pid exactly as `check_story_status` does:
  run 1 fully succeeded (correct code, committed, clean tree, tests pass);
  run 2 the model produced broken code and the guards **failed safe** —
  parked after the repetition nudge, WIP-committed (nothing lost), clean tree,
  no log/pycache pollution. Both are correct driver behavior (a safe-failure
  is what `check_story_status` then records as "failed" → retry/park). The
  stochastic miss on a trivial task is the documented model ceiling, not a
  driver defect.

Not done (future): routing `review` to local (needs a read-only tool set +
validation; still raises `NotImplementedError`).

## Step 5 — Per-backend resource gate (DONE)

`advance_pipeline` no longer gates the whole tick on one global Claude
`paused` flag. Each model-spending action is gated by **its own backend**:
- New `Backend.resource_status() -> {"ok", "reason"}`. `ClaudeCliDriver`
  reports the poller-fed, hysteresis-stabilized usage gate (reads
  `_read_usage_state().paused` — cheap, no live `/cost` probe; fails open on
  missing state). `OllamaDriver` reports **Ollama reachability** only (`GET
  /api/tags`) — no usage/cost limit, so effectively "always ok" while Ollama
  is up. This is the change that **decouples local dispatch from the Claude
  weekly limit** and unlocks overnight autonomy.
- `advance_pipeline` computes `_role_resource_ok("dispatch")` and
  `_role_resource_ok("review")` separately. If dispatch's backend is gated,
  in-flight agents are interrupted (checkpointed) and no new dispatch starts;
  if review's backend is gated, review is deferred. The two are independent —
  a Claude usage pause with dispatch routed local now leaves local dispatch
  running and only defers Claude review. Merge adjudication still always runs
  (no model usage). Summary gains `dispatch_paused`/`review_paused`; `paused`
  is kept (= dispatch gated) for back-compat.
- Verified: 163 tests green (incl. a new integration test asserting local
  dispatch proceeds while Claude review is gated), and the real
  `resource_status()` methods checked live (Ollama up → ok; dead endpoint →
  not ok; Claude driver → reads real usage state). Existing all-Claude gate
  tests pass unchanged (Claude path still reads the same usage state).
