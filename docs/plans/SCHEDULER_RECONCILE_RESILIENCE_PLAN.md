# Scheduler Reconcile Resilience Plan

**Status: design doc → plan draft (2026-09-14).** Born from a live incident
the same morning: the freshly-ingested `overlord-parked-story-autonomy`
plan wedged on the dashboard — two stories `in_progress` with dead agent
PIDs — while the scheduler health file reported `alive: true` the entire
time. Two independent defects conspired; either one alone would have been
recoverable, together they produced a *permanent, silent* livelock that
required a manual `launchctl kickstart -k` to clear.

Incident timeline (2026-09-14, all times local):

1. ~08:54 — scheduler dispatches OPSA-1 and OPSA-2 on glm.
2. glm-5.3:cloud outage begins (the same session's classifier timed out
   twice in the same window). OPSA-1 thrashes to its step cap (119 steps,
   dead). OPSA-2 finishes cleanly before the worst of it — 2 commits,
   9537 tests green, DONE emitted.
3. A reconcile tick, advancing OPSA-2 through the review gate, hangs on
   stacked model calls; 900s join deadline expires → worker abandoned.
   `reconcile_timed_out: 4`, `last_error: "reconcile_fn stalled past the
   join deadline (900.1s elapsed); worker abandoned"`.
4. The abandoned worker still holds the plan's `_plan_lock` flock —
   verified directly (`fcntl` → `Errno 35`). Documented KNOWN LIMITATION
   at `pipeline/scheduler_daemon.py:347-353`.
5. Every subsequent tick skips the locked plan *quickly* → completes →
   resets the abandon streak → the LOCKSTARVE-C2 self-restart hatch
   (`:547-559`) never fires. Health stays `alive: true`. Livelock.

Dispatch tier: **cloud open-source** (registry chain → ollama/glm; all 9
roles pinned glm per `model_registry.json`). Sizing per the dispatch rules:
SRR-1 touches 2 production files, ~2 new functions; SRR-2 touches 1
production file (`scheduler_daemon.py`, 653 lines), ~2 new functions.
Sibling chain: both touch `pipeline/scheduler_daemon.py` → sequenced via
dependency (SRR-2 after SRR-1).

---

## Gap A — a reconcile tick can stack more model-call budget than its own watchdog

The per-call timeout is NOT missing: `OllamaDriver.complete` computes a
wall-clock role-call budget once, before the first attempt
(`app/backend_ollama.py:238-268`), from
`resolve_role_call_timeout()` (`app/inference_providers.py:41-59`,
`PIPELINE_ROLE_CALL_TIMEOUT_SECONDS`, hardcoded default **600s**,
parse-disciplined so it never returns None/zero/inf).

What's missing is a **tick-level bound**. One `advance_all_plans` tick
makes several in-process model calls: review-loop turns (each a separate
bounded `complete()`), a security review on risky stories, overlord
adjudications when autonomy is full. Each call is bounded, but the *stack*
is not — and during a provider outage every call burns its full budget
before failing. Two stacked calls (1200s) already exceed the 900s reconcile
join deadline (`_DEFAULT_RECONCILE_JOIN_TIMEOUT_S`,
`scheduler_daemon.py:92`); the review gate's multi-turn loop can stack
three. The result is not a hang forever — it's a hang *just past the
watchdog*, which is the exact shape that triggers the abandonment that
leaks the lock. The watchdog bound and the worst-case model-call budget
were never reconciled against each other.

**Fix:** clamp the *scheduler process's* per-call budget at the daemon
composition root so worst-case stacking sits comfortably inside the join
deadline. New env knob `PIPELINE_SCHEDULER_ROLE_CALL_TIMEOUT_SECONDS`
(default **180s**), applied in `run_daemon()` as
`min(operator role-call timeout, scheduler clamp)` — mutating
`PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` in the daemon's own `os.environ`
before any model call can happen. Interactive MCP-server processes keep
the 600s default; only the process whose calls stack inside one watchdog
deadline gets the tighter budget. Arithmetic the default must satisfy:
k stacked calls × 180s (k ≈ 3-4: review turns + security review) plus
bounded suite runs stays well inside 900s, so the watchdog stops being
the *primary* bound and becomes what it was designed to be — the
backstop. A clamped-out call raises exactly the `RuntimeError` it raises
today; callers already route that to park/defer. The clamp changes only
how long we wait, never the degraded behavior.

## Gap B — the abandon-restart escape hatch is defeated by its own streak reset

The LOCKSTARVE-C2 hatch exists for precisely this failure: after
`PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD` (default 3) *consecutive*
abandonments, the daemon writes health and raises `SystemExit(1)` so
launchd restarts the process — process death being the only release for
a leaked flock (`scheduler_daemon.py:539-559`).

But the streak resets at `scheduler_daemon.py:528-529`:
`if self._consecutive_abandons == streak_at_start:
    self._consecutive_abandons = 0` — i.e. any tick in which no phase
abandons a worker clears the streak. After an abandonment leaks the plan
lock, every subsequent tick *skips the locked plan quickly and completes*,
so the streak always resets and the threshold is never reached. The
hatch's premise — "more consecutive abandonments will follow" — is false
in exactly the scenario it was built for. The daemon reports healthy
forever; the plan is locked forever.

**Fix:** make the hatch see the *persistent* evidence, not just the
count. The daemon already knows something no streak counter can express:
whether a previously-abandoned worker thread is **still alive**. A worker
that is still alive long after its abandonment is a blocked call that has
not returned — which means the plan lock it holds is leaked on a horizon
the daemon cannot influence.

1. `_run_with_watchdog` records each abandoned worker (the `Thread`
   object plus a monotonic timestamp) on `self._abandoned_workers`.
2. Each `run_once`, after the phases: prune dead workers from the list.
   An abandoned worker **still alive past a grace window**
   (`PIPELINE_ABANDON_WORKER_GRACE_SECONDS`, default **300s**) is a
   leaked lock: set `_last_error` to name it, write health (the existing
   pre-exit pattern), raise `SystemExit(1)`. The grace window keeps the
   check from killing the daemon over a merely-slow worker (a live
   suite run can legitimately outlive the join deadline by a little;
   with Gap A landed, no model call should outlive ~180s, so 300s past
   abandonment means genuinely stuck).
3. The streak reset at `:528-529` must not fire while any abandoned
   worker is still alive — the reset's premise ("phases completed
   normally") is false while the leaked lock persists. Gate it on the
   pruned list being empty.
4. `health()` gains additive watchdog fields (`abandoned_workers_alive`
   count, present only when nonzero) — mirroring how
   `scan_timed_out`/`last_scan_timeout_ts` are additive extras, so the
   pinned clean-tick key set is byte-identical when nothing is wrong.

## Stories

| # | Summary | Files | Deps |
|---|---|---|---|
| SRR-1 | Clamp the scheduler process's per-call model budget inside the reconcile join deadline | `app/inference_providers.py`, `pipeline/scheduler_daemon.py` | — |
| SRR-2 | Drive the abandon-restart escape hatch from abandoned-worker aliveness, not the resettable streak | `pipeline/scheduler_daemon.py` | SRR-1 |

Both stories pre-audit the existing scheduler-daemon test files
(`test_scheduler_daemon_abandon_restart.py`,
`test_scheduler_daemon_scan_watchdog.py`, `test_scheduler_daemon.py`)
before touching anything; per-story test files for all new behavior.

## Definition of done

- A scheduler-process model call can never burn more than the scheduler
  clamp (default 180s), and the documented worst-case stacking of one
  reconcile tick sits inside the 900s join deadline.
- A reconcile abandoned while holding a plan lock self-heals within
  grace + one tick (default ~6 minutes): health names the leak,
  `SystemExit(1)`, launchd restarts, flock released — with no operator
  intervention and no dashboard silence (health carries the alive-worker
  count before the exit).
- Existing abandon-restart threshold behavior unchanged; clean-tick
  health shape byte-identical to the pinned key set.
- All existing scheduler-daemon tests green (with the one pre-authorized
  fake-worker edit class named in SRR-2's brief, if needed).

## Deliberately out of scope

- **In-process lock reclamation** — a flock held by a blocked thread
  cannot be safely stolen from another thread; process death remains the
  release, and SRR-2 makes process death automatic. Recovery *without*
  restart is future work if the restart cost ever matters.
- **Provider circuit breakers** (skip remaining model calls in a tick
  after the first transport failure) — would reduce wasted budget during
  outages further; the clamp makes them an optimization, not a fix.
- **Scan-phase changes** — the scan watchdog (LOCKSTARVE-C1/sh-02) is
  already bounded and was not implicated.