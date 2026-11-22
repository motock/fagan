# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a single-maintainer research project rather than a maintained product
with an SLA - see the README's "Reliability & limitations" for what that means
in practice.

## [Unreleased]

### Added

- A plan whose ready stories are held back by the dispatch gate now says so: the advance tick records `dispatch_gated` in its summary and notifies once per distinct reason (`dispatch is waiting: <reason>`). The reason is sanitised first: one line, at most 200 characters, URL credentials and query strings removed.

- The deployment-only `host_tests_skipped` lock: when the deployment layer sets it to `true`, a story that resolved `scoped` (CI gates its merge) runs no test command on the pipeline host, and the pre-dispatch baseline is skipped. A story degraded to `full` still runs its tests.

- A `role_routing` value may name an undeclared Ollama cloud model. On a remote Ollama endpoint a plain name is accepted, and a `:cloud` tag is accepted on any endpoint; the settings value check now applies the same rule as role resolution, so such a value can be saved (it was refused with `invalid_value` before).

- `PIPELINE_INTAKE_PLUGINS` lists operator-supplied intake callables that the scheduler runs once per tick, after Jira intake, to create plans from the operator's own sources. It needs `PIPELINE_PLUGIN_MODULES` and does nothing while plan approval is off.

- The overlord prepares park options for a parked story. With failure triage on (`PIPELINE_AUTO_TRIAGE`), each scheduler tick prepares options for at most one parked story that has none for its current `parked_reason`: `park_option_count` options (2 to 5, default 3, settable on the org, team and team/repo layers) drawn from the park class's menu plus `retry`, one recommended. Nothing runs until a person chooses. `GET /api/plans/{plan}/stories/{story}/park-options` lists them and `POST /api/plans/{plan}/stories/{story}/park-options/{option_id}/choose` carries the choice out: `retry` clears the retry counters and sets the story `interrupted`, and any other action runs through the triage executor under the plan lock. A stale or unknown choice is refused with a 409 and an error of `not_parked`, `no_options`, `options_stale` or `unknown_option`, and nothing changes.

- Dashboard chat explains a parked story's options and can propose `choose_park_option`. The chat never runs it: the Run button calls the park-option choose route as the signed-in person.

- Jira Cloud intake. With `PIPELINE_JIRA_ENABLED=1` and `PIPELINE_PLAN_APPROVAL` on, the scheduler plans each Jira story assigned to the service account as the plan `jira-<issue key>`, with the team and the board's `repo_url`. Only sites listed in `PIPELINE_JIRA_SITES` are contacted. Each team's `jira_site` (org layer: site, service account and a `secret://` token reference) and `jira_board` (team layer: project key, repo URL, status names and poll interval) settings describe the connection. A planned story gets the label `fagan-planned`; a failure gets `fagan-intake-failed`, and removing that label retries it. The resulting plan still needs a person's approval before anything runs.

- With `PIPELINE_JIRA_ENABLED=1`, each story of a `jira-<issue key>` plan becomes a Jira sub-task labelled `fagan-story-<story key>`. Only the story's summary and description are copied; its brief, files and acceptance fixtures stay in Fagan. The sub-task's status follows the story (in progress when dispatched, in review when its PR opens, done when the story is done), and the parent moves to parent review once every story is done.

- A `webhook` notification channel POSTs each notice as JSON to your own automation, signed with HMAC-SHA256 in `X-Fagan-Signature` over `X-Fagan-Timestamp` and the body. Its `url_ref` and `secret_ref` are credential references (both required) and `timeout` (1 to 30 seconds, default 10) is optional. The URL must be `https` to a public host, and redirects are not followed. The verification example is in REFERENCE.md under "Notification routing".

- A deliver entry may name an `audience` (`plan_owner`, `plan_approvers`) instead of, or as well as, `to`, and an instance may carry a `principal` naming the person who owns it. With both, only the `to` instances whose principal is in the audience receive the notice. `PIPELINE_AUDIENCE_PLUGIN` supplies the members of an audience; without it `plan_owner` is the plan's owner and `plan_approvers` is empty.

- Story-level notifications (`story_parked`, `dispatch_failed`, `tests_failed`, `agent_gave_up`, `story_stalled`, `merge_conflict_rework`) carry a direct link. Slack and e-mail append it as a final `Open:` line, built from `PIPELINE_DASHBOARD_PUBLIC_URL` and the new `PIPELINE_STORY_LINK_PATH` (default `/`). The line is omitted when the origin is unset or either value is invalid.

- An `openai` provider calls OpenAI's Responses API at `https://api.openai.com`, a host that cannot be configured. Its key is the per-namespace credential `openai_api_key`, read on every call; `OPENAI_API_KEY` is the fallback for the default namespace only. List its models under `providers.openai.models` in `model_registry.json`.

- The authority matrix's `readiness.min_paired` and `readiness.max_false_park_percent` rules let an organization tighten the bar a class must clear before its `PIPELINE_TRIAGE_MODE` promotion. They can only tighten; the shipped 20 paired decisions and 10% false-park rate are the loosest values allowed. Each verdict in the readiness report names the thresholds and tier it was judged against.

- `python -m pipeline.overlord_replay` asks the overlord again about recorded rulings, optionally with a candidate `--policy FILE` or `--context DIR`, and scores the new answers against the human resolutions. It is read-only and never executes a ruling.

- `python -m pipeline.overlord_scenarios` asks the overlord a shipped set of known-answer hard cases and reports which pass. `--scenarios DIR` replaces that set. It is read-only, but it does call the overlord.

- `python -m pipeline.rehearsal_report --plan <plan> --repo <mirror checkout>` pairs each story's rehearsal merge with the reference merge of a plan that already finished and lists the files only one side touched. The runbook is `docs/guides/OVERLORD_REHEARSAL.md`.

- A new `own_lint_failure` park class covers a story whose suite passes but whose lint gate is red only on files the story added or declared. Its allowed actions are `rebrief` and `park_for_human`; a lint failure in any other file keeps the `unknown` class.

- A new `review_inconclusive` park class covers a story whose work is pushed and green but whose review never reached a verdict (`review inconclusive after <n> attempts`). Its allowed actions are `resume_review` and `park_for_human`; a reason without a readable attempt count keeps the `unknown` class. The class is not governed by the authority matrix until an operator governs it.

- An identity provider loaded through `PIPELINE_IDENTITY_PLUGIN` can serve the remote MCP transport only when its class sets `network_grade = True` (exactly `True`). Any other provider makes the transport refuse to start, and the dashboard browser login stays OIDC-only. The plugin requires `PIPELINE_PLUGIN_MODULES`.

### Security

- Email delivery to a host that resolves to a loopback, private, link-local or other non-public address is now refused. An install that sends through an internal relay (for example a postfix container) must list that host in `PIPELINE_NOTIFY_SMTP_ALLOW_PRIVATE_HOSTS` or its email stops after upgrading.

- A process serving several namespaces gives each non-default namespace its own worktree directory (`WORKTREE_ROOT/_ns/<org>@<team>/<key>`), so two teams' stories with the same key no longer share a worktree on dispatch; the default namespace keeps `WORKTREE_ROOT/<key>` and refuses the key `_ns` (hrc-01).
- The security reviewer is told the findings format the review gate parses (`- Blocking: <path>: <description>` or `- Blocking: (unscoped) <description>`), so a REQUEST_CHANGES no longer parks the story as inconclusive for using markdown headings instead (srf-01).

### Changed

- A `role_routing` setting may route a role to the `ollama` provider with a cloud model the registry does not declare under `providers.ollama.models`. A plain name such as `kimi-k3` is accepted when the Ollama endpoint is remote, and a `:cloud` tag such as `kimi-k3:cloud` is accepted on any endpoint; local models are still refused. Other undeclared names keep failing with `RoleRegistryError`.

- With `PIPELINE_CHECKPOINT_MODE=file`, a dispatched agent records a checkpoint by committing its work and dropping `.agent_checkpoint` in its worktree (the prompt asks for that file instead of the checkpoint tool), and the local agent's checkpoint tool does the same without store access; with the mode unset the prompt and tool call are unchanged (hrc-04).

- Documents plan advancement modes (`PIPELINE_ADVANCE_WORKERS`, `PIPELINE_ADVANCE_MODE`, `PIPELINE_ADVANCE_STALE_SECONDS`) in REFERENCE.md, and adds a regression test showing that two namespaces holding a plan with the same name (`demo`) are each advanced exactly once through the queue sweep and the worker pool, with the child request carrying its own namespace and a request for a namespace that does not hold the plan refused (nq-06).

- With `PIPELINE_ADVANCE_WORKERS` above 1, the scheduler advances each plan in a non-default namespace in a child process bound to that namespace (the same pool the default namespace uses), a child timeout or failure is recorded against that plan only, and the namespace walk starts at a different namespace on each pass; a deployment whose only namespace is the default, or that leaves the worker pool off, sees no change (nq-05).

- The queue-mode advance sweep (`PIPELINE_ADVANCE_MODE=queue`) requests stale plans in every in-scope namespace, not only the default: each other namespace is listed once through the store's record view and its requests carry the namespace, the same plan name may be queued once per namespace, and `PIPELINE_SCHEDULER_NAMESPACES_ONLY` / `_SKIP` apply as they do to the scheduler tick (an invalid scope refuses the pass). A deployment whose only namespace is the default sees the same sweep result, events and logs (nq-04).

- Scheduler events and wake markers carry their namespace: `make_event` takes an optional `namespace` stamped as a top-level key (absent when not given), wake markers are `<org>@<team>@<plan>` outside the default namespace and `<plan>` inside it, and each tick scans the default namespace then every other in-scope namespace under its own binding; a deployment with only the default namespace sees the same events, marker names and logs (nq-03).

- Advance workers and the wake hook carry a plan's namespace: the worker request line adds `"namespace"` only for a named namespace, `wake_handler` and `reconcile_handler` pass `namespace=` to the wake hook and decide the precondition from that namespace's manifest, and a non-string namespace on an event is refused as `namespace_malformed`; a deployment with only the default namespace sees no change (nq-02).

- The advance queue keys every entry by `(namespace, plan)` and rotates fairly between namespaces, so a plan name shared by two namespaces queues and coalesces independently and one busy namespace cannot starve the others; the default namespace stays `None` and existing `take()` callers see no change (nq-01).

- A plan's settings team comes from its tenancy namespace through the optional `PIPELINE_SETTINGS_TEAM_PLUGIN` seam (`team_for_namespace(namespace) -> str | None`), not the free-text manifest `team` label, so a plan labelled `local` in the `default` namespace reads its team's test scope, autonomy and merge gates (stn-01). Unset, the team is the manifest `team` as before.

- The plan completion summary counts the story whose merge completes the plan: `notify_if_plan_completed` takes `just_merged`, which the three merge paths pass, so a `story_merged` row the event bus has not persisted yet no longer reads as `stories_merged=0` (fcr-01).
- `approve_merge` now records a `story_merged` notification when it merges a story, as `advance_merge` already did, so plan completion summaries and the console's recent activity count every merged story.
- Signing out of the dashboard in a browser (`GET /auth/logout`) also ends the identity-provider session, redirecting to the provider's end-session endpoint with `client_id` and the site root as `post_logout_redirect_uri` (no ID token in the URL); the standalone realm allows that redirect, and a realm imported earlier needs it added by hand (see `docs/DEPLOY_STANDALONE.md`).
- Workspace path containment and clone-URL credential redaction were hardened so CodeQL recognises them: the containment check compares normalized, realpath-resolved strings, and credential redaction is a linear-time scan with no length limit on the userinfo, so a long `https://` run no longer backtracks.

- Dashboard chat's file, directory and search tools read the repository the turn names (its managed clone) instead of the process-wide active workspace the console cannot set, so "summarize this code base" works. The `GET /api/workspace/file|files|search` routes take an optional `repo_url`; without it they behave as before.

- Dashboard chat proposes every state-changing action (ingest, approve, dispatch, interrupt, patch, review, done, checkpoint, advance, pause, resume, answer decision) and never runs one itself: the person presses Run, and the console calls the existing route with their own credentials. `approve_merge` now refuses the chat origin.

- Dashboard chat tells the planner which plan is selected, so "this plan" means the plan the person chose.

- Under `PIPELINE_AUTONOMY=full`, a high-risk merge the overlord rules `proceed` is now held for a human unless the plan's `triage_mode` is `execute`. The ruling is still recorded in the decisions log.

- Under OIDC, dashboard chat tool calls (`/api/chat`, `/api/chat/stream`) now act as the signed-in caller instead of failing with 401 Unauthorized.

- The dashboard story log now shows the agent log that dispatch records in the story worktree, instead of "No agent log yet" for every dispatched story. An absolute log path is served only when it resolves under the worktree root; anything else still reads as unavailable.

- Approving a held merge now waits for a busy plan the way other operator commands do (`PIPELINE_COMMAND_LOCK_WAIT_SECONDS`), and answers 409 instead of 404 when the plan stays busy, so a retriable refusal no longer looks like a missing story.

- The plan detail (`GET /api/plans/{plan}`) now reports `archived` for an archived plan, matching the plan list.

- Dashboard chat and `/api/decompose` accept `repo_url`: the planner reads that repository and a plan the chat saves targets it, so a signed-in author can draft and stage a plan for a repository.

- Ingest can read organization-level context sources from an operator-selected provider (`PIPELINE_ORG_CONTEXT_PLUGIN`) in addition to `PIPELINE_CONTEXT_CONFIG`; a provider failure refuses the ingest.

- `scripts/dashboard.sh` honours `PIPELINE_PYTHON` as the interpreter it runs the dashboard with, ahead of `.venv` and PATH, and refuses an unusable value instead of falling back; the dashboard script tests now pass in a git worktree with no `.venv`.

- Adds the deployment-only `tenant_test_policy_locked` setting: when the deployment layer sets it to `true`, its `test_scope` and `reverify_full_suite` are final for every tenant, so a team, plan, story or env request cannot loosen them (hrc-02).

- With `PIPELINE_CHECKPOINT_MODE=file`, the control plane journals a checkpoint an agent drops as `.agent_checkpoint` in its worktree (size-capped, never followed, never committed), so a sandboxed agent without store credentials can checkpoint (hrc-03).

- The acceptance-fixture pytest dry-run collects from a `.pipeline-fixtures-*` directory inside the story's repo when a repo root is set, so a sandboxed command that sees only the worktree can run it; `_commit_wip` never commits such a directory left by a killed run (hrc-05).

- `PIPELINE_TRIAGE_MODE` no longer promotes every overlord decision class at once. Each class follows its tier in the authority matrix (`PIPELINE_AUTHORITY_MATRIX`): `human` stays in shadow, `auto+notify` executes and notifies through the sweep summary, and `auto` executes. Any `PIPELINE_TRIAGE_MODE` other than `execute` still holds everything. The high-risk merge gate is always governed: a `proceed` ruling merges only when the matrix's `high_risk_merge` tier is `auto+notify` or `auto`, and the shipped matrix keeps it at `human`, so deployments in shadow see no change.

- Under full autonomy, the merge gate runs the repository's tests, build and lint on the branch head before the overlord rules on a high-risk merge, and shows the result to the overlord as a `LOCAL CHECKS:` line. Red checks park the story with `high risk held: local checks red @ <sha>` without a ruling. The story stays `pr_open` while the detached job runs.

- The overlord's high-risk merge prompt carries a `DIFF EVIDENCE` block: the diff stat, the declared files, changed files outside them, existing tests modified or deleted, and a redacted production diff excerpt of up to 12000 characters. If git cannot produce it the block reads `DIFF EVIDENCE: unavailable`, which is missing evidence and never a pass.

- The overlord sees the context sources recorded at dispatch for the `triage` and `merge_adjudication` phases, rendered after its policy. Context guides the ruling and cannot grant an action; the digests of what was rendered are recorded with the ruling.

- Every failure-triage and high-risk merge ruling records an `input` object: the redacted question (capped at 16000 characters, with a `truncated` flag), digests of the prompt and policy, the context digests, the overlord backend and model, and the park class. The raw prompt is not stored, and the plan-detail response omits `input`. Earlier records have no `input` and are read unchanged.

- The park-option readiness report counts the choices a person made per park class, as `park_option_choices` and `park_option_agreements`, beside each class's verdict. Choices never enter the paired counts or the false-park rates.

- The readiness report counts a `park_for_human` ruling that a person answered with input as agreement, not a false park. Changing the brief (`rebrief`), widening the scope (`amend_scope`), approving a test edit (`approve_test_edit`), adding files, or an unblock with a note now agrees and is tallied as `park_needed_input` beside each class's verdict. A false park is now only a park the person answered with nothing supplied (`retry`, `resume_review` or `other` with no input). The unblock with a non-blank `note` records `brief_changed` on its resolution; the note's text is not recorded.

- `unblock_story` says what the review will do with the story it leaves at `tests_passed`: a kept `last_reviewed_sha` means no re-review until the branch moves.

- The overlord no longer rules on a high-risk merge while its PR checks are still pending. The merge stays held with no ruling recorded, and the hold is re-adjudicated once CI finishes; a cached pending CI snapshot is read again rather than trusted.

### Fixed

- A failed story's `failure_reason` names why its latest attempt failed. A red test run reads `tests failed (exit <code>)` (or the not-found text for exit 127), a passing grade clears a reason left by an earlier attempt, and an empty branch whose agent could not authenticate to its model provider says so.

- The scheduler's single-instance lock records the daemon's identity (`<pid> <start_ticks>` from `/proc`), so a lock file left by a previous container whose pid now belongs to an unrelated process is taken over instead of blocking the scheduler from starting. A takeover logs one INFO record, `scheduler lock taken over`, with the recorded pid and a reason.

- `GET /api/usage` reports each role's provider and model from the team's `role_routing` setting. The per-role backend rows resolved from the model registry file only, so a deployment that keeps its routing in settings showed every role with an empty provider and model.

- A team layer's `notification_routes` was stored but never used. Notices now deliver through the team's routes in every namespace, and an instance name defined with different settings by two layers is skipped and logged by namespace and name.

- Email, Slack and webhook channels look up their credentials in the namespace of the notice being sent instead of `default`, so a credentials plugin can serve each team's secrets.

- The merge gate applies the plan's stored settings when it is called without a plan argument, which is how the merge path calls it. Under full autonomy a high-risk merge had been parked as `high risk held for human review` without reaching the overlord, and the stored autonomy and auto-merge values were ignored.

- The CI rework message names the failing checks and tests. A CI failure answered from the rollup cache kept no check names, and the failing-log lookup ran outside the story repository and read the newest workflow run rather than the failing job, so the message said only `ci fail:`. It now names each failing check, and the failing tests from the failing job's own log.

- Deployment-layer settings now apply to teams in every namespace. Before, a team outside the default namespace never saw them, so deployment locks and ceilings (for example `tenant_test_policy_locked`, `test_scope`, `merge_ci_gate`) were not applied to it. Operators of a multi-namespace deployment should review their deployment settings, because they take effect for all teams on upgrade.

- A team's own routing and merge settings now apply to that team's plans. A plan in a team namespace read its settings in the default namespace, so the team's `role_routing` and merge gates were ignored and its stories ran on the deployment default. Plans in the default namespace are unchanged.

- A review that would certainly skip is no longer respawned on every tick. Once a commit's unchanged-since-review skip has been announced, the advance starts no `review_job` for it and lists the story under `skipped`, instead of spawning a process that skipped again each minute. `unblock_story` returns `review_hint: "unchanged_since_last_review"` when it sends a story to `tests_passed` and keeps its `last_reviewed_sha`, so the operator sees that no review will run.

### Security

- Lint and suite baseline probes, the acceptance-oracle gate, the failing-test re-run, pytest collection for the guard-liveness check and the local reviewer's `bash` tool run their commands through the command-wrap seam (`tenant_exec.wrap`), so a configured wrap plugin sees every one of them; a refused command makes its probe report `*_probe_failed` (detail `CommandWrapError`), the oracle gate report `errors`, the re-run return no facts, the collection return no data, and the reviewer shell return exit code 126 without running the command. With no plugin configured, nothing changes (tcw-05).
- Dashboard: `/api/chat/stream` no longer accepts a `k-`-prefixed `X-Pipeline-Api-Key` in place of the shared secret; the unvalidated pass-through let an unauthenticated caller invoke the chat model.
- Review: the local-model reviewer's `bash` tool now runs without the pipeline's forge, notification and provider credentials in its environment, and its `view_file` tool refuses paths outside the story worktree.
- Dashboard: under OIDC the index page no longer embeds the shared-secret API key.
- Container: the entrypoint refuses `PIPELINE_IDENTITY_PROVIDER=shared-secret` on a non-loopback `DASHBOARD_HOST` unless the UI is off.
- Dashboard: every response now carries `Content-Security-Policy: frame-ancestors 'none'` (plus `base-uri`, `object-src`, `form-action`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`; the dashboard can no longer be framed.
- Worktree patches: the apply-side deny list (the git and Claude config directories, the agent instruction file, the MCP config, agent logs) now matches case-insensitively.
- Agent harness and review spawns pass their argv through the optional `PIPELINE_COMMAND_WRAP_PLUGIN` seam (`wrap(argv, cwd, kind, namespace) -> list[str]`), which fails closed: a plugin failure or malformed answer refuses the command, and it cannot be combined with ssh execution or `PIPELINE_SANDBOX=docker` (tcw-01; see ADR 0016).

- Grading and merge re-check commands (the acceptance and build re-verify, the check-story-status test run, and the detached grader's argv) also pass through the `PIPELINE_COMMAND_WRAP_PLUGIN` seam, so an operator's wrap covers every repository test and build run, not only agent and review spawns (tcw-02).
- Baseline snapshots, reviewer self-fix re-runs, triage suite-state checks, worktree venv setup, lint gates, acceptance-fixture collection and Claude CLI completions that run in a working directory also pass through the `PIPELINE_COMMAND_WRAP_PLUGIN` seam (tcw-03).

## [0.10.0] - 2026-11-17

### Added

- Settings: the `autonomy`, `auto_merge` and `triage_mode` tenant settings now resolve per plan (namespace, team, repo) in the merge gate, the advance tick and overlord triage, and are documented in REFERENCE.md; their environment variables (`PIPELINE_AUTONOMY`, `PIPELINE_AUTO_MERGE`, `PIPELINE_TRIAGE_MODE`) remain the deployment default, so nothing changes until a value is set in the settings Store.
- `PIPELINE_TENANT_DATA_EXEMPT_PATHS` (default unset) lists dashboard-extension paths under `/api/ext/` that authenticate but skip namespace binding.
- Remote settings calls are authorised per layer through the authorizer seam. `set_setting` needs `config:write` and `get_settings`/`list_settings` need `config:read`, each on a resource naming the layer's scope id and the repo, and an agent principal is still refused before the authorizer is asked. The `deployment` layer stays refused to a remote caller whatever the authorizer allows. With no authorizer plugin (`LocalPolicy`) every remote settings call is refused, as before.
- Added the settings record kind (ADR 0015) to the SQLite store and the plugin wrapper. A `settings` table (migration 6) holds one versioned record per (namespace, scope, scope_id, repo), written compare-and-set on version inside the write transaction. **Contract change for plugins:** `get_settings`, `put_settings` and `list_settings` are now in `RECORD_STORE_METHODS` and so in `PLUGIN_STORE_METHODS`; a plugin built before this change is refused at startup until it implements them. The record-store conformance suite gains settings cases that every store runs.
- Documented the published store plugin surface (ADR 0012 decision 7): `docs/guides/STORE_PLUGINS.md` now names `PLUGIN_STORE_METHODS` (the record methods, the outbox methods and `audit_log()`), the loader's `PluginStore` wrapper, and the outbox, audit and transaction conformance suites with their fixtures (`audit_rewrite_raw`, `record_store_transaction_tier`, `record_store_serves_other_namespaces`); `REFERENCE.md`'s `PIPELINE_STORE=plugin` row is corrected to match, and ADR 0012 records the amendment and that the Postgres implementation lives in the enterprise distribution.
- Added append-only usage records to the record store contract (ADR 0012 decision 3). Every `RecordStore` now offers exactly `append_usage(namespace, plan_name, record)` and `get_usage(namespace, plan_name)` — no update and no delete, so an accounting line is never rewritten — with `USAGE_RECORD_FIELDS` in `pipeline/store_records.py` as the single declaration of the telemetry contract's field set, which `pipeline.usage_record.USAGE_FIELDS` now aliases. A record is validated before anything is written: its keys must be exactly the contract's fields and every value must be one that field may hold (`InvalidUsageRecordError`, whose message names the offending field and never its value), so an extra field, a missing field, a plan that does not match the plan it is stored under, a negative or boolean token count, or a negative/non-finite cost or duration is refused and leaves the plan's history untouched. `FileRecords` appends through `persistence._write_usage_record` (same `<plan>.usage.jsonl` sidecar and rotation) and reads it back in file order, skipping a malformed line with one warning that carries no content; `SqliteRecords` delegates to that file view and `InMemoryRecordStore` keeps one list per `(namespace, plan)`.
- Added `record_decision`, the MCP tool that records an agent's judgment call (`pipeline/decision_record_tool.py`) in the plan's hash-chained decision stream and writes one audit record naming the decision id. It authorizes the new `decision:record` action before anything else — every authenticated `human` or `agent` holds it on a plan it may view (`app.policy.can_view`), no reserved `system:` principal does, and a hidden plan is refused identically to a missing one — then validates the ruling, rationale, evidence, decision class and story key, then applies a per-principal one-minute sliding limit (`PIPELINE_RECORD_DECISION_PER_MINUTE`, default 30, in-process so it resets on restart). Refusals never raise and never echo an argument value (`not_authorized`, `empty_ruling`, `empty_rationale`, `empty_evidence`, `invalid_decision_class`, `unknown_story`, `rate_limited`). `tier` follows the bound principal (a human records `human`, an agent `auto`: the v2 chain refuses `tier: human` from a non-human principal), the rationale is stored as the single `"rationale: <text>"` element of `alternatives` (200-character cap, so 189 characters of text), the `question` is accepted but not persisted (v2 has no field for it), and no ruling, rationale, question or evidence text reaches the audit chain. Registered in `TOOLS` and `REMOTE_ENFORCED`; `pipeline/server.py` needed no edit. Under the shared-secret provider the caller is `LOCAL_OPERATOR`, so agent attribution still needs the OIDC seam (S2).
- Added decision record v2 (ADR 0014): `pipeline/decision_records.py` keeps one hash-chained stream per plan, `<PLAN_DIR>/<plan>.decisions.chain.jsonl`, recording each autonomous decision's class, subject, evidence, rule shas, alternatives, ruling, tier, reversibility and outcome alongside `schema_version`, `seq`, `prev_hash`, `hash`, `ts`, `namespace` and the transport-established `actor`/`principal_type`/`client_id`/`correlation_id`. Caller-supplied identity or chain fields are refused before any write, every string is capped, prompt-shaped keys are rejected at any depth, and rejections name the field rather than its value; reads filter by namespace and never repair the stream, and the chain is anchored as `decisions:<plan>` so a truncated tail is detectable.
- Added the layered authority matrix as inert, versioned data: `pipeline/authority_matrix.py` loads and validates an org layer and an optional team layer, folds them tightest-first so a team can only narrow what the org allows, and ships every never-list rule locked, while `overlord-authority.json` puts all four initial decision classes at the `human` tier so the matrix grants no autonomy. Rejected team values are recorded as violations — returned, never sent, since the team-leader alert is a later story — and two `REFERENCE.md` rows document `PIPELINE_AUTHORITY_MATRIX` and `PIPELINE_AUTHORITY_TEAM_LAYER`.
- Agent principals bound to a team scope (ADR 0014 decision 3): an OIDC client listed in `PIPELINE_OIDC_AGENT_CLIENTS` authenticates as an `agent` principal bound to the `<org-id>/<team-id>` pair in its `PIPELINE_OIDC_AGENT_SCOPE_CLAIM` claim, carries no IdP roles, and is confined to its own team's plans — `pipeline.authorization` refuses it, before any authorizer runs, on a plan whose manifest names a different team and on a plan that names no team at all (fail closed). It is also denied `plan:approve` and `config:write` — on both the plan-scoped `authorize` path and the host-scoped config-write path (`refusal_for_host_config`); new audit records gain `principal_type` and `client_id`, and pre-existing chains still verify.
- Documented the `PIPELINE_TELEMETRY_PROFILE` deployment setting (`customer` by default, `harness` when set to that value, any other value falling back to `customer`) and the metrics it governs in `REFERENCE.md` and `docs/TELEMETRY_CONTRACT.md` — the story lifecycle histograms `fagan.story.dispatch_to_pr.duration`, `fagan.story.pr_to_ci.duration` and `fagan.story.pr_to_done.duration` that both profiles emit, and the `harness`-only `fagan.usage.tokens`, `fagan.usage.cost` and `fagan.usage.call.duration`; content capture, worker log shipping and failure-reason attributes are not built.
- Documented trace propagation in the telemetry contract: strict validation of an inbound `traceparent`, one trace per dispatch attempt with `TRACEPARENT` handed to the agent subprocess, and the attempt-0 attribution limit when a dispatch event is missed.
- Merge now refuses an approval that does not cover the commit being merged:
  `pipeline/review_merge_binding.py::check(story, branch_head_sha)` is a pure,
  deny-by-default consumer of the `review_verdict_sha` that `review_story`
  already records, called from `approve_merge` and from the scheduler merge
  adjudication on the story-branch tip captured before the merge-time rebase.
  A commit pushed after the review (agent rework or a human push) no longer
  merges on the earlier approval; the refusal names both short shas, tells the
  caller to re-run `review_story`, and appends one
  `merge.review_binding_denied` audit record with identifiers only (plan,
  story, the two short shas) and no diff or feedback text. The pipeline's own
  post-review checkpoint/rebase/force-push re-binds the verdict at its commit
  point instead of loosening the check.
- Operator-supplied adapters for the identity provider, authorizer, event bus,
  forge, ticket provider and notification channels via
  `PIPELINE_<SEAM>_PLUGIN=module:attr` (and `PIPELINE_CHANNEL_PLUGINS` for
  registrars), off by default; the remote MCP transport now accepts any identity
  provider declaring `network_grade = True` instead of only `OidcProvider`.
- An operator-supplied credential provider (`PIPELINE_CREDENTIALS_PLUGIN`) for the model credential and notification secrets, off by default, with `password_ref` and `webhook_url_ref` channel settings.
- A `story.review_start` audit event: `review_story` now appends one record when a review actually begins, carrying `target.plan`, `target.story`, `detail.started_at` (ISO-8601 UTC, captured before the review call) and `detail.forced`, so time-in-review can be computed from the audit stream later (liveness S9); a skipped, locked or failed review writes nothing, no verdict, diff, feedback, path or sha is recorded, and an audit failure never fails the review — the append's `OSError` is logged at ERROR by `pipeline.audit` as for every other call site, while an unexpected non-`OSError` is logged once at WARNING by type only. Documented in `REFERENCE.md`.
- The liveness signals are documented: the four health thresholds (`PIPELINE_HEALTH_STALE_SECONDS`, `PIPELINE_HEALTH_REQUIRE_SCHEDULER`, `PIPELINE_CONSUMER_STALL_SECONDS`, `PIPELINE_OUTBOX_STALE_SECONDS`) with their defaults and malformed-value rules in `REFERENCE.md`, and how to read `/api/health`, `/livez` and `/readyz` — with example Kubernetes probes — in `docs/DEPLOY_STANDALONE.md`.
- An operator-only Prometheus scrape endpoint, `GET /api/metrics`, serving the scheduler, outbox, broker/durable-consumer and plan-approval-status signals as text exposition format 0.0.4; unknown values are omitted rather than rendered as NaN, a failing producer bumps `fagan_metrics_errors_total{source}` instead of failing the scrape, and every label value comes from a closed set or a broker durable name.
- Story service-level gauges on `GET /api/metrics`: `fagan_story_dispatch_to_review_seconds`, `fagan_story_review_to_merge_seconds` and `fagan_story_cycle_seconds` at `quantile` `0.5` and `0.9`, plus `fagan_story_service_level_samples{measure}` carrying the count behind each one. They are computed from the audit stream's first dispatch, earliest review start and first merge over the stories merged in the last 30 days, using nearest-rank percentiles; queue time is deliberately not measured because the stream has no eligibility event, the numbers are fleet-wide with no plan, story or actor label, and a measure with no samples renders only its count.
- An explicit, audited `review_story(plan_name, story_key, force=True)`: `force` deletes the story's last-reviewed markers (`last_reviewed_sha`, `last_reviewed_logic_fingerprint`, `skip_notified_sha`) under the plan lock, so the review runs instead of being skipped, and it records one `story:force_review` audit entry when the markers are cleared; it leaves `review_verdict`, `review_feedback` and `rework_attempts` alone, so a forced review landing `REQUEST_CHANGES` counts against the rework budget like any other and `force` cannot loop past `PIPELINE_REWORK_MAX_ATTEMPTS`. Documented in `REFERENCE.md`.

**Namespaces and access hooks**

- Every store record is now partitioned by namespace, and a request selects one with the `X-Fagan-Namespace` header on the remote MCP transport and the dashboard. The namespace is verified against the namespace resolver (`PIPELINE_NAMESPACE_RESOLVER_PLUGIN`) before any work runs and refused with a `403` and a reason token (`namespace_malformed`, `namespace_missing`, `namespace_not_permitted`, `namespace_out_of_scope`, `namespace_error`), each denial audited. With no plugin there is one namespace, `default`, and nothing changes. See REFERENCE.md, "Namespaces".
- `PIPELINE_SCHEDULER_NAMESPACES_ONLY` / `PIPELINE_SCHEDULER_NAMESPACES_SKIP` limit one instance to a set of namespaces, for both the scheduler tick and incoming requests, failing closed when malformed. The tick advances every namespace that holds plans, and the advance worker, review job and dispatched agent (`PIPELINE_WORKER_NAMESPACE`) run in the plan's namespace.
- The dashboard plan list is a roll-up across the namespaces the view scope (`PIPELINE_VIEW_SCOPE_PLUGIN`) allows, each row labelled with its namespace, capped at `PIPELINE_PLAN_LIST_NAMESPACE_CAP` (default 20) namespaces and reporting `degraded` rather than failing.
- Three operator hooks, all off unless selected and all audited by reason token: `PIPELINE_REPO_ACCESS_PLUGIN` is consulted before every dispatch and merge (a refusal parks the story with `access refused: <reason>`); `PIPELINE_ADMISSION_PLUGIN` admits a dispatch, a spend or a notification (dispatch and spend fail closed, notifications fail open); `PIPELINE_REFERENCE_PLUGIN` decides cross-namespace `blocked_by` entries `ref:<namespace>:<plan>:<story_key>`, which are refused when it is unset.
- Model and forge credentials resolve per namespace: `default` still reads the environment, while any other namespace gets them only from the credentials plugin (`credential_missing` otherwise), and its agents never inherit the process `GH_TOKEN` or OAuth token. `CredentialRef` accepts two-segment `<org>/<team>` namespaces and the new `forge` and `secret` purposes; `secret://<provider>/<name>` references resolve through the built-in `env` and `file` (`PIPELINE_SECRET_FILE_ROOT`) providers or `PIPELINE_SECRET_PROVIDER_PLUGINS`.
- `PIPELINE_OIDC_EXTRA_CLAIMS` copies the named token claims onto the verified principal for plugins to read; core never reads them.
- The usage gate's state and cooldown can be keyed per credential (storage layer only; the default credential keeps today's paths).
- `notification_routes` and `role_routing` tenant settings can be stored per org, team and team-repo layer and are validated on write; nothing consults them at runtime yet. `PIPELINE_SETTINGS_KEYS_PLUGIN` lets an operator register additional settings keys.
- The standalone TLS front door imports operator route files from `STANDALONE_EXTRA_ROUTES_DIR` (`/etc/caddy/extra-routes/*.caddy`) ahead of its built-in routes, so one namespace can be served by its own instance.

**Story diagnosis tools**

- MCP tools `get_story_log`, `get_story_journal`, `get_story_worktree` and `get_story_events`: plan-scoped, read-only, no path argument, and every secret value masked before it is returned.
- MCP tool `unblock_story`, the remote form of `python -m pipeline.unpark`: held to the plan's owner or a delegate, clears allowlisted retry/review fields, optionally sets the status, appends `files` and a brief note, records the caller as the human resolution and resets plan approval when the brief or files change. `python -m pipeline.unpark` gains `--add-files` and records the same human resolution.

**Test scope and file-size policy**

- Plan- and story-level `test_scope` (`full` | `scoped`). A `scoped` story is graded on its acceptance paths plus the test files it adds or changes, and the full suite is left to remote CI and the `ci_rework` loop. It is resolved per dispatch from the settings layers, recorded as `test_scope_resolution`, degraded to `full` when the repo has no CI, and shown by `get_effective_config`. Telemetry counts dispatches and time-to-done by scope. See REFERENCE.md, "Test scope".
- `file_size_policy` (`advisory` default, or `enforced`) is resolved per dispatch and stamped on the story. Under `advisory` the agent is told never to park on file size and the reviewer reports size only as a Suggestion. `scripts/check_line_limit.py` gains `--mode warn|block`, and core CI runs it in `warn`.

**Overlord triage**

- Failure-triage rulings name the observed park class and are limited to the actions that class allows. Five new actions run in shadow until `PIPELINE_TRIAGE_MODE=execute`: `resolve_rebase`, `retry_transient` (which carries the overlord's diagnosis into the next dispatch), `rebrief`, `approve_test_edit` (gated by a judge of the existing-test edit) and `resume_review`. `amend_scope` now acts on scope-gate and guard evidence. A sweep sends one `triage_sweep_summary` digest. See overlord-policy.md, "Park classes".
- `python -m pipeline.triage_readiness` reports, per park class, whether its shadow rulings meet the promotion rule. It reads the human resolutions now recorded beside each ruling.

**Dashboard, CI and other additions**

- The dashboard's plan header can pause and resume a plan, and its **Review** panel approves a pending plan or requests changes.
- The merge-gate rework message names the failing tests from the CI log.
- CI scans the built server image with Trivy (report-only) and adds the `dependency-review` and OpenSSF Scorecard workflows.
- `ingest_plan` warns when a brief's Docs section edits a file the story's `files` omits.
- The plan name `host` is reserved (it would collide with the host audit-anchor key).

**Plan approval**

- Notifications for the three plan-approval events (`plan_pending_approval`, `plan_approved`, `plan_changes_requested`).
- Digest-bound plan review and decision JSON API (`GET /api/plans/{plan}/review`, `POST /api/plans/{plan}/review/approve`, `POST /api/plans/{plan}/review/request-changes`; a stale digest is refused with 409).
- Dashboard extension mount for review UIs via `PIPELINE_DASHBOARD_PLUGINS`.

**API contract**

- The dashboard's OpenAPI document is now a committed contract at
  `docs/api/openapi.json`, generated from the live application and checked in
  CI: `python scripts/api_contract.py check` fails when a route or schema
  change was not reflected in the snapshot, and `classify` flags a breaking
  difference. The surface is versioned by `API_VERSION` (stamped into
  `info.version`): a breaking change requires a MAJOR bump, an additive one a
  MINOR bump. See `docs/API_CONTRACT.md`.
- `PIPELINE_DASHBOARD_UI=off` makes the dashboard API-only: `/` and every
  static asset answer 404 and the shared-secret key is never injected into a
  response, so a replacement UI can be served from the same origin.
- Opt-in CORS for a UI hosted on another origin via
  `PIPELINE_DASHBOARD_CORS_ORIGINS` (exact origins, header credentials only —
  no cookie, no wildcard). Unset installs no CORS middleware.
- Response schemas for previously untyped operations, so a generated client
  has a `components/schemas` reference for every typed JSON response.

### Changed

- **Behaviour change:** the merge-readiness budget (`merge_readiness_timeout_s`) and the rework-on-CI-failure switch (`rework_on_ci_fail`) resolve per plan (namespace, team, repo) from the settings Store instead of the `PIPELINE_MERGE_READINESS_TIMEOUT` and `PIPELINE_REWORK_ON_CI_FAIL` environment variables, which now only seed the Store (`python -m pipeline.settings_seed`). A deployment that set those variables and never seeded the Store falls back to the defaults (180 s, rework off). When `merge_ci_gate` is off for a repo, readiness treats an `UNKNOWN` mergeability and an `UNSTABLE` merge state as ready; a conflict, a draft, or branch protection still blocks.
- **Behaviour change:** a successful *full* `ingest_plan` now consumes the staged plan draft — once `<plan>.manifest.json` is written the draft is deleted (`records.delete_plan_draft`), because the manifest is the only record of the plan (ADR 0012 decision 5). An `only_epics` ingest keeps the draft (the epics it did not ingest exist nowhere else), and so does a refused ingest and one answered `{"skipped": "locked"}`, because nothing was written. Re-ingesting a plan with no staged draft is now refused with `plan '<name>' has no staged draft: save_plan it (export_plan renders the current plan) before re-ingesting` instead of reading the old `<plan>.json` file, so callers that ingested twice without re-staging must `save_plan` again (or `export_plan` and re-save) between ingests. A crash between the manifest write and the draft delete is harmless: the next ingest re-applies identical content by explicit story key. Plan approval (ADR 0007) still binds to the same plan-defining content — the export carries each story's `description` and the plan's `team`, so an unchanged `save` → `ingest` → `export` → `save` → `ingest` round trip keeps the approval, its digest and `digest_version`.
- **BREAKING:** `save_plan` now stages the plan as a *plan draft record* (`records.put_plan_draft`, ADR 0012 decision 3) instead of writing `<plan>.json` itself, and its response no longer carries a filesystem `path` the record contract cannot provide: it answers `{"ok": true, "plan": <name>, "staged": true, "epic_count": ..., "story_count": ...}`. Every refusal — invalid JSON, a non-object, a missing `epics`, a server path from a remote caller, a `repo_root` outside the allow-list — still stages nothing, and `pipeline.plan_draft.read_plan_draft` answers with the same refusals `persistence.read_plan_file` produces, so `ingest_plan`'s messages are byte-identical. `list_plans()` now lists the plans that have a staged draft *or* an ingested manifest, where it used to return the stem of every `*.json` in the plans directory (including `x.manifest`, `x.decisions` and journal stems); `list_ingestable_plans()` keeps its meaning — a draft with no manifest yet.
- The `agent_done` wake handler (`pipeline.event_wiring.wake_handler`) reads its plan manifest through the *record store* instead of opening `PLAN_DIR/<plan>.manifest.json` itself, so on a store-backed deployment (`PIPELINE_STORE=sqlite`) the precondition is decided from the database's record rather than this host's file. The read goes through the one store-first reader, `pipeline.manifest_source.read_plan_manifest`; with the legacy `FileStore` or an in-process stand-in the plan directory is still the primary read, and a plan the record view does not know falls back to it. The handler's failure contract is unchanged — an unreadable manifest, from either source, is a `{"ok": False, "skipped": "no_manifest"}` refusal rather than an exception on the delivering thread, and the advance still re-reads the manifest before acting.
- Platform notices — the scheduler's consumer-stall and outbox alerts — are attributed to the sorted-first plan the *record store* knows, so a store-backed deployment (`PIPELINE_STORE=sqlite`) no longer labels an operator-audience alert with a plan that merely happens to sit on the scheduler's disk. The attribution rule now has one home, `pipeline.manifest_source.read_plan_names`, which `pipeline.tick_checks` calls instead of globbing `PLAN_DIR` itself; with the legacy `FileStore` the local plan directory is still the primary read, and an empty store listing still falls back to it. The label remains a routing stopgap for the tenancy work (see enterprise ADR 0001).
- The ingest story-sizing auto-route decision now applies the 2-production-file cap only to stories whose resolved dispatch model is on-device, so a capable target (Claude, litellm, or an `ollama` `:cloud` tag) is not re-routed to the host default and its file-size reason no longer ends `for the weakest dispatch tier`; the advisory `sizing_risk` notification and the 1000-line file-size cap are unchanged for both classes. The class is read from the resolved dispatch model, derived from the provider and model tag unless a registry entry declares one.
- The dispatch rules now distinguish a capable class (Claude and cloud-served open-weight models) from an on-device class; PIPELINE_BACKEND_DISPATCH=auto takes the class of the model that resolves for the first attempt.
- **BREAKING:** telemetry names moved to the `fagan` namespace: service name `fagan-pipeline` is now `fagan`, metrics `pipeline.*` are now `fagan.*`, spans `dashboard.request` and `pipeline.lifecycle` are now `fagan.dashboard.request` and `fagan.lifecycle`, and span attributes `pipeline.*` are now `fagan.*`. Dashboards and alerts keyed on the old names must be updated.
- `/openapi.json`, `/docs` and `/redoc` are **no longer served
  unauthenticated**. FastAPI's documentation endpoints sat outside the `/api/`
  credential gate; the spec now lives at `/api/openapi.json` behind the same
  authentication as every other API route, and both interactive pages are off
  by default. Set `PIPELINE_DASHBOARD_PUBLIC_DOCS=1` to restore the old public
  pages; see `docs/API_CONTRACT.md`.

### Fixed

- `import_file_store` no longer exits 0 over an audit stream it cannot address. A `*.audit.jsonl` file whose name is not a valid plan name was silently skipped by the file audit log; the importer now refuses it as `invalid_name` and exits non-zero, with the report and log naming neither the file's path nor its contents. An unlistable plan directory now fails the import instead of reading as zero chains. `FileAuditLog.list_chains` keeps its contract for other callers.
- `import_file_store` creates a new `--db` with mode `0600`, not the operator's umask (the database holds the full audit trail, manifests and context snapshots). `--dry-run` refuses a `--db` that does not exist, exits 2 and creates nothing. An existing database keeps its mode.
- A green, mergeable PR is no longer marked `failed` because GitHub had not finished computing its mergeability yet. The merge gate now polls the forge's new `merge_readiness` verb (`gh pr view --json mergeable,mergeStateStatus`: `MERGEABLE`+`CLEAN` is ready, `DIRTY`/`CONFLICTING`/`BLOCKED`/`DRAFT` is blocked, anything else including `UNKNOWN` is pending) for up to `PIPELINE_MERGE_READINESS_TIMEOUT` seconds (default 180, 5s doubling to a 30s cap) before calling `gh pr merge`. A refusal to merge is not a failed merge: a budget that expires while the host is still computing spends no `merge_attempts` slot, leaves the story `pr_open` with a `merge_pending_reason`, and lets the next tick retry, while a definitive refusal keeps today's attempt-counting path. An unreadable readiness is pending, never ready. `gh pr merge` failures now carry gh's stderr tail in `merge_error` instead of only the argv (2026-11-08: PR #1618 `c1bfcfbf` burned four attempts and PR #1621 `e0163dbc` three, both CLEAN/MERGEABLE with all four required checks SUCCESS and both merged first try by hand).
- overlord-policy.md and REFERENCE.md no longer say the `full` autonomy level always holds high-risk merges; the overlord adjudicates them and the ruling is recorded.
- `consumer_status()` now reports `dead_lettered` as the sum of the durable's dead-letter subject counts instead of the whole stream message total, which `subjects_filter` never narrowed.
- A durable event type is no longer handled twice: `NatsEventBus.publish` used
  to run the publishing process's handlers for every acknowledged event, so a
  `notification` was dispatched inline *and* replayed by its durable consumer,
  sending every Slack and e-mail notice twice. A type listed in
  `PIPELINE_NATS_DURABLE_TYPES` is now handled only by its durable consumer;
  non-durable types keep their inline dispatch.
- A skipped re-review now says why it was skipped and how to get out of it:
  `review_story`'s unchanged-since-last-review result carries `reason`,
  the truncated `head` and `last_reviewed` SHAs, the `stored_verdict` still in
  force and a `to_force` hint. The skip only holds while HEAD and the
  review-logic fingerprint are both unchanged; land a new commit (redispatch or
  rework), or force one with
  `python -m pipeline.unpark <plan> <story-key> --clear last_reviewed_sha`
  (`patch_story` cannot clear that field).
- A notification channel that keeps failing now backs off - one minute,
  doubling up to an hour - instead of retrying on every scheduler tick, so a
  broken channel stops hammering its endpoint. The email channel now reports a
  disconnect during the login phase instead of failing silently, and treats a
  temporary 4xx authentication reply as retryable rather than permanent.
  Queued notifications older than 7 days are dropped at drain time, so a
  channel that recovers after a long outage no longer flushes weeks of stale
  messages at once.
- The scheduler daemon now keeps retrying the NATS broker in the background when the broker is not up yet at start-up (the standalone stack starts `nats` a few milliseconds after the pipeline container), and starts the durable consumers when it answers. Before, those consumers stayed down until the container was restarted and their events piled up in the stream.
- REFERENCE.md now documents the story diagnosis tools, namespaces, the namespace, view-scope, repo-access, admission, reference and secret-provider plugin seams, `PIPELINE_OIDC_EXTRA_CLAIMS`, `PIPELINE_ESCALATION_BACKEND`/`_MODEL` and `PIPELINE_WEDGE_SCAN_ENABLED`/`_NOTIFY_COOLDOWN_SECONDS`. It no longer claims that credentials always resolve in the `default` namespace or that forge and Ollama keys stay environment-only. The tenant settings table lists `notification_routes` and `role_routing`, and the triage docs list all park classes and the `resume_review` action.
- REFERENCE.md now documents `PIPELINE_PLAN_LOCK_SLOW_SECONDS`, `PIPELINE_COMMAND_LOCK_WAIT_SECONDS`, `PIPELINE_DASHBOARD_PUBLIC_URL`, `PIPELINE_APPROVAL_LINK_PATH` and `PIPELINE_DASHBOARD_PLUGINS`.

### Upgrading

- **BREAKING: tenant settings are no longer read from the environment.** The eight tenant-scoped variables `PIPELINE_MERGE_CI_GATE`, `PIPELINE_MERGE_BUILD_GATE`, `PIPELINE_TEST_SCOPE`, `PIPELINE_MERGE_CI_TIMEOUT`, `PIPELINE_MERGE_READINESS_TIMEOUT`, `PIPELINE_REWORK_ON_CI_FAIL`, `PIPELINE_REVERIFY_FULL_SUITE` and `PIPELINE_GRADE_XDIST_WORKERS` now belong to the settings Store (ADR 0015) and are refused at startup. The MCP server and the scheduler daemon exit non-zero while any of them is set, to any value (the old defaults included), and print one line per variable naming its replacement key without its value. A deployment that sets one does not start until it migrates. Migration order:
  1. Run `python -m pipeline.settings_seed` once with the variables still set, as the operator, with no scope for container-wide values. Then run it again with `--team T --repo R` for any value meant for one repository only. Use `--dry-run` first; a differing stored value is reported as `already_set` and needs `--overwrite`.
  2. Remove the eight variables from the compose and env files, the launchd plist and the shell profile of each deployment. The shipped launchd scheduler plist no longer sets `PIPELINE_REWORK_ON_CI_FAIL`, so a host that installs the repo's plist now runs with `rework_on_ci_fail` off unless the Store says otherwise; set it with `set_setting` before you restart.
  3. Restart the MCP server and the scheduler daemon. Confirm they start, and read the values back with `get_settings` or `list_settings`.
  Values are never read from the environment after this change, so a change made by editing a variable has no effect; set the key in the Store instead.
- `ingest_plan` no longer returns `manifest_path`. The manifest is a record in the store, not a file with a path, so the response carries only the manifest fields. Callers that read that key must read the manifest through `get_manifest` instead.
- A `PIPELINE_STORE=sqlite` deployment does not import its file-store history
  by itself. Before pointing the store at the database, run once, as the
  operator, `python -m pipeline.tools.import_file_store --db <absolute path> [--dry-run]`
  (`--dry-run` first). It copies every record kind (manifests, plan drafts,
  completion markers and reports, decisions, journals, usage, outbox queues,
  notification history and context snapshots) and each audit chain into the
  database, verbatim, so the audit hash chain and its file anchors still match.
  It is safe to re-run, never modifies the source files, and exits non-zero
  when anything was refused; the JSON summary names what and why. A new
  database is created owner-only (`0600`); `--dry-run` needs the database to
  exist already; a real run creates it.
  When `PIPELINE_AUDIT_ANCHOR` is configured, an audit chain is refused before
  anything is written for it, with one of two outcomes: `anchor_mismatch` when
  the chain is shorter than its anchored `seq`, or its hash at that `seq`
  differs from the anchor (a cut tail, or a record rewritten and re-hashed);
  and `anchor_unreadable` for every chain when the anchor sink is configured
  but cannot be read, since the import then has nothing to check against. With
  no anchor sink configured the import is unchanged.

- Plans ingested before the manifest descriptive-fields story carry their
  authored `description`, `epic`, `plan_key`, `plan_epics`,
  `preflight_override` and `repo_url` only in their staged plan draft, not on
  the manifest. Backfill them once with
  `python -m pipeline.tools.backfill_plan_source` (`--dry-run` first; see
  REFERENCE.md, "Operator runbook: backfilling plans ingested before these
  fields existed").
  Run it **before deploying the draft-consumption story** and **before the
  digest-version-2 build**: it refuses (and reports `skipped_digest`) any plan
  whose `canonical_digest` would change, and under digest version 1 those fields
  are not digest members, so no approval record is touched and no re-approval is
  needed.
- **BREAKING: the plan-approval digest now covers each story's `description`
  and the plan's `team`**, so `APPROVAL_DIGEST_VERSION` is 2 and every approval
  recorded under version 1 stops dispatching immediately (fail closed) until a
  reviewer re-approves the plan once. Upgrade in this order: (1) deploy up to and
  including the plan-draft record and backfill story and run
  `python -m pipeline.tools.backfill_plan_source` *while descriptions are still
  outside the digest*, so recording them on legacy manifests cannot change any
  digest; (2) deploy the digest-version-2 build; (3) run
  `python -m pipeline.tools.reset_outdated_approvals --dry-run`, then
  `python -m pipeline.tools.reset_outdated_approvals` without `--dry-run`;
  (4) reviewers re-approve each plan once, now seeing its descriptions and team
  in the review payload. `patch_story` still refuses to edit `description`:
  descriptions change by re-ingest only.

## [0.9.0] - 2026-11-03

### Changed

- The standalone stack now defaults `PIPELINE_PLAN_APPROVAL` on: compose
  resolves `${PIPELINE_PLAN_APPROVAL-1}` and `install.sh` writes `1` for a fresh
  install, so every plan must be approved before it can be dispatched
  (ADR 0004, ADR 0007). Re-running the installer over an existing
  `deploy/standalone/.env` that has no `PIPELINE_PLAN_APPROVAL` line leaves the
  gate off and warns about it, so an upgrade does not strand plans ingested
  before the gate existed. Operators who enable the gate on an existing server
  must re-ingest and approve each plan once; see
  `docs/DEPLOY_STANDALONE.md` §6 and §7. The server-level default outside the
  standalone stack is unchanged.
- Relicensed AGPL-3.0-only (GNU Affero General Public License v3.0) from
  Apache-2.0. Releases through commit `98550bb` remain Apache-2.0; see
  NOTICE for the boundary and LICENSE-APACHE for the prior text. A
  contribution grant now covers all merged contributions (CONTRIBUTING.md).

### Added

**Setup**

- `scripts/configure.py` runs a guided setup: it detects what is already
  installed and writes `.pipeline.env`, per-role provider and model routing,
  policy, optional e-mail and Plane settings, the Claude Code assets, and
  (optionally) the background services. `scripts/install.sh` runs it last.
  It prompts only on a TTY, applies secure defaults with `--yes` or when
  piped, writes secret-bearing env files as 0600, and supports `--dry-run`.
  See `docs/guided-setup.md`.

**Merge**

- `PIPELINE_AUTO_MERGE=0` keeps dispatch and review automatic but parks every
  reviewed PR for a human merge (`approve_merge` or by hand). The default
  (`1`) is unchanged. The parked-story notification now includes the PR URL.

**Cross-plan coordination**

- A story can declare `blocked_by: ["<plan>:<story_key>", ...]`. It is held
  out of `list_ready_stories` and `advance_pipeline` until every referenced
  story is done or merged; a malformed or unknown reference holds it.
  `ingest_plan` validates the shape, and `patch_story` can set it.
- A `file_overlap` notification warns when a story's `files` overlap another
  plan's in-flight story, at ingest and when the story becomes ready. It
  never blocks.

**Enterprise readiness**

- `PIPELINE_POLICY_ENFORCE` (on by default) makes the service layer deny by
  default for `story:merge`, `story:mark_done`, `plan:approve`, `plan:reject`
  and `plan:pause`. Only `0`, `false`, `no`, `off` or `disabled` turns it off;
  a blank or unrecognised value keeps enforcement on.
- A plan's manifest records its `team` (default `local`) and `ingested_by`, and
  each approval records the deciding principal, its primary role, whether it
  was self-approved, and the request's correlation id.
- The dashboard mints or preserves one correlation id per request, returns it
  as `X-Correlation-ID` on every response including the 401/403 refusals, and
  stamps it onto decision and approval records.
- `PIPELINE_LOG_FORMAT=json` renders application logs as one JSON object per
  line carrying `timestamp`, `level`, `service`, `correlation_id`, `message`
  and every `extra=` field. Off by default.
- Each plan gets an append-only, hash-chained audit stream at
  `<PLAN_DIR>/<plan>.audit.jsonl`, with `actor`, `action`, `target`, `outcome`,
  `correlation_id` and a timestamp on every record; `verify_audit_chain`
  reports the first altered record. Truncation of the tail is not detected —
  the head hash is not shipped anywhere external yet.
- A dispatch that loses the advisory git lock records `stale_base_dispatch`,
  and one whose base is provably stale (checked with `git ls-remote`) is
  refused instead of being cut from the old ref; the story retries on a later
  tick.

**Authentication and remote transport**

- `PIPELINE_IDENTITY_PROVIDER=oidc` makes the dashboard verify an
  `Authorization: Bearer <jwt>` credential instead of the shared secret, using
  `app/oidc.py`: only RS256 and ES256 are accepted (the token header never
  chooses its own algorithm), `exp`/`iss`/`aud`/`sub` are required, issuer and
  audience come from configuration, the JWKS is cached for 300s, and the
  `Principal.subject` is issuer-qualified as `<issuer>#<sub>` so one issuer's
  user cannot collide with this deployment's `local-operator`. Unset keeps the
  shared secret. A misconfiguration raises at startup rather than falling back.
- `PIPELINE_OIDC_ISSUER`, `PIPELINE_OIDC_AUDIENCE`, `PIPELINE_OIDC_JWKS_URL`
  (https only) and `PIPELINE_OIDC_CLOCK_SKEW_S` (default 60, capped at 300)
  configure the verifier. `PIPELINE_OIDC_ALLOW_PRIVATE_JWKS` — enabled only by
  `1` or `true` — permits a JWKS host on a private network, which a self-hosted
  IdP inside a VPC needs.
- OIDC mode authenticates **API clients only**. The bundled browser dashboard
  mints no token, so under it the pages load but every `/api/` call is a 401,
  and the chat-stream `k-` pass-through does not exist.
- `PIPELINE_MCP_TRANSPORT=streamable-http` (or `sse`) serves the MCP server
  over HTTP at `PIPELINE_MCP_HOST`/`PIPELINE_MCP_PORT` behind an
  `Authorization: Bearer` gate; stdio stays the default and is unchanged. The
  HTTP transports refuse to start unless `PIPELINE_IDENTITY_PROVIDER=oidc`,
  and a rejection is a 401 with `{"detail":"Unauthorized"}`, an
  `X-Correlation-ID` header and a `WWW-Authenticate: Bearer` challenge. TLS is
  not terminated here — put a reverse proxy in front.
- `PIPELINE_MCP_RESOURCE_URL` makes the remote transport publish RFC 9728
  protected-resource metadata at
  `/.well-known/oauth-protected-resource`: the resource identifier, the
  authorization servers, the supported bearer methods and scopes, and a 401
  challenge that points at it, so a client that discovers the authorization
  server first can start its login flow. Unset keeps today's bare `Bearer`
  challenge.
- `deploy/standalone/mcp-token` mints a fresh MCP access token for a client
  that cannot run a browser OAuth flow: it exchanges the per-user Keycloak
  client secret (grant type `client_credentials`) and prints either a JSON
  headers object for Claude Code's `headersHelper` or, with `--raw`, the bare
  token.
- The remote policy covers every registered pipeline tool (30, up from 17), so
  each one now has a service path that consults it; a name the policy does not
  list is still refused, and `record_security_audit` stays host-scoped and
  unavailable to a network caller.
- A remote plan author may not name a server path: `repo_root` and workspaces
  are refused for remote principals, so a plan can only point at code with
  `repo_url`.
- `PIPELINE_DASHBOARD_ALLOWED_HOSTS` lets the operator allowlist the hostnames
  the dashboard accepts behind a reverse proxy, instead of trusting whatever
  `Host` header arrives.

**Standalone server (Docker)**

- `docs/DEPLOY_STANDALONE.md` is the operator guide for the standalone stack
  without the installer: what each process, port and volume does, the install
  by hand step for step, the secret file behind each variable, the
  configuration reference, Claude Code and OpenCode setup, and upgrade, backup
  and troubleshooting procedures.
- `deploy/install.sh` takes a machine that has Docker to a running standalone
  stack and prints how to connect Claude Code and OpenCode. It is interactive
  by default; `--non-interactive` reads every value from flags or the
  environment for CI. Secrets are never accepted on the command line — they
  come from `--*-file` flags or a no-echo prompt. The whole script runs under
  `umask 077` and writes every compose secret file and the generated
  `deploy/standalone/.env` at `0600`.
- `deploy/standalone/realm.json` is a Keycloak realm template the installer
  renders at install time: each `${env.NAME}` is substituted from the resolved
  value (failing by variable name when one is unset), instead of being imported
  with its placeholders intact. The realm ships PKCE for both confidential
  clients, registration off and a 900s access-token lifespan.
- A plan may name a `repo_url` instead of a server path; the server clones it
  into the managed root and re-fetches that clone on ingest, so a long-lived
  server ingests a fresh checkout. A plan naming both `repo_url` and
  `repo_root` is refused as ambiguous.
- Dispatch-time git — the branch check, the worktree fetch and the agent's own
  push — authenticates to a private origin with the same mounted forge token
  the clone uses, so a private `repo_url` no longer exits 128 mid-dispatch.
- The runtime loads its operator credentials (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN`, `OLLAMA_API_KEY`) from mounted secret
  files, refusing anything that is not a `0600` regular file owned by the
  service uid, and exports each only to the processes that need it.
- Startup refuses placeholder credentials: the three Keycloak and OIDC values
  compose reads with `${VAR:?}` are rejected when they are still the published
  `change-me-*` constants, so a copied or outdated env file cannot boot a stack
  with a secret that is printed in this repository. `deploy/standalone/.env.example`
  ships those entries empty, so `docker compose up` refuses to start until they
  are set.
- The NATS container shares the pipeline container's network namespace, which is
  attached to the compose project network, so its previous `0.0.0.0` binding let
  every other service on that network reach the broker; both stacks now bind it
  to loopback.
- The installer prompts for (or accepts flags for) SMTP and Slack notification
  settings and writes the `notification.json` that the server's notification
  routing loads, deciding what is enabled from the persisted secret files so a
  re-run keeps the configured channels. That config carries only
  environment-variable names, never a credential, and the Slack webhook is
  stripped from the environment every agent child sees.
- `deploy/standalone/smoke.sh` runs the whole operator path against a throwaway
  state directory — install, MCP token, a `repo_url` plan saved, ingested,
  approved and dispatched, and grading — and the `standalone-smoke` workflow
  runs it on a Linux runner behind the repository variable
  `ENABLE_STANDALONE_SMOKE_CI` (off by default); with the gate enabled it passes
  there.
- CI builds the server image on every pull request, and a push to `master`
  publishes it to GHCR as `ghcr.io/<owner>/fagan`, tagged with the commit SHA
  and `master`, with SBOM and provenance attestations (see
  `docs/RELEASING.md`).

**Dashboard**

- The story modal has a read-only **Diff** section showing the worktree's
  changes against its fork point, including uncommitted and untracked files.
  The same data is available as the `get_story_diff` MCP tool and
  `GET /api/plans/{plan}/stories/{key}/diff`.
- The sidebar has a SYSTEM settings group, and each settings view has a
  deep-linkable URL hash.

**Tests**

- A frozen-pin guard (`tests/unit/test_pin_guard_baseline.py`) fails CI on a
  new SHA-256 pin, exact enumeration or exact count of a cumulative artifact
  in `tests/`, against a baseline that may only shrink.

### Fixed

- Standalone installer rendered the Keycloak realm file `0600`, which the container user (uid 1000) could not read when the installing user was not uid 1000, so the IdP crash-looped; `render_realm` now publishes it `0644`, and the client secrets stay private because every caller renders into a `0700` directory (install.sh's `$STATE_DIR/import`, created `mkdir -m 0700` and refused if symlinked or foreign-owned, and the documented manual recipe's `deploy/standalone/import`) — never copy `realm.json` out of that directory.
- The standalone runtime image installed no `procps`, so every `ps` call from
  status, watchdog and review paths raised `FileNotFoundError` and a dispatched
  story could never be seen as finished; the package is now part of the
  existing runtime apt line.
- A hand-started standalone stack failed before the server ran because the
  compose stack bind-mounts `deploy/standalone/model_registry.json` and nothing
  created it. The installer now seeds that file once — the operator's local
  registry if present, else the shipped catalog — and leaves an existing one
  untouched; it is gitignored because its rows are machine-specific.
- The standalone MCP transport refused the default 443 deployment with 421
  because the SDK matches a host wildcard only when the `Host` header carries a
  port; both the bare host and the wildcard form are now accepted, and the
  operator docs match.
- The first end-to-end standalone smoke run exposed four defects, all fixed:
  the remote transport now allows the public host with any port, the clone root
  is created owned by the runtime uid, the admin user the installer logs in as
  is seeded, and the seeded bare repo points at the branch that has the commit.

## [0.8.0] - 2026-10-22

### Added

**Agent tooling**

- `restore_file` gains an optional `from_base` mode that restores a file
  from origin's default branch, giving an agent a first-class way back when
  its own commit captured a bad change (the destructive-git block refuses a
  base-scoped checkout). The default path is unchanged, and the per-path cap
  applies to both modes.
- `scripts/agent_log_scorecard.py <worktree>/agent.log [--json]` reports a
  per-attempt scorecard from an agent log: step totals, per-tool counts, the
  first mutating step, the productive-step ratio, guard trips by normalized
  label, and the outcome with its done summary or parked reason. It slices
  at the final `[boot]` line so a resumed run is scored on its last attempt,
  and reads the same grammar the log writers emit.

**PR review**

- A warn-only `pr_title_mismatch` notification fires after a PR opens when
  its title shares no 4+ letter word with the story summary or the changed
  files. It fails open and never alters the PR, verdict, or merge path.

**Scheduler**

- The scheduler notifies when the running checkout trails upstream by
  commits that touch `pipeline/` or `app/`, since merged orchestration
  changes are otherwise invisible until the daemon restarts. The notice
  dedups on the upstream SHA and points at `scripts/reload_pipeline_daemon.sh`.

### Changed

- Python 3.11 is now the floor everywhere — install check, `ruff`
  `target-version`, and a new 3.11 CI leg (3.11-3.14) — with a consistency
  test so the three cannot drift. Python 3.10 is no longer supported.
- Dependencies: `fastapi>=0.143.0`, `ruff==0.17.0`.
- The on-device dispatch tier is documented as not cleared for story
  dispatch, including its measured first-pass-clean rates and the preflight
  an operator must run to route it back.

### Fixed

**Security**

- Acceptance fixture paths that are absolute or contain `..` are rejected at
  ingest and skipped with an operator notice at dispatch, so plan-authored
  data can no longer write outside the worktree (security audit 2026-10-19).
- Pipeline secrets (`PIPELINE_NOTIFY_EMAIL_PASSWORD`, `PLANE_API_KEY`,
  `GH_TOKEN`, `GITHUB_TOKEN`) are stripped from every agent subprocess
  environment, including locally spawned agents that inherit the parent
  environment.

**Dispatch and merge**

- A story re-dispatched while still `todo` no longer preserves a stale or
  half-written acceptance fixture from a dead earlier attempt: the skip is
  gated on the story's status, so a fixture-only oracle cannot go green with
  no implementation (Mode 45).
- A merge-gate CI-fail rework keeps any earlier review feedback instead of
  replacing it with only the CI error, so the implementer cannot silently
  re-break what the reviewer asked for.
- An escalation whose resolved target equals the story's current
  backend/model is skipped rather than resetting the rework budget and
  inflating the first-pass-clean metric.

**Local agent**

- Every write path rejects a file whose last non-blank line is a trailing
  omission marker (e.g. `# ... (rest of file remains same)`), so a
  context-exhausted model cannot silently land a truncated file.
- `replace_lines` computes dropped top-level symbols unconditionally, so a
  renamed definition can no longer slip past the symbol-loss check.
- `create_file` overwrites of non-`.py` files reject content that drops more
  than ~10% (floor of two) of the original lines unless `confirm_removals`
  is set, covering markdown and other text files the AST guard cannot see.

**Review, chat and classification**

- The review loop makes one final no-tools call for a verdict when it
  exhausts without one, failing closed to the existing no-verdict path.
- At the turn cap, chat strips `[TOOL_CALL]` markup and answers in prose; a
  near-cap nudge asks the model to do so before the budget runs out.
- Legacy event-less sidecar notices map to the same reason names structured
  events use, so escalations are no longer double-counted and benign topic
  words no longer mark a clean story non-clean.

**Scheduler, usage and notifications**

- The staleness check receives the config fingerprint it compares, so the
  trailing-checkout advisory actually fires in production.
- Subscription usage percentages parse when the `resets ...` suffix is
  absent (e.g. `Current session: 0% used`), instead of falling back to the
  request-count approximation and potentially pausing an available
  subscription.
- The review-gate deferral notice fires once per gated episode instead of
  once per scheduler tick (one plan accumulated 159 identical records in
  3.2 hours).

## [0.7.0] - 2026-10-09

### Added

**Dispatch**

- The test-author step now runs detached from `dispatch_story` through
  non-blocking start/collect primitives, with a decision step for when to
  use it; the planner block moved into `dispatch_phases` (no behavior
  change for that extraction).
- A PR title is derived from the branch's Conventional Commits subject
 .

**Local-agent harness**

- `view_file` returns a repeat advisory instead of re-emitting identical
  bytes, and the one-sided-read latch guard keys reads by their own bound
  so orientation in a large file is not a false-positive park.
- `FULL_SUITE_DONE_BAR` is armed on the oracle harness, the oracle gets
  the story summary in its env, and its auto-commit has a story-derived
  subject.

**Scheduler**

- Health is written at startup before the first reconcile and refreshed
  during long watchdog joins; `SchedulerDaemon.start()`'s reconcile is
  bounded by the watchdog.

**Benchmark tooling**

- Every role is pinned per arm through `role_config`, the grid refuses to
  run when its pins cannot resolve, and a one-command published-matrix
  driver records pinned provenance.
- A Tier 3 `inventory_pagination` cross-module task, Claude dollars per
  success in the scorecard, preserved dispatch cost logs, and Claude
  cells skipped when credits or rate limits run out.

### Changed

- The maintainer's home path is scrubbed from archived plans, specs and
  retros, and the committed launchd files use a placeholder home
  directory.
- The exported overlord policy spec matches the live parked-story policy.
- A bare `pytest` run is clean on a fresh clone.

### Fixed

**Scheduler and merge**

- A dispatch lease whose owner pid is dead is treated as not live.
- CI status queries run in the scoped plan repo, and an unreadable CI
  status parks the story instead of being treated as no CI.
- A trailing DONE line in `agent.log` counts as `agent_done` in the wedge
  scan.

**Review gate**

- `ClaudeCliDriver.resource_status` accepts `model_tag`, so the review
  gate no longer crashes the scheduler.
- Review is gated on the model it will actually run, the Claude usage
  pause is re-derived from cached percentages, and consecutive
  rate-limited review deferrals are bounded and then park.

**Local agent**

- A successful `create_file` resets its own repetition count.

## [0.6.0] - 2026-09-30

### Added

**Language-agnostic gates (LAG)**

- A repo can declare its test, lint and build commands, and its
  `test_globs`, in a root `.fagan.json`; the scope gate treats declared
  test globs as test files (#1037, #1039).
- Build, test and lint command detection moved out of `build_detect.py`
  into `ecosystem_detect.py`, which now also detects Go and .NET and fails
  visibly on any other unsupported ecosystem (#1036, #1038).
- Maven and Gradle projects run `verify` / `check` through the project
  wrapper when one is present, and their test failures are parsed so
  acceptance runs scope to the failing JVM test classes (#1041, #1044).
- One test-path classifier is shared by the scope gate, ingest and
  `plan_conflict_ruling`, and the ingest sizing check recognises any
  backticked relative source path (#1035, #1040).

**Global rules bundle**

- An opt-in installer (`scripts/install_global_rules.py`, hinted by
  `install.sh`) writes the engineering standards and the pipeline workflow
  rules into each supported agent CLI's global instructions file inside a
  managed block, honouring `XDG_CONFIG_HOME`; documented in the README
  Quickstart and REFERENCE (#992-#1000, #1019, #1021).

**Security audit tracking**

- The `record_security_audit` MCP tool and a per-repo audit record, with
  due thresholds and a scheduler notice when a repo is due (#1009-#1012).
- The security-engineer verdict is posted on the PR, and ingest warns when
  a brief needs a security review the pipeline will not run (#1007, #1008).

**Scheduler and harness diagnostics**

- The scheduler warns at startup when the launchd plist and the MCP
  server config disagree, by diffing the two environments rather than a
  hand-maintained catalog; secret-like and non-`PIPELINE_` values are
  masked (#1027, #1032, #1059).
- Each dispatch attempt's log starts with a `[boot]` line, and a watchdog
  kill snapshots the end of `agent.log` into the plan dir (#1023, #1024).
- A rejected story is re-reviewed when the reviewing logic has changed,
  and a skipped re-review is notified once per SHA (#1049, #1052).
- A two-tier CI gate enforces the 1000-line rule: blocking on production
  Python, warn-only elsewhere (#1048).
- The one-line installer finishes Claude Code setup, and the README leads
  with a demo and the one-line install (#982, #987).

### Changed

- A triage `mark_done` ruling requires a merged PR before it marks a story
  done, and a PR merged outside the pipeline is recorded as `story_merged`
  when `mark_story_done` closes it (#1033, #984).
- Each automated rebrief cause has its own event name, and the rebrief
  header detectors match an occurrence rather than a mention (#1050,
  #1031).
- `effective_env_config`'s `effective` field is labelled as this-process
  rather than resolved against code defaults (#1058).
- `scripts/local_agent.py`, `scripts/local_agent_oracle.py`,
  `app/dashboard.py`, `pipeline/dispatch.py`, `pipeline/story_status.py`
  and `pipeline/worktree_patch.py` were each brought under 1000 lines.
  No behavior change (#1051, #1053-#1057).
- The retro backlog is capped so the pending-marker list stays drainable
  (#1047).

### Fixed

**Dispatch & merge**

- Dispatch is refused when a plan's `repo_root` no longer exists, and a
  story that is already done or has an open PR is not re-dispatched
  (#1016).
- A story's registry model-name pin resolves to its tag, and the scope
  gate treats nested and Rust test paths as test files (#1004, #1003).
- Every pipeline runtime marker is self-excluded in worktrees, and the
  acceptance oracle is kept out of story commits (#1014, #1015).
- A dispatch attempt clears the previous attempt's done markers, and the
  watchdog skips its SIGTERM when the stored pid belongs to a process
  started after dispatch (#1025, #1026).
- Triage defers a transient overlord failure instead of parking the story,
  and reports git evidence against the worktree-resolved base branch
  (#981, #1042, #1043).

- Triage's sweep no longer writes back a stale whole-manifest snapshot: it
  re-reads the manifest and applies only the sweep's own changes, so
  writes made while a ruling was in flight survive (#1061).

**Tests**

- The smoke precondition test no longer depends on the operator's local
  model registry (#1062).

**Reporting accuracy**

- The local-success report is filtered to a single repo and names its
  cohort; the plan summary flags a reviewer-bounced story beside its
  baseline verdict; ruling and sizing notices carry their story key
  (#1030, #989, #983, #979).

**Dashboard**

- The toast message gets its own full-width line (#990).

## [0.5.0] - 2026-09-24

### Added

**Local-dispatch convergence (LDC)**

- A local agent that reaches its step cap now finishes as done when its
  branch carries production changes against the default branch and the
  full suite and lint pass, instead of burning a rework cycle on work that
  already landed. A rework redispatch that produced no new commit starts
  the next attempt from a fresh context seeded with a root-cause
  diagnosis, and a third `restore_file` on the same path in one run is
  refused (#932, #933, #937, #938).
- The test-author phase is skipped for stories whose declared `files` are
  all docs or config, and its prompt now carries a size budget and asks
  for structural assertions rather than prose wording (#928, #934).
- Each completed plan's summary, including a per-story first-pass-clean
  verdict, is written to `<plan>.report.md` in `PLAN_DIR` (#931, #936).

### Changed

- **Re-ingesting an already-ingested plan is rejected when any story lacks
  an explicit `key`.** Such a re-ingest used to mint duplicate stories
  that dispatched concurrently; give every story a `key` to re-ingest
  (#929).
- `triage.py`, `service.py` and `server.py` were each brought under 1000
  lines, extracting `triage_patch_acceptance`, `triage_story_actions`,
  `service_decisions`, `service_role_config` and
  `server_tools_lifecycle` as their own modules. No behavior change
  (#941-#944, #946).
- Every `pipeline` module now imports cold, in any order - the remaining
  import-order dependencies on `pipeline.server` were removed and the
  stale comments that described them corrected (#966-#969, #971-#974).
- Improved the descriptions of the lowest-scoring MCP tools (#927).

### Fixed

**Dispatch & merge**

- A story's bare registry model name now resolves to its tag instead of
  silently falling back to the default local model (#930).
- A worktree whose HEAD is detached is re-attached to the story branch
  before the rebase, or the story is parked when re-attaching would
  orphan commits (#965).
- A rescoped story's PR title is refreshed when the PR already exists, so
  the squash commit subject describes the final work (#945).

**Local harness**

- A capped `view_file` result is cut on a line boundary and says the file
  on disk is intact, so a weak executor no longer "repairs" a file that
  only looked truncated; the scope gate's revert advice is now runnable on
  the local harness (#958, #961).

**Reporting accuracy**

- The plan rollup no longer counts the synthetic uncorrelated bucket as a
  story, collapses metric groups that share a story key, and counts
  watchdog kills and reviewer send-backs as rework cycles (and a reviewer
  send-back as a dirty first pass). Per-story metric, sizing-auto-route
  and preflight-override notices carry their story key; step-cap and
  give-up rebriefs emit `brief_patched`; retry-cap notices render the cap
  instead of an object address (#948-#951, #954-#957, #959, #960).

**Dashboard**

- A long multi-line notification toast now renders cleanly (#953).

**Test determinism**

- Two tests that raced git's background auto-maintenance or the shared
  advisory fetch lock across xdist workers are now deterministic (#935,
  #975).

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
