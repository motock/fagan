# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a single-maintainer research project rather than a maintained product
with an SLA - see the README's "Reliability & limitations" for what that means
in practice.

## [0.4.0] - 2026-09-23

### Added

**MCP registry listings**

- Added `server.json` for the official MCP registry
  (`io.github.motock/fagan`) and a Dockerfile for Glama's build/
  introspection check, plus a Glama badge and dashboard screenshots in the
  README (#811, #812).

**Linux systemd deployment**

- Added systemd user-unit templates for the advance-scheduler daemon and
  the usage-poller, a service+timer pair, a logrotate template, a
  `scripts/generate_systemd_units.sh` generator, and README documentation
  for the Linux setup path (#822, #824, #825, #831, #838).

**Dashboard plan-ingest picker & Comms polish**

- A new backend endpoint lists plans available to ingest, backing a
  dynamic select in the ingest picker in place of free-text plan-name
  entry; the Comms panel shows a typing indicator while a reply is
  pending (#820, #821, #823, #828, #830).

**Local-dispatch observability (LD90-W0)**

- Every escalation, fallback, park and rework notice across
  `story_status`, `triage`, `escalation`, `review_orchestrator`,
  `dispatch` and `advance` now carries `event`, `story_key` and
  `correlation_id`. A `first_pass_clean`/`disqualifying_events` rollup and
  a new pure `local_success` classifier feed a `local_success_report` CLI:
  rolling first-pass-clean rate by dispatch tier, with a reason breakdown,
  read from the plan notification sidecars (#850-#858, #865).

**Reviewer & scheduler hardening (LD90-W5, MFR, SWH)**

- The reviewer now recognizes and classifies transport failures (502/503/
  504, timeouts) and defers the review like a rate limit instead of
  charging them against `review_inconclusive_count`; a diff file-inventory
  criterion was added to the review checklist. The reviewer's bash tool
  and the notification-outbox drain phase are now bounded by a hard
  timeout so a runaway command or a wedged drain can no longer hold a
  scheduler tick open. The scheduler now publishes the checkout revision
  it's actually running, and preflight reports it too (#861, #863,
  #866-#873, #906, #907, #909).

**Rework & done-bar correctness (FSU, TGE, SST, SU, HRG, ASB, RFW)**

- A red full suite gets one retry before the done-gate rejects it.
  Pre-existing baseline failures are now exempted from the tick-side
  detached grade and both agent-side full-suite gates, with the baseline's
  failing node ids recorded in the worktree for the agent to read. A
  rework round that carries an acceptance block is graded on the same
  done-bar the agent was originally held to; a rework transcript that
  already claims done is never resumed; a reviewer-feedback rework round
  can no longer be marked done on oracle-green alone. The agent scratchpad
  is untracked before grading, and the planner/dispatch prompts now state
  it must never be committed (#874-#885).

**Overlord autonomy round 2 (OA2)**

- Triage now recovers born-broken-oracle stories (`blocked_oracle`),
  resets its own attempt counter after a successful recovery, and skips
  stories with an unresolved infra-transport failure instead of ruling on
  them. Watchdog kills converge via a streak counter so repeated hangs
  escalate. `request_decision` fails open - parking the story for a human
  - when the overlord backend errors. Agent `give_up` now routes through
  the existing rebrief diagnosis path instead of notify-only. Merge-park
  re-adjudication was widened to the risk-threshold reasons the gate
  actually emits, and `PIPELINE_AUTO_TRIAGE` now ships enabled in the
  shipped launchd template (#834, #886-#893).

**Resume-rebase remote sync (SRB)**

- A resumed agent's story branch is force-pushed after the resume rebase
  so the resumed agent's own push is a fast-forward, and the seed prompt
  is now forbidden from pulling, merging, or rebasing the branch from
  origin itself (#894, #895).

**Post-LD90 priorities: file-size discipline & plan-conflict handling
(PLD90)**

- `dispatch.py`, `story_status.py` and `advance.py` were each split to
  bring their line counts under 1000, extracting `dispatch_routing`,
  `dispatch_attempt`, `dispatch_baseline`, `dispatch_worktree`,
  `detached_grade` and `advance_merge` as their own modules. A new
  plan-conflict classifier and overlord-ruling module distinguish a red
  suite caused entirely by pre-existing, branch-untouched tests from a
  real regression, routing the former to an overlord ruling instead of
  burning a rework cycle. A scope gate sends a branch with out-of-scope
  production changes back before review, naming the exact offending
  paths. `ingest_plan` gained an optional `files` field to scope and size
  stories, with an ingest-time warning when a local/cloud-open-source
  story's file scope exceeds the documented dispatch-sizing caps. The
  reviewer prompt now receives the story's brief, sibling status and
  Deferred lines (#833, #841, #896-#920).

**MCP tool surface hygiene (MCPHYG)**

- Prompted by an external tool-quality review: `check_story_status` is now
  actually registered as an MCP tool - it was fully implemented and
  documented but never decorated, making it unreachable via MCP.
  `mark_story_in_progress` and `mark_story_done` now serialize their
  manifest writes under the same plan lock every other write path uses,
  closing a race with the scheduler's tick. The duplicated
  `_VALID_STORY_STATUSES` allowlist (each copy listing `"done"` twice) is
  now a single source of truth. A new `patch_plan` tool edits plan-level
  fields (currently `role_config`) under the same locked, allowlisted
  pattern as `patch_story`. `checkpoint` is renamed to `checkpoint_story`
  for naming consistency with the rest of the tool surface, with
  `checkpoint` kept as a working alias (#922-#926).

### Changed

- **`get_role_config` removed.** It was a strict subset of
  `get_effective_config` (5 roles vs. 9, fewer fields, same underlying
  resolver) - use `get_effective_config(plan_name=None)` instead. This is
  a breaking change for any external caller of the MCP tool surface
  (#924).
- `resource_status` now uses a provider-aware default free-memory floor
  (2048MB for MLX, 512MB generic default for Ollama/LM Studio), and local
  model context sizing (`num_ctx`) is now driven by an empirically-tuned
  per-model table and sized per parallel slot, in place of a single static
  override (#818, #844-#846, #849).

### Fixed

**Merge & dispatch correctness**

- Merge now pushes from the plan's own worktree instead of the pipeline's
  `REPO_ROOT`; dirty worktree state is committed before the merge-gate
  rebase instead of retrying the same unstaged-changes failure verbatim;
  the merge gate persists its own fresh test result onto the story instead
  of leaving `last_test_check` stale; the squash-merge commit gets an
  explicit, stable title instead of trusting GitHub's single-commit
  default, and no longer repeats the story key in the PR title; two
  dispatch/decision-log bugs found while live-diagnosing a parked plan
  were fixed (#819, #829, #835, #839, #840, #879).

**Circular imports**

- Broke a `pipeline.ci` <-> `pipeline.build_detect` <-> `pipeline.server`
  circular import that failed whenever `pipeline.ci` was imported first
  (#843).

**Scratchpad tracking**

- A checkpoint commit no longer tracks `.agent_scratchpad.md`, even if an
  earlier commit already had (#836).

**Rebrief & triage prompts**

- The rework diagnosis prompt now receives the story's original brief so
  it can't invent a fix that contradicts an already pre-authorized edit;
  triage skips stories with an unresolved infra-transport failure instead
  of misclassifying them, and keeps the real exception once the
  escalation ladder is exhausted (#834, #837).

**`[no-new-tests]` anchoring**

- The opt-out token is now anchored to the start of the brief, so a
  sentence elsewhere in the brief that merely *mentions* the token can no
  longer accidentally trigger it (#833).

**Dashboard/UI polish**

- The Comms typing indicator now anchors after the thread with tighter
  panel spacing, and the stale plan-board progress bar is hidden once the
  executor signals completion (#830, #905).

**Docs accuracy**

- Fixed a stale `gpt-oss:20b` 32768 `num_ctx` claim and documented the
  `gpt-oss-20b-high` override; documented the launchd install+reload
  requirement and added a reload helper so a plist edit actually reaches
  the running daemon; fixed the "Both routes" wording in the patch review
  & apply section (#827, #846, #847).

## [0.3.0] - 2026-09-16

### Added

**Chat-driven worktree patch apply (WAP)**

- Chat can now propose and apply a patch directly against a stuck story's
  worktree: origin-gated propose/apply routes distinguishing chat from UI
  callers, unified-diff parsing with size/path/whole-file-replacement limits,
  an in-memory patch-record store with a TTL and an HMAC token bound to the
  patch id and diff hash, a plan-locked atomic apply engine restricted to
  stuck stories, and every propose/apply journaled and notified with a
  correlation id (never diff content). A dashboard UI renders the stored
  diff and requests apply with a UI-origin header, and an end-to-end test
  suite covers the full propose/review/apply path (#750, #752, #753,
  #756-#761, #766, #768-#770, #772, #773).

**Overlord parked-story autonomy**

- The overlord can now rule on parked stories under a documented decision
  matrix and autonomy-mode ladder, informed by a live git-state probe and a
  fail-closed SPLIT payload parser. It executes its own rulings directly -
  `split_story` creates child stories in the manifest, `mark_done` corrects
  the story record from live evidence, `patch_acceptance` rewrites a
  diagnosed-broken fixture through validation - and adjudicates high-risk
  merges itself in full-autonomy mode, with every executed action recorded
  in the decisions log alongside its prior state (#736-#738, #740,
  #742-#745, #747).

**Registry-authoritative provider/model routing**

- An interactive picker (`scripts/choose_providers.py`) lets an operator
  choose each role's provider from the command line, and the
  getting-started smoke now announces and runs on the actually-resolved
  dispatch provider instead of refusing everything but `claude` (#795-#800).
- `resolve_role` is now the single source of truth for dispatch and
  escalation backend resolution: registry roles outrank
  `PIPELINE_BACKEND_<ROLE>`, which becomes an empty-state fallback rather
  than an override, preflight reports the registry-aware resolution, and
  the launchd templates stopped shipping routing env vars now that the
  registry is authoritative (#801-#807).

**Dashboard & Comms chat**

- The Comms chat panel renders markdown (bold, lists, code) while keeping
  the raw transcript export (#731, #734, #735), and dashboard plan lists
  can be filtered to the active repository with an all-repos toggle,
  backed by `repo_root` on plan summaries (#732, #733). A plan-ingest panel
  was added to Comms so a plan can be ingested from the dashboard UI
  without a chat-origin route, since `ingest_plan` is unreachable from
  chat by design (#782-#784, #787, #788).

**One-line install & public-readiness**

- `scripts/remote-install.sh` (`curl -fsSL ... | bash`) clones or updates a
  fixed local checkout and runs `install.sh`; README/CLAUDE.md gained the
  community-health files and fixes needed for public visibility; and the
  launchd plist/template files and generator were renamed from
  `com.claude.pipeline.*` to `com.fagan.pipeline.*` end to end, including
  runtime label references (#779-#781, #785, #786).

### Fixed

**Merge-park & CI-evidence correctness**

- A story parked by the merge gate is no longer offered to triage as a
  candidate, an unreadable or in-flight CI status is never presented to the
  overlord as "no PR checks," a parked high-risk merge hold is
  re-adjudicated when its recorded evidence changes, CI evidence gathering
  now probes the story's real branch instead of a stale one, and high-risk
  merge adjudication records name the story instead of leaving it implicit
  (#751, #754, #755, #762-#765, #767, #771).

**Scheduler reconcile resilience**

- The scheduler process's per-call model budget is now clamped inside the
  reconcile join deadline, and the abandon-restart escape hatch is driven
  by whether the abandoned worker is actually still alive rather than a
  resettable streak counter (#739, #741, #746).

**Chat streaming**

- Fixed a duplicate reply bubble on the server's reply+result frame pair,
  stopped dropping the user's message and prior turns from the prompt
  across tool round-trips, and widened stall detection to catch
  attribute-style `[TOOL_CALL name=...]` openers (#774, #777, #778).

**Getting-started smoke hardening**

- The smoke now drives through the real `ingest_plan` path against a
  scratch repo with a real origin and a success bar scoped to
  `tests_passed`, rejects a malformed acceptance entry at ingest instead of
  crashing, resolves its announced dispatch backend through `resolve_role`,
  and its subprocess-spawning tests no longer leak the developer's real
  `.pipeline.env` into the child process (#791-#794, #808, #809).

**Local dispatch reliability**

- An in-flight local-backend story is no longer interrupted on local memory
  pressure (#790).

**Test infrastructure**

- Node frontend test suites now gate merges instead of being silently
  skipped, a workspace chat-body test was made behavioral instead of
  pinning source text, and a benchmark `MockBackend` regression was fixed
  (#775, #776, #789).

## [0.2.0] - 2026-09-12

### Added

**Streaming chat**

- Streaming chat over SSE: `POST /api/chat/stream` backed by a `stream_turn`
  event generator, with the dashboard consuming the stream for incremental
  tool activity (#685, #686, #689, #695, #707).

**Per-backend usage reporting**

- Per-backend usage reporting: `collect_backend_status` surfaces
  `resource_status` per configured role, served by the dashboard usage
  endpoint and rendered as one row per backend in the usage banner
  (#688, #693, #694).

**Plan-completion notifications**

- Plan-completion notifications: plan completion is detected exactly once and
  emitted as a `plan_completed` notification, spooled to a per-plan outbox and
  delivered over SMTP with credential handling and redaction; the scheduler
  tick drains the outbox (#710, #713, #714, #715, #716, #717, #718).

**Canonical agent.log grammar**

- Canonical agent.log line grammar: a single `agent_log_format` module defines
  the grammar, `local_agent` delegates to it, and a Claude stream-json
  translator feeds ClaudeCliDriver dispatch while raw NDJSON is kept in a
  sidecar (#687, #690, #691, #692, #699).

**Process**

- Started an ADR practice: `docs/adr/` with an index, a template and the first
  four decision records (#682).
- Introduced this CHANGELOG (#681).

### Fixed

**Plan-lock starvation**

- Merge adjudication moved to the start of the tick (#704), and the plan lock
  is now released around `dispatch_story` and `review_story`, guarded by a
  dispatch lease so a released lock cannot double-dispatch (#697, #698, #709,
  #712); per-plan per-tick dispatch is capped (#708).
- The `.agent_done` marker is claimed before its event is published and is
  written as soon as `finish_if_green` decides the run is finished (#703,
  #705).
- `reconcile_fn` gained the same bounded join watchdog `scan_fn` already has
  (#700).
- The daemon exits for a launchd restart after repeated abandoned watchdog
  workers, since a leaked plan lock is only released by process death (#706).

**Watchdog**

- The stale-activity watchdog floors the activity age at the current
  dispatch's elapsed time, so a redispatch into a reused worktree is not
  killed at elapsed 0s (#711), and records the last agent.log line and the
  branch commit count when it kills a dispatch (#702).

**Done-bar**

- The done-bar no longer narrows to a story's own new test file (cf48767),
  guarded by `tests/unit/test_done_bar_not_narrowed.py`.

## [0.1.0] - 2026-09-11

First public release.

Fagan splits software engineering along the line where model cost actually
differs: a frontier model decomposes the work, plans it, reviews the diff and
adjudicates anything risky, while a local model writes the implementation at no
marginal cost. What makes the cheap half trustworthy is inspection.

### Added

**Orchestration**

- MCP server exposing the pipeline to any MCP client (plan ingest, story
  dispatch, review, advance, merge approval, decisions).
- Web dashboard with a kanban board, per-story replay timeline, effective-config
  view and workspace picker - the pipeline can be driven entirely from the UI
  with no MCP client registered.
- Scheduler daemon that advances ready stories unattended, with per-plan locking
  and a wedged-story detector.

**Cost-tiered execution**

- Per-role provider/model routing: dispatch, review, planner, overlord,
  decompose, test_author, diagnosis, chat and security each resolve
  independently.
- Backends: `claude` (Claude Code CLI), `ollama`, `lmstudio`, `mlx` and
  `litellm`, plus an `aider` harness adapter.
- Escalation from a struggling local attempt to a stronger model, with
  spend-aware usage gating that pauses work near configured thresholds.

**Quality gates**

- TDD enforced before implementation, including an optional test-author phase
  that writes the failing suite before a weak executor touches code.
- Independent review gate returning APPROVE / REQUEST_CHANGES with bounded
  rework cycles.
- Acceptance-oracle grading against read-only fixtures materialized into the
  worktree.
- Risk-tiered overlord that stops for a human on anything irreversible,
  regardless of autonomy level.
- Merge gate that rebases onto the default branch, polls real CI and re-runs the
  suite before merging.

**Isolation**

- One git worktree per story, with runtime artifacts excluded from tracking.
- Optional Docker sandbox per worktree (ships disabled, fails closed).
- Optional remote execution over SSH.

**Operations**

- Standalone mode (`scripts/standalone-setup.sh up`) provisioning a scratch data
  dir, personas, dashboard and scheduler in one command.
- Runtime preflight reporting resolved configuration and flagging disabled
  safety gates.
- Configuration provenance showing where each setting was resolved from.
- CI on Linux (Python 3.12/3.13/3.14) and macOS.

### Known limitations

Documented rather than hidden - see the README's "Reliability & limitations"
and `retros/` for the incident record these came from:

- Local (non-Claude) dispatch degrades sharply on large or multi-concern
  stories.
- A green test suite is not proof of a correct or complete change.
- A story marked `done` is not proof its title's full scope shipped.

[0.2.0]: https://github.com/motock/fagan/releases/tag/v0.2.0
[0.1.0]: https://github.com/motock/fagan/releases/tag/v0.1.0
