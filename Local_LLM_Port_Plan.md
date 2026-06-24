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

**Open / unresolved — devstral's tool-calling reliability with any MCP server
attached:** Across real runs, every dispatch *without* an MCP server attached
succeeded (file created, committed); every dispatch *with* one attached
failed — the agent hallucinated a tool name that doesn't exist (`create_file`,
then `file_editor`) and gave up after 2 messages, regardless of whether 19
tools or just 1 (`checkpoint`) were registered. Sample size is small (2 vs. 2),
so this isn't conclusive, but it's a consistent pattern, not noise from tool
clutter alone. **Before relying on local dispatch with checkpointing, this
needs more isolation runs** (vary the checkpoint instruction wording, try a
larger `num_ctx`, test whether *any* MCP tool — not just ours — breaks it) to
determine whether this is a devstral limitation, an OpenHands-version quirk,
or something fixable on our side. Until resolved, local dispatch works
reliably *without* the checkpoint instruction (i.e. `plan_name=None`), but
loses resumability on interruption.

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
