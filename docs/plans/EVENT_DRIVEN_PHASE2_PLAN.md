# Event-driven pipeline — Phase 2 (wiring) — DRAFT

Status: **draft, not ingested.** Phase 1 is the ingested `event-driven-pipeline`
plan (S1–S9, currently paused, all `todo`). Phase 1 ships the parts; nothing in
it changes runtime behavior except S4+S5 (the non-blocking CI poll). This
document drafts the stories that actually turn the event loop on.

---

## The design decision Phase 2 has to make first

Phase 1's `EVENT_TYPES` lists eleven event types (`story_ready`,
`agent_dispatched`, `tests_passed`, `pr_open`, `ci_complete`, …), which implies
a handler-per-stage architecture: each event type gets a subscriber that
performs that stage of the pipeline. **Do not build that.** Every stage's logic
already lives in `_advance_pipeline_locked`; a handler per stage duplicates all
of it into a second code path, and the two paths drift the moment either is
touched. That is a rewrite disguised as a wiring task.

Build the cheap version instead:

> **Events wake the tick; they do not replace it.**

An `agent_done` event triggers an immediate `advance_pipeline(plan)` for that
one plan instead of waiting up to 60s for the next sweep. The stage logic is
untouched and stays single-sourced. The reconcile timer remains the backstop
for anything that never produced an event. This captures essentially all of the
latency win at a fraction of the risk, and it leaves the handler-per-stage door
open if a later phase ever justifies it.

Consequences for Phase 1 as ingested:

- **S3's guards are still needed**, but their job narrows: they gate the *wake*
  (don't wake a plan for a story whose status no longer matches the event), not
  a family of counter mutations. Under the wake design an at-least-once
  redelivery costs one redundant `advance_pipeline` call, which is already
  status-guarded, rather than a double-increment.
- **S2 (`JsonlEventBus`) has no named producer.** The done-marker files *are*
  the out-of-process transport; the daemon is in-process with its handlers.
  Unless a cross-process producer is planned (a git hook, a CI webhook receiver,
  the dashboard writing events), S2 is speculative and should be dropped or
  deferred until something needs it. Flagging rather than deleting — it's a
  cheap, self-contained story either way.

---

## Stories

Ordering respects the local-dispatch rules: ≤2 production files each, one
concern each, anchored edits, rename-and-delegate over in-place re-indent.

### P2-1 — Add `pipeline/event_wiring.py` with the guarded wake handler

*Depends on: S1, S3. New file, 1 production file.*

Composition root for the event side. Provides:

- `wake_handler(event) -> dict` — reads the plan's manifest, calls
  `check_precondition(manifest, event['story_key'], event['type'])` from
  `pipeline.event_guards`, and on `ok` calls `advance_pipeline(event['plan'])`.
  On a failed precondition it returns the skip record and does nothing — a
  redelivered or stale event is a normal, silent no-op.
- `build_bus() -> EventBus` — returns an `InProcessEventBus` with
  `wake_handler` subscribed to `agent_done`.

Rules: import `advance_pipeline` lazily *inside* the function (module-level
import of `pipeline.server` is circular). Never raise into the bus — the bus
already logs and continues, but the handler should return structured outcomes
rather than exceptions. No file writes beyond what `advance_pipeline` does.

Tests: precondition-met wakes exactly once; precondition-not-met does not call
`advance_pipeline` at all; unknown story key is a silent skip; a raising
`advance_pipeline` is logged and does not propagate; `build_bus` returns a bus
with exactly one `agent_done` subscriber.

### P2-2 — Add `scan_all_plans` to `pipeline/watchers.py`

*Depends on: S7. Modifies 1 production file.*

`scan_done_markers` (S7) is per-plan and takes a manifest the caller already
read. Production needs the sweep: `scan_all_plans(bus) -> list[dict]` iterates
`PLAN_DIR`'s `*.manifest.json`, skips paused plans, reads each manifest, and
calls `scan_done_markers(manifest, plan_name, bus)` for each.

Rules: an unreadable/malformed manifest is logged at WARNING and skipped, never
fatal — one bad plan must not stop the sweep. Still no test running, no manifest
mutation, no `subprocess`. Anchored `str_replace`; do not touch
`scan_done_markers` itself.

Tests: two plans each with a marker publish two events; a paused plan is
skipped; a malformed manifest is skipped and the other plan still scans; an
empty `PLAN_DIR` returns `[]`; the sweep never invokes `subprocess`.

### P2-3 — Give `SchedulerDaemon` a production entrypoint

*Depends on: S8, S9, P2-1, P2-2. Modifies 1 production file.*

Add `def run_daemon() -> int:` plus an `if __name__ == "__main__":` block to
`pipeline/scheduler_daemon.py` that composes the real collaborators:
`bus = build_bus()`, `scan_fn = lambda: scan_all_plans(bus)`,
`reconcile_fn = advance_all_plans` (lazy import), `interval_s` from
`PIPELINE_SCHEDULER_INTERVAL_S` (default 60), `health_path` from
`PIPELINE_SCHEDULER_HEALTH_PATH` (default none), then `run_forever`.

Add a single-instance guard: an flock on a pidfile, so a second daemon exits
non-zero immediately rather than running a second clock. Reuse the existing
flock idiom in `pipeline/concurrency.py` rather than inventing one.

Tests: `run_daemon` wires `advance_all_plans` as the reconcile fn (assert via
patching, do not run it); a second `run_daemon` while the lock is held exits
non-zero without calling `reconcile_fn`; the interval and health path are read
from env with the documented defaults; `__main__` is not executed on import.

### P2-4 — Cut the launchd agent over from a 60s tick to the daemon

*Depends on: P2-3. Modifies the plist template + generated plist; needs a doc update.*

`launchd/com.claude.pipeline.advance-scheduler.plist.template` currently runs
`python3 -c 'import app.pipeline_mcp_server as p; p.advance_all_plans()'` with
`StartInterval: 60`. Change `ProgramArguments` to run the daemon module and
replace `StartInterval` with `KeepAlive: true` — launchd's job becomes
crash-restart only, never cadence. Keep `RunAtLoad`, the environment block, and
both log paths exactly as they are.

Two things this story must state out loud:

1. **Only one clock may run.** With `KeepAlive` and no `StartInterval`, launchd
   restarts the daemon but never fires a second sweep. The single-instance lock
   from P2-3 is the belt to this braces.
2. **The installed plist in `~/Library/LaunchAgents/` is a separate copy.** This
   story changes the repo's template and generated file only; reloading the
   agent on the host is a manual step and belongs in the story's completion
   notes, not in the diff.

Doc update required (`README.md` or `REFERENCE.md`): the scheduler is now a
long-lived process, plus the two new env vars from P2-3.

Note for whoever writes an acceptance fixture here: a fixture that
`plistlib.load`s these files breaks on `--` inside XML comments, and
"regenerate reproduces the committed file" cannot hold when run inside a
worktree. Prefer asserting on the template's text.

### P2-5 — Skip the rebase/force-push while CI is pending

*Depends on: S5. Modifies 1 production file (`pipeline/server.py`).*

The optimization deliberately deferred out of S5. Today the merge phase rebases
and force-pushes unconditionally before polling CI. Once the poll is
non-blocking, every tick re-rebases; whenever the default branch has moved that
mints a new SHA, force-pushes, and restarts CI, so a pending story can churn
Actions minutes and converge slowly. S5's `_ci_pending_expired` bound already
makes this terminate, so this is a cost/latency fix, not a correctness one —
which is exactly why it is its own story.

Record `ci_pending_sha` alongside `ci_pending_since`, and while a story is
pending, poll that recorded SHA instead of rebasing and pushing again. Clear
both fields together on any resolved state.

This one needs care: the natural shape wraps the existing rebase/push block in a
conditional, which is an in-place re-indent of ~45 lines inside a ~470-line
function — the single most reliable way to break a weak local executor. Extract
the block into a helper (`_rebase_and_push_for_merge(...) -> tuple[str, str]`
returning `(gate_error, pushed_sha)`) and call it conditionally, rather than
indenting it in place. **Do not dispatch this one to a local model without that
extraction spelled out.**

---

## Deliberately not in Phase 2

- **Handler-per-stage.** See the design note above.
- **Publishing the other ten event types.** `agent_done` is the only one with a
  producer (S6's marker) and a consumer (P2-1's wake). Adding publishers for
  event types nothing subscribes to just re-creates the Phase 1 dead-code
  problem one layer up. Add each type when a subscriber needs it.
- **Removing the reconcile sweep.** It is the only evaluator of the dispatch
  watchdog and the only recovery path for a lost event. Permanent.

---

## Phase 3 — notification interface (separate plan)

Sketched here so the sequencing is visible; it should be its own plan, and its
first half does not depend on the event bus at all.

**3a — structured notification records (independent of Phase 1/2).** Today
`_notify_user(plan, message)` appends a free-text line to
`<plan>.notifications.log` (`pipeline/persistence.py`), and the dashboard tails
it as raw strings (`app/dashboard.py`). No severity, no story key, no event
type, no dedup key — which is why the dashboard cannot filter or badge, and why
nine identical warnings appear in a row. Extend to
`_notify_user(plan, message, *, story_key=None, severity="info", event=None,
dedup_key=None)` and write a JSONL record beside the existing log. **Keep the
positional signature working:** there are ~49 call sites in `pipeline/server.py`
alone and the helper is the most heavily patched symbol in the test suite (~50
tests), so a breaking signature change is a very large blast radius. Migrate
call sites incrementally afterwards.

**3b — sinks over the bus.** Notification delivery becomes a subscriber set on
the Phase 1 bus, not a second bus. File-log sink is the default and reproduces
today's behavior exactly; dashboard sink next; anything outbound after that.
Three constraints to bake in from the start:

1. A sink failure must never break the tick — same swallow-and-log-at-ERROR rule
   the `InProcessEventBus` already specifies for handlers.
2. **Outbound sinks must not do network I/O inline.** A webhook POST inside
   `_notify_user` reintroduces exactly the blocking-the-sequential-tick problem
   S4/S5 exists to fix. Queue them, or run them in the daemon.
3. **Redact at the sink boundary; outbound sinks off by default.** Notification
   text today embeds gate errors, branch names, worktree paths and raw CI
   stderr. The moment a sink leaves the machine, all of that is published.

First genuinely new signal to carry: a story sitting in `summary['ci_pending']`
past S5's bound. Today that state produces no notification at all.
