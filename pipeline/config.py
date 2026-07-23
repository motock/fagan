"""Scalar config constants for the pipeline MCP server.

Read once at import time from env vars; every other module imports these by
name rather than re-reading the env, so there is one source of truth per
knob. Path constants (PLAN_DIR / WORKTREE_ROOT / AGENTS_DIR / REPO_ROOT) live
in pipeline_paths; PLANE_* live in pipeline_ticketing with the provider code.
"""

import os

# Autonomy: dry-run (plan/log only) | gated (act up to threshold) | full.
PIPELINE_AUTONOMY = os.environ.get("PIPELINE_AUTONOMY", "gated").lower()
# Highest story risk the overlord may act on unattended.
PIPELINE_RISK_THRESHOLD = os.environ.get("PIPELINE_RISK_THRESHOLD", "low").lower()
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Default model per persona when a story does not override it.
DEFAULT_MODEL = os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")

# Usage gate: pause new dispatch/review when either window reaches its own
# PAUSE_THRESHOLD%; once paused, stay paused until both windows drop back
# below their own RESUME_THRESHOLD% (hysteresis prevents flapping right at
# the boundary). Session and week have independent thresholds because the
# week window resets far less often, so a high weekly total shouldn't gate
# session-level work as tightly as a high session total should.
SESSION_PAUSE_THRESHOLD = int(os.environ.get("PIPELINE_PAUSE_THRESHOLD", "90"))
SESSION_RESUME_THRESHOLD = int(os.environ.get("PIPELINE_RESUME_THRESHOLD", "70"))
WEEK_PAUSE_THRESHOLD = int(os.environ.get("PIPELINE_WEEK_PAUSE_THRESHOLD", "90"))
WEEK_RESUME_THRESHOLD = int(os.environ.get("PIPELINE_WEEK_RESUME_THRESHOLD", "70"))
# How long a frozen (parse-failure) usage reading is trusted before the gate
# fails open. Guards against a CLI output-format change turning a transient
# blackout into a permanent pause.
USAGE_STALE_AFTER_SECONDS = int(os.environ.get("PIPELINE_USAGE_STALE_AFTER_SECONDS", "1800"))
# Request-count thresholds for the new CLI format (post percentage removal).
# session_pct = min(100, daily_requests * 100 // DAILY_REQUEST_THRESHOLD)
# week_pct   = min(100, weekly_requests * 100 // WEEKLY_REQUEST_THRESHOLD)
DAILY_REQUEST_THRESHOLD = int(os.environ.get("PIPELINE_DAILY_REQUEST_THRESHOLD", "3000"))
WEEKLY_REQUEST_THRESHOLD = int(os.environ.get("PIPELINE_WEEKLY_REQUEST_THRESHOLD", "15000"))
# After the gate has been blind this long, flip to fail-closed (paused=True)
# so a permanent CLI-format change doesn't leave spend unguarded indefinitely.
USAGE_BLIND_PAUSE_AFTER_SECONDS = int(os.environ.get("USAGE_BLIND_PAUSE_AFTER_SECONDS", str(6 * 3600)))
# Emit a blind-gate stderr log only on the first blind transition and every
# Nth poll thereafter (default hourly at 60 s poll cadence = 60 polls).
USAGE_BLIND_LOG_INTERVAL = int(os.environ.get("USAGE_BLIND_LOG_INTERVAL", "60"))

# Cap on agents dispatched and running at once, across all plans in this
# session. The usage gate above reacts to a polled /cost snapshot, which lags
# real spend — dispatching every ready story in one tick can let that many
# agents collectively burn through the window before the next poll trips the
# pause. Capping concurrency bounds how much can be spent between polls.
# <=0 disables the cap (dispatch every ready story each tick).
MAX_CONCURRENT_AGENTS = int(os.environ.get("PIPELINE_MAX_CONCURRENT_AGENTS", "3"))

# Error budget for the merge step. _merge_pr shells out to `gh`/`git push`,
# any of which can fail transiently (network, a momentary GitHub 5xx). Rather
# than crash the tick or burn the story on the first hiccup, a failed merge
# leaves the story pr_open and bumps its attempt counter; once attempts reach
# MERGE_MAX_ATTEMPTS the story is marked failed for human intervention.
MERGE_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_MERGE_MAX_ATTEMPTS", "3"))

# Error budget for dispatch (per-story, across ticks). A dispatch can fail two
# ways: dispatch_story raises (git pull/worktree/backend error), or the agent
# launches but produces no output (empty agent.log - a failed launch). Either
# bumps the story's dispatch_attempts; while under budget the story stays
# dispatch-eligible (todo/interrupted) and the next tick retries it, but once
# attempts reach DISPATCH_MAX_ATTEMPTS it is marked failed (terminal - failed
# is not dispatch-eligible) so a story that can never launch stops looping.
# Cleared once a launch actually produces output. Legitimate usage-gate
# interrupts go through interrupt_story and never touch this counter.
DISPATCH_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_DISPATCH_MAX_ATTEMPTS", "3"))

# How long after Popen to trust that an empty agent.log means the agent is
# still bootstrapping (alive but its first print() hasn't flushed) rather
# than genuinely dead. 90s covers Ollama's `-np 1` queue waits for one
# request against devstral:24b even when 2-3 dispatches collide, while still
# flagging a script-crash-before-any-print within a couple of polls.
# Defense-in-depth with local_agent.py's `[boot]` heartbeat - even older
# agents without the heartbeat still benefit from this grace window.
DISPATCH_STARTUP_GRACE_SECONDS = int(
    os.environ.get("PIPELINE_DISPATCH_STARTUP_GRACE_SECONDS", "90")
)

# Absolute ceiling on how long a dispatched agent process may stay alive
# before check_story_status treats it as hung rather than "running". The
# step cap and per-call LLM timeout are supposed to bound a dispatch, but a
# blocking, non-streaming provider call can stall indefinitely on a single
# stuck request (observed directly during MLX provider validation: a
# dispatch subprocess sat at 0% CPU with no error, past the outer harness's
# own timeout, and was found still running minutes later — the harness never
# killed it). Generous default so a legitimately slow local run isn't killed
# mid-flight.
DISPATCH_WATCHDOG_SECONDS = int(
    os.environ.get("PIPELINE_DISPATCH_WATCHDOG_SECONDS", "3600")
)

# Terminal markers the headless agent prints on the LAST line of its
# agent.log when it hits its step cap and exits with code 2. The agent
# has already WIP-committed its in-progress work before printing these,
# so the right thing for the orchestrator to do is mark the story
# "interrupted" (dispatch-eligible, resumable from the existing worktree
# and journal) — NOT run the test suite against the WIP commit and label
# it tests_passed (which is merge-eligible and was how incomplete work
# landed on master in PR #49 / commit 90a3cf1). Local-agent marker first,
# oracle marker second; check_story_status matches the LAST non-empty line
# of agent.log against this tuple.
STEP_CAP_MARKERS = (
    "[ended without done — step cap reached]",
    "[ended without oracle green — step cap reached]",
)

# Substring (not exact-match, unlike STEP_CAP_MARKERS - the message carries a
# variable exception string) marking a dispatch that died on an INFRASTRUCTURE
# failure (an Ollama/LLM transport error, after chat()'s own retries and the
# 5xx trim-retry are exhausted) rather than a genuine review/test-quality
# outcome. Found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD): two
# separate infra deaths (an Ollama 500, an outright timeout) each burned a
# full rework_attempts slot exactly like a real REQUEST_CHANGES cycle would,
# even though the model never got a fair, complete attempt either time - the
# rework cap parked the story partly on infrastructure flakiness it had no
# way to avoid. check_story_status routes a last-line match here to
# "interrupted" (dispatch-eligible, resumes from the WIP commit) WITHOUT
# incrementing rework_attempts, mirroring the STEP_CAP_MARKERS branch but
# without that branch's model-fallback-switching logic (an infra blip is not
# evidence the MODEL is struggling, so it must not trigger a model switch).
INFRA_FAILURE_LOG_SUBSTRING = "LLM call failed"

# A story that keeps hitting the step cap is classified "interrupted" (see the
# STEP_CAP_MARKERS branch below), never "failed" - so it never reaches the
# "failed"-gated local_model_fallback check in advance_pipeline's polling loop
# and can cycle on a struggling model forever. This threshold gates a SEPARATE
# fallback: after this many consecutive step-cap interrupts on the same model,
# switch story["model"] (never story["backend"] - stays local, never Claude)
# for the next resume. Only takes effect when the plan has opted in via
# manifest["local_model_fallback"] (see _escalate_to_local_fallback_model).
#
# When the plan has NOT opted into local_model_fallback, the same threshold
# and the same streak fields instead gate escalation to Claude (see
# _escalate_to_claude, called from check_story_status) once
# PIPELINE_BACKEND_DISPATCH=auto - a repeated step-cap streak indicates a
# local-model capability problem, not a review-convergence problem, so under
# auto dispatch it is treated the same as a local test-failure escalation.
# The two fallbacks are mutually exclusive (no chaining): a plan with
# local_model_fallback configured always takes the local-fallback path.
STEP_CAP_FALLBACK_THRESHOLD = int(
    os.environ.get("PIPELINE_STEP_CAP_FALLBACK_THRESHOLD", "3"))

# Layered local-first dispatch (PIPELINE_BACKEND_DISPATCH=auto):
#   1. A-priori: stories with risk above PIPELINE_LOCAL_MAX_RISK (default "low")
#      or a security persona go straight to Claude.
#   2. A-posteriori: if the local agent fails (bad code / test failure), the
#      orchestrator escalates that specific story to Claude and starts clean.
# Explicit "local" or "claude" values bypass the risk-ceiling half of this
# router, but NOT the security-persona half: dispatch_story applies the
# persona override (see _persona_requires_claude) regardless of dispatch
# mode, unless the story already has an explicit story["backend"] (e.g. from
# a prior escalation), which always wins as-is.
PIPELINE_LOCAL_MAX_RISK = os.environ.get("PIPELINE_LOCAL_MAX_RISK", "low").lower()
_LOCAL_SKIP_PERSONAS = {"security-engineer"}

# RELIABILITY_PLAN.md T16: "local" (env-resolved via PIPELINE_LOCAL_PROVIDER)
# is a permanent back-compat alias; "ollama"/"lmstudio"/"mlx" let a role name
# the actual local transport directly (backend._DRIVERS). A resolved backend
# name that could be any of these four must be treated identically by any
# gate keyed on "is this dispatch local-family" - NOT the two-value
# {"local","claude"} space _route_dispatch_backend()'s auto router returns,
# which is intentionally left alone (auto-mode doesn't route to a specific
# provider, only local-vs-claude).
_LOCAL_BACKEND_NAMES = frozenset({"local", "ollama", "lmstudio", "mlx"})

# Rework budget. When the reviewer returns REQUEST_CHANGES the story is sent
# back for rework (redispatched with the reviewer's feedback). To stop a story
# the reviewer keeps rejecting from looping through review/rework forever, cap
# the cycles: once rework_attempts reaches REWORK_MAX_ATTEMPTS the story parks
# for human review instead of redispatching again. Cleared on APPROVE.
REWORK_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_REWORK_MAX_ATTEMPTS", "3"))

# Oracle-aware rework budget: a story carrying a non-empty `acceptance`
# block already has an objective, pre-verified correctness signal (it only
# reaches review after tests - including the oracle - pass), so a reviewer
# that keeps finding beyond-oracle issues on 3 full cycles is mostly
# spending time, not changing the outcome (2026-07-03 replication run: 5 of
# 12 non-successes were ground-truth-correct code that still burned the
# full budget before parking). A lower cap converges to the same "parked
# for human review" endpoint faster. Falls back to REWORK_MAX_ATTEMPTS for
# any story without a truthy `acceptance` list (ordinary TDD, where the
# reviewer's judgment is the primary correctness signal and deserves the
# full budget).
REWORK_MAX_ATTEMPTS_ORACLE = int(os.environ.get("PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE", "1"))

# Rework budget for a story that has already been escalated to Claude (see
# _escalate_review_to_claude). REWORK_MAX_ATTEMPTS_ORACLE exists to converge
# LOCAL review fast; once escalation has already paid its cost (real Claude
# usage, and often real wall-clock time - see 2026-07-04's benchmark
# validation, where a story that escalated could take hours if the Claude
# reviewer got rate-limited), reusing that same tight 1-attempt cap just
# throttles Claude's shot at the SAME feedback for no benefit - 6 of 11
# escalated cells in that validation run parked after exactly 1 post-
# escalation cycle. Takes priority over REWORK_MAX_ATTEMPTS_ORACLE
# regardless of whether the story has an acceptance oracle, since once
# escalated the story is on the "give it a real shot" track, not the
# "converge fast" track.
REWORK_MAX_ATTEMPTS_ESCALATED = int(os.environ.get("PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED", "3"))

# Inconclusive-review budget. A non-rate-limited UNKNOWN verdict (a reviewer
# response with no parseable VERDICT line, or the fail-safe path for a
# reviewer backend's own internal error) is not evidence the story needs
# rework - it's an infrastructure hiccup. Counting it against
# REWORK_MAX_ATTEMPTS would let a flaky reviewer silently exhaust the rework
# budget and park a correct implementation, and redispatching the agent with
# the (empty) reviewer output would make it rework blind. So leave the
# story's status untouched and let the next advance_pipeline tick retry
# review instead - but cap the retries too, since an inconclusive reviewer
# that never recovers would otherwise loop forever just like an unbounded
# rework cycle would. Cleared on any conclusive verdict (APPROVE or
# REQUEST_CHANGES).
REVIEW_INCONCLUSIVE_MAX = int(os.environ.get("PIPELINE_REVIEW_INCONCLUSIVE_MAX", "2"))

# Error budget for Plane state transitions. Plane sync is a best-effort side
# effect of an action that already succeeded in git, so its budget is an inline
# retry (not an across-ticks retry like merge/dispatch): _plane_set_state
# retries a transient failure up to PLANE_MAX_ATTEMPTS, then records the drop
# durably (notify, not a silent print) rather than raising.
PLANE_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_PLANE_MAX_ATTEMPTS", "3"))


__all__ = [
    "PIPELINE_AUTONOMY",
    "PIPELINE_RISK_THRESHOLD",
    "_RISK_ORDER",
    "DEFAULT_MODEL",
    "SESSION_PAUSE_THRESHOLD",
    "SESSION_RESUME_THRESHOLD",
    "WEEK_PAUSE_THRESHOLD",
    "WEEK_RESUME_THRESHOLD",
    "USAGE_STALE_AFTER_SECONDS",
    "DAILY_REQUEST_THRESHOLD",
    "WEEKLY_REQUEST_THRESHOLD",
    "USAGE_BLIND_PAUSE_AFTER_SECONDS",
    "USAGE_BLIND_LOG_INTERVAL",
    "MAX_CONCURRENT_AGENTS",
    "MERGE_MAX_ATTEMPTS",
    "DISPATCH_MAX_ATTEMPTS",
    "DISPATCH_STARTUP_GRACE_SECONDS",
    "DISPATCH_WATCHDOG_SECONDS",
    "STEP_CAP_MARKERS",
    "INFRA_FAILURE_LOG_SUBSTRING",
    "STEP_CAP_FALLBACK_THRESHOLD",
    "PIPELINE_LOCAL_MAX_RISK",
    "_LOCAL_SKIP_PERSONAS",
    "_LOCAL_BACKEND_NAMES",
    "REWORK_MAX_ATTEMPTS",
    "REWORK_MAX_ATTEMPTS_ORACLE",
    "REWORK_MAX_ATTEMPTS_ESCALATED",
    "REVIEW_INCONCLUSIVE_MAX",
    "PLANE_MAX_ATTEMPTS",
]