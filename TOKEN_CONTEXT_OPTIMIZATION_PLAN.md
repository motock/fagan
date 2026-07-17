# Plan: Trim token waste in the planner and reviewer roles

**Status:** NOT YET IMPLEMENTED. Plan only, 2026-07-17. Awaiting user review.
The first step is a measurement, not a change — see Step 0.
**Origin:** 2026-07-17 exploration (user question): are the decompose/tech-lead
planner and the reviewer carrying full context that leads to unnecessary token
usage? Investigation found the **user-prompt** side of both roles is already
lean; the waste is concentrated in **duplicated system prompts re-billed every
call with no prompt caching**, plus a **dead output-cap knob**, plus **no
input-context cap** on the Claude reviewer.

---

## Findings (verified)

### Role 1 — Planner / tech-lead checklist (`PIPELINE_DECOMPOSE=cloud`)

- Called **once per story** at first dispatch (`pipeline_mcp_server.py:3185`),
  gated (3179-3183) so resumes/rework don't re-spend it. A separate rework
  planner (`_run_rework_planner`, 1152) fires per rework cycle with only
  `review_feedback` as its user prompt (3243).
- **User prompt is already scoped**: only the single story's `agent_instructions`
  (1100). No plan, no epics, no sibling stories, no repo tree, no files, no
  acceptance fixtures. The user's "full context" suspicion is *not* confirmed
  here on the user-prompt side.
- **System prompt is duplicated full-price every call**: `_PLANNER_SYSTEM`
  (~300 tok, 957-1001) + optional `_PLANNER_SCRATCHPAD_CLAUSE` (~120 tok) on
  every story; `_REWORK_PLANNER_SYSTEM` (~220 tok, 1119-1148) on every rework
  cycle. For N stories that is ~300N tokens of identical system text re-billed.
- **No prompt caching**: the Claude path builds `claude -p --append-system-prompt
  <system>` via subprocess (`backend.py:176-178`) and never sets `cache_control`
  anywhere (verified across backend.py, local_agent.py, inference_providers.py,
  pipeline_mcp_server.py). `cache_creation_input_tokens` / `cache_read_input_tokens`
  are only *read* from the CLI's usage output (`backend.py:218-219`), never *set*.
- **No input or output caps** on the planner: `agent_instructions` passes
  through at whatever size the plan author wrote; `max_tokens` is not passed to
  the planner `complete()` (1099-1103).

### Role 2 — Reviewer (`_run_reviewer`, `review_story`)

- **User prompt is already lean**: the reviewer preloads **no** diff, files,
  plan, or spec. It gets `cwd=worktree` + `allowed_tools="Bash,Read"` (1380)
  and tool-calls into the tree on demand. The acceptance list is used only to
  **scope the test command** (paths), not embedded as text (1310-1328). The
  prompt body itself is ~1-2 KB (1337-1364).
- **System + static-prompt duplicated full-price every call**:
  `_persona_body("code-reviewer")` (1254) + the static review-checks block
  (1337-1364, ~1-2 KB) re-sent on every review call, 1-3× per story (initial +
  rate-limit fallback 4067 + transient retry 4088-4095) plus the security
  reviewer (4105) on high-risk stories. No prompt caching on the Claude path.
- **Local reviewer has a per-tool output cap** the Claude reviewer lacks:
  `_run_readonly_tool` truncates `view_file` and `bash` output to 3000 chars
  (`backend.py:514, 535`). So the local reviewer can't load a whole large
  file; the Claude reviewer (real `claude` CLI Read/Bash) can, at the model's
  discretion.
- **`PIPELINE_REVIEW_MAX_TOKENS` is a dead knob**: documented as an output cap
  (default 4096, passed at `pipeline_mcp_server.py:1381`) but does **nothing**
  on either backend — Claude path `del max_tokens` (`backend.py:187`, the CLI
  dropped `--max-tokens`); local path "max_tokens is not honored by either
  driver now" (`backend.py:625`, Ollama caps via `num_ctx`).
- **No input-context cap** on any role. The only input-side guard that exists
  is the local reviewer's 3000-char per-tool truncation.

### Summary of waste

| Waste | Where | Rough cost |
|---|---|---|
| Planner system prompt re-billed per story, no cache | 1101, 1167 | ~300 tok × N stories + ~220 tok × rework cycles |
| Reviewer system+static prompt re-billed per call, no cache | 1254, 1337-1364 | ~1.5-2 KB × (1-3 retries + security) × stories |
| Dead `PIPELINE_REVIEW_MAX_TOKENS` | backend.py:187, 625 | misleading; 0 savings today |
| No input-context cap (Claude reviewer can pull whole files) | — | unbounded, model-discretion |

---

## Step 0 — Measure before changing (do this FIRST)

The Claude CLI **reports** `cache_read_input_tokens` and `cache_creation_input_tokens`
in its usage output (`backend.py:218-219`). That means it *may* already auto-cache
the system prompt / conversation prefix — in which case the "duplicated system
prompt" waste is largely already mitigated and the caching work below is low
value. **Do not assume the duplication is full-price; verify.**

Action: on a short benchmark run (a few cells), log `cache_read` vs
`cache_creation` per planner and per reviewer call (they're already parsed in
backend.py; surface them in the run log or a side file). Decision:
- If `cache_read > 0` on repeat calls within a story → caching is working;
  deprioritize Step 1, focus on Steps 2-3.
- If `cache_read == 0` → the duplication is real full-price waste; proceed to
  Step 1.

This is cheap and gates the rest of the plan. (Per the verify-don't-assume
discipline: the investigation found no `cache_control` is *set*, but that
doesn't prove the CLI isn't auto-caching — only the measurement does.)

---

## Stories (ingest_plan schema)

```json
{
  "repo_root": "/Users/jessecarroll/.claude/mcp-servers/pipeline",
  "epics": [
    {
      "summary": "Token efficiency for planner and reviewer",
      "stories": [
        {
          "summary": "Measure actual prompt-cache hits on planner and reviewer calls before assuming system-prompt duplication is full-price waste",
          "description": "Step 0. The Claude CLI reports cache_read_input_tokens / cache_creation_input_tokens (parsed at backend.py:218-219). Log these per planner and per reviewer call on a short run and report cache_read vs cache_creation. Gates the caching work: if the CLI already auto-caches the system prefix, the duplication is cheap and Step 1 deprioritizes.",
          "agent_instructions": "CONTEXT: TOKEN_CONTEXT_OPTIMIZATION_PLAN.md Step 0. backend.py:218-219 already parses cache_creation_input_tokens and cache_read_input_tokens from the Claude CLI usage output. The investigation found no cache_control is SET anywhere, but the CLI may auto-cache the system/conversation prefix — only measurement confirms whether the duplicated system prompts (planner _PLANNER_SYSTEM ~300 tok × N stories; reviewer persona+checks ~1.5-2KB × 1-3×/story) are re-billed full price or largely cached.\n\nWHAT TO BUILD: add lightweight logging that surfaces, per planner call and per reviewer call, the cache_read_input_tokens and cache_creation_input_tokens (and total input tokens) from the CLI usage. Run a short benchmark (3-4 cells, production-mimicking) and report: per-role, are repeat calls within a story hitting cache_read > 0? What fraction of input tokens are cache reads vs full-price?\n\nHARD CONSTRAINTS:\n1. This is instrumentation only — do NOT change prompt construction or caching behavior. Read-only measurement.\n2. Do not break the run; the logging must be side-channel (a log file / stderr) and off by default (env-gated) so production runs aren't noisy.\n\nTDD: a unit test that the cache-token fields are parsed and logged correctly given a synthetic CLI usage blob (no real CLI run needed).\n\nTESTABLE SUCCESS CRITERIA: a short run produces a per-role cache-read report; a go/no-go decision for Step 1 is stated from data, not assumption.",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "low",
          "dependencies": []
        },
        {
          "summary": "Prompt-cache the duplicated static system prompts (planner + reviewer) if measurement shows they are re-billed full-price",
          "description": "Step 1, gated on Step 0. Mark the static system blocks (_PLANNER_SYSTEM, _REWORK_PLANNER_SYSTEM, code-reviewer persona + static review-checks) as cacheable so N per-story / per-retry calls share one cached copy instead of re-billing ~300 tok × N (planner) and ~1.5-2 KB × 1-3×/story (reviewer).",
          "agent_instructions": "CONTEXT: TOKEN_CONTEXT_OPTIMIZATION_PLAN.md Step 1, DEPENDENT on the Step 0 measurement showing cache_read == 0 (full-price waste). If Step 0 shows the CLI already auto-caches, SKIP this story. The Claude path builds `claude -p --append-system-prompt <system>` via subprocess (backend.py:176-178) and never marks cache_control.\n\nWHAT TO BUILD: enable prompt caching of the static system blocks for the planner (_PLANNER_SYSTEM 957-1001, _REWORK_PLANNER_SYSTEM 1119-1148) and the reviewer (_persona_body('code-reviewer') 1254 + static review-checks 1337-1364), so the identical system text is written to the cache once and read on subsequent calls.\n\nFEASIBILITY TO NAIL DOWN FIRST: does the `claude -p --append-system-prompt` subprocess route support a cache breakpoint at all? If not, this may require restructuring (e.g. the SDK, or a stable system-prompt prefix the CLI auto-caches). Determine the mechanism before implementing — do not assume.\n\nHARD CONSTRAINTS:\n1. Behavior must be byte-for-byte identical (same prompt text reaches the model); only billing changes.\n2. Cache breakpoints only on blocks that are genuinely identical across calls (the static system text) — never on the per-story user prompt.\n3. If the CLI route can't mark cache breakpoints and auto-caching is the only option, document that and confirm via Step 0's measurement that it's actually happening.\n\nTDD: a test that the cacheable prefix is stable/identical across calls (deterministic content); a measurement re-run showing cache_read > 0 after the change where it was 0 before.\n\nTESTABLE SUCCESS CRITERIA: Step 0 measurement re-run shows cache_read > 0 on repeat planner/reviewer calls; full suite green; no behavior change in outputs.",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "medium",
          "dependencies": ["Measure actual prompt-cache hits on planner and reviewer calls before assuming system-prompt duplication is full-price waste"]
        },
        {
          "summary": "Fix or retire the dead PIPELINE_REVIEW_MAX_TOKENS output cap",
          "description": "PIPELINE_REVIEW_MAX_TOKENS (default 4096) is documented as the reviewer output cap but does nothing on either backend — Claude path del's max_tokens (backend.py:187, the CLI dropped --max-tokens); local path doesn't honor it (backend.py:625, Ollama caps via num_ctx). Either wire it to a real enforcement path or re-document it as advisory-only and stop passing it.",
          "agent_instructions": "CONTEXT: TOKEN_CONTEXT_OPTIMIZATION_PLAN.md. pipeline_mcp_server.py:1381 passes PIPELINE_REVIEW_MAX_TOKENS (default 4096) as max_tokens to the reviewer complete() call. backend.py:187 does `del max_tokens` on the Claude path (the claude CLI dropped --max-tokens). backend.py:625 comments 'max_tokens is not honored by either driver now' on the local path (Ollama caps via num_ctx). So the env var is a documented cap that caps nothing — misleading.\n\nWHAT TO BUILD: pick ONE — either (a) wire it to a real enforcement path that actually caps reviewer output (e.g. truncating the reviewer's raw output to the cap before storing it as review_feedback / PR body, with a marker that it was truncated), or (b) re-document it as advisory-only in the README and stop passing it / remove the dead plumbing. Recommend (a) only if a real truncation site is clean; otherwise (b). The README row must match whatever is chosen.\n\nHARD CONSTRAINTS:\n1. If choosing (a) truncation: never truncate the VERDICT line — the parser needs it. Truncate only the findings prose / PR body, and leave a visible '...[truncated]...' marker.\n2. If choosing (b): remove the dead `del max_tokens` / pass-through cleanly; don't leave dangling references.\n\nTDD: (a) a test that reviewer output longer than the cap is truncated with the marker AND the VERDICT line preserved; (b) a test that the env var is no longer passed / documented as advisory.\n\nTESTABLE SUCCESS CRITERIA: pytest passes; the README row matches the implementation; a reviewer producing >cap output is either correctly truncated (VERDICT preserved) or the cap is honestly documented as advisory.",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "low",
          "dependencies": []
        },
        {
          "summary": "Add an input-context cap for the Claude reviewer mirroring the local reviewer's per-tool truncation",
          "description": "Optional / lower priority. The local reviewer truncates view_file and bash output to 3000 chars (backend.py:514, 535); the Claude reviewer (real claude CLI Read/Bash) has no such cap and can pull whole files into context at the model's discretion. Add an analogous bound so a whole-file Read can't blow out the Claude reviewer's context.",
          "agent_instructions": "CONTEXT: TOKEN_CONTEXT_OPTIMIZATION_PLAN.md. backend.py:514, 535 truncate local reviewer tool output to 3000 chars. The Claude reviewer uses the real claude CLI Read/Bash with no input-side cap.\n\nWHAT TO BUILD: an input-context bound for the Claude reviewer analogous to the local one's 3000-char per-tool truncation. Investigate the cleanest enforcement point — likely a guard on the reviewer's total tool-loaded context, or a Read cap. Keep it conservative so it doesn't reject legitimate large-file reviews.\n\nHARD CONSTRAINTS:\n1. Must not break reviews of legitimately large files — the cap should bound ABUSE (whole-repo reads), not normal review. Consider a per-file cap, not a total cap, to start.\n2. Env-gated / off-by-default if there's any risk of false positives; opt-in.\n\nTDD: a test that an oversized Read is bounded; a test that a normal-sized review is unaffected.\n\nTESTABLE SUCCESS CRITERIA: pytest passes; a Claude reviewer can no longer pull an unbounded whole-file read while normal reviews are unaffected.",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "medium",
          "dependencies": ["Measure actual prompt-cache hits on planner and reviewer calls before assuming system-prompt duplication is full-price waste"]
        }
      ]
    }
  ]
}
```

---

## Explicitly rejected / deferred

- **Shrink the planner/reviewer user prompts.** Rejected: investigation showed
  they're already scoped (planner = one story's `agent_instructions`; reviewer
  = no preloaded diff/files/plan, tool-calls on demand). There's no user-prompt
  bloat to trim.
- **Preload the reviewer with the diff / changed hunks instead of letting it
  tool-call.** Deferred: could save tool-call round-trips but trades determinism
  and risks over-scoping the reviewer. Out of scope for this plan.
- **Cap planner `agent_instructions` input.** Deferred: plan authors control
  that size; a hard cap could truncate implementation briefs. Not a runtime
  waste to fix here.

---

## Validation

- Step 0 produces a data-driven go/no-go for Step 1 (the highest-leverage
  change). If the CLI already auto-caches, the plan's savings shrink to Steps
  2-3 (the dead knob + input cap) — still worth doing, but the headline
  "cache the system prompts" item may be unnecessary.
- After Step 1 (if warranted): re-run the Step 0 measurement; cache_read > 0
  on repeat calls confirms the caching landed.
- Compare total token spend on a fixed 9-cell run before vs after. The savings
  are per-call static-prefix duplication, so they scale with N stories ×
  (1 planner + 1-3 reviewer) calls — modest per call but consistent across
  every run.