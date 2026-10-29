# Plans awaiting a retro

Durable backlog for the retro process described in
`docs/plans/PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2.1. Going forward,
`mark_story_done` appends a line here automatically (with a real
completion date) whenever a pipeline-self-repo plan's last story goes
done. **Remove a plan's line once its retro is written and added to
`retros/INDEX.md`** — this file is a to-do list, not an archive.
`_record_retro_pending` writes those markers at end-of-file, so no
section added here may be the last one in the file.

**Drain policy (set 2026-09-29).** A *window* retro —
`retros/harness-review_<date>.md`, covering a stated dispatch window at
family level — discharges every plan-completion marker whose plan finished
inside that window. Per-plan retros remain the richer option when a single
plan warrants one, but they cannot keep pace with the arrival rate (43 new
markers in the week of Sep 22–29 against zero removals), and a backlog that
only grows stops being a to-do list. A window retro must state its window
and its coverage in its own body. Backfilled markers predating 2026-08-12
are discharged by family as the relevant subsystem is reviewed.

## Harness gaps with no plan yet

One-off harness defects observed during incident work that still need
either a plan or a retro write-up. Remove a line once one exists.

_None outstanding_ — the rescoped-commit-message gap (OA2-05) was addressed by the warn-only `pr_title_mismatch` notification (PR #1123). The scheduler-heartbeat wall-clock flake was fixed by plan `scheduler-heartbeat-flake-bound`.

## Abandoned plans

Plans stopped before completion. `mark_story_done` never fires for
these, so no marker is written automatically. Remove a line once its
retro is written.

_None outstanding_ — `bench-test-author` was discharged by
`retros/harness-review_2026-09-29.md`.

## Plan completion markers

The entries below are a one-time backfill (2026-08-12) of every
pipeline-repo plan that was already fully done before the automated
marker existed. Their true completion dates predate this file and were
not individually reconstructed — check `git log` on the relevant PR/files
if a precise date is needed when writing one of these up.

- failure-mode-catalog-sync — completed 2026-10-18, 1 story
- python-311-floor-ci-and-ruff — completed 2026-10-18, 1 story
- pr-title-diff-mismatch-warning — completed 2026-10-18, 1 story
- harness-instrument-fixes — completed 2026-10-26, 2 stories
- retro-p0-fixes — completed 2026-10-26, 5 stories
- actor-identity — completed 2026-10-26, 5 stories
- request-correlation — completed 2026-10-26, 3 stories
- structured-logs — completed 2026-10-26, 4 stories
- dispatch-stale-base — completed 2026-10-27, 1 story
- policy-enforcement — completed 2026-10-27, 6 stories
- stale-base-probe — completed 2026-10-27, 1 story
- audit-stream — completed 2026-10-27, 6 stories
- stale-probe-robustness — completed 2026-10-27, 1 story
- oidc-identity — completed 2026-10-27, 4 stories
- remote-mcp — completed 2026-10-27, 3 stories
- post-phase6-hardening — completed 2026-10-27, 3 stories
- ownership-and-ingest-followups — completed 2026-10-27, 2 stories
- remote-mcp-hardening — completed 2026-10-28, 2 stories
- config-write-guards — completed 2026-10-28, 2 stories
- one-repo-server-remainder — completed 2026-10-28, 8 stories
- dashboard-refusals-followups — completed 2026-10-28, 3 stories
- audit-anchor — completed 2026-10-28, 2 stories
- eventing-broker-prep — completed 2026-10-28, 2 stories
- remote-mcp-hardening-2 — completed 2026-10-28, 2 stories
- service-py-headroom — completed 2026-10-28, 1 story
- enterprise-enforcement-closeout — completed 2026-10-28, 4 stories
- dashboard-oidc-login — completed 2026-10-28, 4 stories
- observability-otel — completed 2026-10-28, 5 stories
- flake-and-followup-fixes — completed 2026-10-29, 4 stories
