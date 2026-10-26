# Notification channels plan — pluggable channel types, routed per team

> Status: **shipped** as plan `notification-channels` (NC-01..NC-08, PRs
> #1221-#1229, plus `notification-channels-registration`). Supersedes the
> 2026-10-21 draft and the first revision of this document. Kept as design
> rationale.

## 1. Goal

Send pipeline notifications to any number of channels — email, Slack, SMS,
a webhook, a message broker — with configuration that decides *which message
types go to which channel for whom*. One team can receive plan completions by
email and parked or failed stories in Slack; another can receive everything by
email and nothing in Slack. Adding a channel type must be one new module, and
adding a team must be configuration only. Neither may touch the outbox, the
scheduler, the renderer, or another channel.

## 2. Decisions recorded

| Question | Decision | Why |
|---|---|---|
| Channel scope | Slack and email as types for V1 (email migrated onto the registry); the contract serves SMS and others | The abstraction, not the count, is the deliverable |
| Type vs instance | A **type** is code (`slack`, `email`); an **instance** is configuration (a name, a type, its settings) | Several Slack destinations, per-team email recipients |
| Routing | Routes match a plan-name glob and map event types to instances; **first matching route wins**; unlisted events go nowhere | Per-team control with a predictable, deny-by-default rule |
| Tenant key | Plan-name glob today; `match.owner` is reserved and rejected until the plan-owner field exists (ADR 0006) | Plans have no owner yet; the selector grows without a schema break |
| Config surface | One JSON file, `PLAN_DIR/notifications.json` (override `PIPELINE_NOTIFY_CONFIG_PATH`) | Several instances and routes do not fit flat env vars; JSON needs no dependency |
| Secrets | A setting named `<x>_env` holds the **name** of an env var; inline secrets are rejected at load | The file can be committed, shared or inspected without leaking a credential |
| Bad config | Fail closed: a broken file delivers nothing and logs ERROR; it never falls back to legacy mode | A typo must not silently reroute messages |
| No file | Exactly today's behaviour: email via its env vars and the legacy outbox | No migration for existing setups |
| Retire policy | One queue per **instance**; a record is retired per instance when that instance accepts it | Gives "all enabled channels" with no duplicates and independent retry |
| Permanent failure | `REJECTED` drops the record with one ERROR log | A revoked webhook must not block its own queue forever |
| Retries and back-off | `DeliveryResult.retry_after_seconds` is in the contract; V1 retries once per drain tick and ignores it | A broker-backed type supplies real back-off later, with no contract change |

## 3. Architecture

```
notification.json  ──►  NotificationConfig  (instances + routes, validated, fail closed)
                              │ resolve(plan, event) -> [instance names]
bus "notification" event      ▼
   └─ outbox_sink ──► <plan>.outbox.jsonl                  (legacy; email env path, unchanged)
                  ├─► <plan>.outbox.<instance>.jsonl       (one per routed instance)
                  └─► ...

scheduler tick (bounded drain worker)
   ├─ drain_outbox(ALL_PLANS, send_notification_email)      (unchanged)
   └─ drain_channel_outboxes() ── per instance: _drain_one_outbox(path, as_sender(instance))
```

Worked example (the two teams):

```json
{
  "version": 1,
  "instances": {
    "team-a-mail": {"type": "email", "host": "smtp.example.com", "port": 587,
                    "user": "notifier", "from": "pipeline@example.com",
                    "to": ["team-a@example.com"], "password_env": "TEAM_A_SMTP_PASSWORD"},
    "team-a-slack": {"type": "slack", "webhook_url_env": "TEAM_A_SLACK_WEBHOOK"},
    "team-b-mail": {"type": "email", "host": "smtp.example.com", "port": 587,
                    "user": "notifier", "from": "pipeline@example.com",
                    "to": ["team-b@example.com"], "password_env": "TEAM_B_SMTP_PASSWORD"}
  },
  "routes": [
    {"name": "team-a", "match": {"plan": "team-a-*"}, "deliver": [
      {"events": ["plan_completed"], "to": ["team-a-mail"]},
      {"events": ["story_parked", "dispatch_failed", "tests_failed", "agent_gave_up"],
       "to": ["team-a-slack"]}]},
    {"name": "team-b", "match": {"plan": "team-b-*"}, "deliver": [
      {"events": ["*"], "to": ["team-b-mail"]}]}
  ]
}
```

Plans that match no route receive nothing from this file.

- **The channel type contract** (`pipeline/notification_channels.py`):
  `type_name`, `from_settings(instance_name, settings, env)` which validates
  configuration, and a per-instance `send(record) -> DeliveryResult` that never
  raises. `DeliveryResult` is `DELIVERED`, `RETRY` or `REJECTED`, plus an
  optional `retry_after_seconds` and a fixed-token `reason`, never exception
  text, a URL, or a recipient.
- **`as_sender`** adapts an instance to the `record -> bool` callable that the
  existing `_drain_one_outbox` expects. `drain_outbox`'s signature and
  behaviour do not change.
- **One renderer** (`pipeline/notification_render.py`) turns a record into a
  channel-neutral `RenderedNotification`; each type only formats it for its
  transport.
- **Email on the registry.** `send_notification_email` keeps its exact
  signature, env knobs and tests; it and the new `email` type share one SMTP
  send core. The legacy env path stays as the no-config default.

## 4. Security requirements (Secure by Design)

- Everything is deny by default: no config file means no new channel; a route
  delivers only the events it lists; no matching route delivers nothing.
- Credentials are never in the file. Inline `password`, `token`, `webhook_url`,
  `secret` and `api_key` settings are rejected at load. Secrets are read from
  the named environment variable at send time and never stored on an instance.
- A broken config file fails closed and is loud; it never reroutes messages.
- The Slack URL must be `https`, uses the verifying TLS context, and
  **redirects are not followed**.
- No webhook URL, password, recipient list or payload appears in any log line,
  exception text, `DeliveryResult` or test failure message. Only exception types
  are logged, and payloads are never logged at INFO or below.
- `send` and `from_settings` never leak configuration in errors: messages name
  the instance and the setting key only.
- Config size is bounded (256 KiB) and every glob, name and address is
  validated. Email addresses reject CR/LF.
- No new dependency: stdlib `json`, `fnmatch`, `urllib`, `ssl`, `smtplib`.

## 5. Non-goals (V1)

- SMS, Teams, a generic webhook or a broker type. The contract is built for
  them and none is built.
- Interactive Slack features, inbound webhooks, or two-way integration.
- Real back-off, attempt caps and a dead-letter queue. The seam exists
  (`retry_after_seconds`); the policy is later work.
- Owner-based routing (`match.owner`) until the plan-owner field exists.
- Hot-reload guarantees: the file is read on each spool and drain, so an edit
  takes effect on the next event or tick, but no atomicity guarantee beyond
  that is promised.

## 6. Stories

| Key | Story | Risk | Production files |
|---|---|---|---|
| NC-01 | Extract the channel-neutral renderer | low | `notification_render.py`, `notification_email.py` |
| NC-02 | Channel type contract and registry | medium | `notification_channels.py` |
| NC-03 | Load, validate and resolve the config and routes | high | `notification_routing.py` |
| NC-04 | Route, spool and drain per-instance outboxes | medium | `notification_outbox.py` |
| NC-05 | Scheduler wiring and the config and extension docs | medium | `scheduler_daemon.py`, `REFERENCE.md` |
| NC-06 | Email as a routable channel type | medium | `notification_email.py` |
| NC-07 | Slack incoming-webhook channel type | high | `notification_slack.py`, `REFERENCE.md` |
| NC-08 | Real-socket and multi-tenant verification | low | none (tests only) |

The chain is linear. NC-05 and NC-07 both edit `REFERENCE.md`, and NC-06 sits
between them so no two stories edit it concurrently. NC-03 and NC-07 are
`risk: high` so the security-engineer pass runs on config and credential
handling and on the network code.

## 7. Preflight and tier notes (complete at ingest time)

1. **Tier:** the live registry pins `dispatch` to `claude/sonnet` and
   `PIPELINE_BACKEND_DISPATCH` is unset, so this is the Claude-class tier.
   Re-confirm before ingest (CLAUDE.md Step 1). The `Preflight:` line is
   required only for a non-Claude tier.
2. **Known coupling, already handled in the briefs:**
   - `test_notification_outbox_drain.py` pins `drain_outbox(plan_name, sender)`:
     NC-04 adds a new function and leaves it alone.
   - `test_scheduler_drain_overlap.py` monkeypatches
     `notification_outbox.drain_outbox` and
     `notification_email.send_notification_email`: NC-05 resolves both through
     the module at call time.
   - `test_notification_email_*` pin the sender's behaviour: NC-06 must pass
     them unedited, which is the grade for the SMTP-core extraction.
   - `test_readme_reference_split.py` pins moved `REFERENCE.md` sections
     verbatim: NC-05 and NC-07 only append subsections.
   - `test_config_provenance*.py` may pin the env var list. NC-07 adds no env
     var (the webhook variable name is chosen by the operator in the file), so
     none is expected; if one breaks, raise it through `request_decision`.
3. **Not yet run:** the impact run in `local-dispatch-preflight.md` section 1;
   required only for a non-Claude tier.
4. Every story has an explicit `key`.
5. NC-08 carries `[no-new-tests]` because it is itself the verification.

## 8. Risks and open questions

- **Event identity:** a record has no id. Per-instance queues avoid needing
  one; a downstream dedup would need an id added at spool time.
- **Disk growth:** an instance that is never drained grows its own file. NC-04
  reuses the existing size rotation; a queue-age cap is later work.
- **Re-routing:** records already spooled stay in the queues of the instances
  they were routed to, and files for an instance removed from the config are
  left undrained rather than deleted. This is documented, not hidden.
- **Config location:** the default is `PLAN_DIR/notifications.json`, which is
  per-machine. A shared server (topology T3) will need the file to be
  per-tenant or served; the `match` selector and `version` field leave room for
  it.
- **Legacy outbox gate:** with a config file present the routes are the event
  allowlist, so `PIPELINE_NOTIFY_OUTBOX_EVENTS` no longer filters instance
  queues. State this in the migration notes of `REFERENCE.md`.

## 9. Ingest-ready plan JSON

```json
{
  "repo_root": "/Users/jessecarroll/git/fagan",
  "epics": [
    {
      "summary": "Notification channel types, routing, and Slack delivery",
      "stories": [
        {
          "key": "NC-01",
          "summary": "Extract the channel-neutral notification renderer",
          "description": "Behavior-preserving extraction so every channel type renders from one source.",
          "persona": "software-engineer",
          "risk": "low",
          "files": [
            "pipeline/notification_render.py",
            "pipeline/notification_email.py"
          ],
          "agent_instructions": "Read `docs/plans/NOTIFICATION_CHANNELS_PLAN.md` sections 3 and 4. Create `pipeline/notification_render.py` holding `SUBJECT_LABELS`, `STORY_LEVEL_EVENTS` and `render_notification(record: dict) -> RenderedNotification` (frozen dataclass: `subject`, `body`, `event`, `plan`, `story_key`). Move the record -> subject/body logic out of `send_notification_email` UNCHANGED: payload via `record.get('payload') or {}`, the legacy 'plan complete' fallback for a missing/None/unknown event, story key appended only for STORY_LEVEL_EVENTS, body from payload message then top-level message. `notification_email.py` imports from the new module and must still expose `SUBJECT_LABELS` and `STORY_LEVEL_EVENTS` under its own namespace so existing imports and monkeypatches keep working; its subject/body output must be byte-identical. `render_notification` may raise on a malformed record (callers already wrap it in a fail-closed try); do not add a swallow. Tests in new file `tests/unit/test_notification_render.py`: each of the 5 allowlisted events, a missing event, an unknown event, a story-level event with and without a key, `plan_completed` with a key set (key must NOT appear), payload null, legacy flat record, a non-dict payload raises. Do NOT modify any existing test. Success: new tests plus the existing `test_notification_email_*` suites pass; `ruff check .` clean."
        },
        {
          "key": "NC-02",
          "summary": "Define the channel type contract and registry",
          "description": "The extension point: a new transport is one class with from_settings and send, plus one register call.",
          "persona": "software-engineer",
          "risk": "medium",
          "files": [
            "pipeline/notification_channels.py"
          ],
          "agent_instructions": "Read plan sections 3-5. Create `pipeline/notification_channels.py` (stdlib only). Types: `DeliveryStatus` (enum DELIVERED, RETRY, REJECTED); `DeliveryResult` (frozen dataclass: `status`, `retry_after_seconds: float | None = None`, `reason: str = ''` -- a short fixed token, never exception text, never a URL, never a recipient). A channel TYPE is a class with `type_name: str` (regex `^[a-z][a-z0-9_]{0,31}$`) and a classmethod `from_settings(instance_name: str, settings: dict, env: Mapping[str, str]) -> Channel` that validates the instance settings and raises `ChannelConfigError` (a ValueError subclass; message names the instance and the offending KEY, never a secret value) on any problem. A Channel INSTANCE has `name`, `outbox_suffix` (`.outbox.<name>.jsonl`), and `send(record: dict) -> DeliveryResult` which MUST NOT raise. Secrets never appear in `settings`: a setting named `<x>_env` holds the NAME of an environment variable, resolved through the `env` mapping at call time; `from_settings` must reject any setting whose key looks like a secret held inline (`password`, `token`, `webhook_url`, `secret`, `api_key`) with ChannelConfigError. Registry: `register_channel_type(cls)` (duplicate or invalid `type_name` raises ValueError), `channel_type(type_name)` (unknown raises KeyError with the type name), `registered_types()`. `as_sender(channel) -> Callable[[dict], bool]` is the adapter the existing `_drain_one_outbox` needs: DELIVERED -> True; RETRY -> False; REJECTED -> log ERROR once with instance name and reason token, return True (a permanently rejected record is dropped so it cannot block its queue); a raising `send` -> False with only the exception type logged. `channel_outbox_path(plan: str, channel) -> Path` under `paths.PLAN_DIR`; a plan name containing `/`, a backslash or `..` raises ValueError. Instance names match `^[a-z][a-z0-9_-]{0,31}$`. Module docstring records the rule: one queue per channel INSTANCE, so each retries independently and a record is never resent to an instance that accepted it. The channel-type registry is cumulative -- later stories register more types. Your tests must assert only what YOU add (membership, behaviour), never the full registry contents or an exact count. Tests in new file `tests/unit/test_notification_channels.py` with a fake type: each status through `as_sender`, a raising send, duplicate and invalid type names, an unknown type, inline-secret keys rejected, `_env` resolution, path traversal in plan, REJECTED log carries no record content. Do NOT touch `notification_outbox.py`, `scheduler_daemon.py`, or any existing test. Success: new tests pass; `ruff check .` clean.",
          "dependencies": [
            "NC-01"
          ]
        },
        {
          "key": "NC-03",
          "summary": "Load and validate the notification config and resolve routes",
          "description": "Instances, routes and tenant scoping as validated configuration. Fails closed.",
          "persona": "software-engineer",
          "risk": "high",
          "files": [
            "pipeline/notification_routing.py"
          ],
          "agent_instructions": "Read plan sections 3-5. Create `pipeline/notification_routing.py`. The config file is JSON at `PIPELINE_NOTIFY_CONFIG_PATH`, default `paths.PLAN_DIR / 'notifications.json'`; an ABSENT file means 'legacy mode' and `load_notification_config()` returns None. Shape (version 1): `{\"version\": 1, \"instances\": {<name>: {\"type\": <type_name>, ...settings}}, \"routes\": [{\"name\": str, \"match\": {\"plan\": <glob>}, \"deliver\": [{\"events\": [<event>|\"*\"], \"to\": [<instance name>]}]}]}`. Implement `load_notification_config(path=None, env=os.environ) -> NotificationConfig | None`, which parses and validates and builds every instance via `channel_type(t).from_settings(name, settings, env)`; `NotificationConfig.resolve(plan: str, event: str | None) -> list[str]` returning the instance names to deliver to; and `NotificationConfig.instances`. Resolution rule (document it in the docstring): the FIRST route whose `match.plan` glob (`fnmatch.fnmatchcase`) matches the plan wins; within it, an event is delivered to the union of `to` lists of every `deliver` entry that lists the event or `*`; an event the winning route does not list goes NOWHERE (deny by default); no matching route means nothing is delivered. `match` supports only `plan`; any other key, including `owner`, is rejected as 'not supported yet' (reserved for the plan-owner field in ADR 0006). Validation failures raise `NotificationConfigError` (names the problem and the key path, never a secret or a full file dump): bad JSON, wrong `version`, unknown type, duplicate or invalid instance name, a route referencing an unknown instance, an empty `to`, an empty `events`, a non-string event, non-object entries, a file over 256 KiB. `load_notification_config` itself RAISES on an invalid file; add `safe_load_notification_config()` which catches NotificationConfigError, logs ERROR with the error text, and returns an EMPTY config (no instances, nothing delivered) -- fail closed, never fall back to legacy mode on a broken file, because a typo must not silently reroute messages. Tests in new file `tests/unit/test_notification_routing.py` with a fake type: absent file -> None; each validation failure; first-match-wins; a team route with `*` to email only and another with parked/failed to slack only (the two-team scenario from the plan); an unlisted event goes nowhere; no matching route goes nowhere; `owner` rejected; inline secret rejected; broken file -> empty config and an ERROR log. Pure logic plus one file read. The channel-type registry is cumulative -- later stories register more types. Your tests must assert only what YOU add (membership, behaviour), never the full registry contents or an exact count. Do NOT modify any existing test. Success: new tests pass; `ruff check .` clean.",
          "dependencies": [
            "NC-02"
          ]
        },
        {
          "key": "NC-04",
          "summary": "Route, spool and drain per-instance outboxes",
          "description": "Per-instance queues: independent retry, no duplicate delivery, legacy path untouched without a config.",
          "persona": "software-engineer",
          "risk": "medium",
          "files": [
            "pipeline/notification_outbox.py"
          ],
          "agent_instructions": "Read plan sections 3-5. In `pipeline/notification_outbox.py`: (1) extend `outbox_sink` so that when `safe_load_notification_config()` returns a config, in addition to the existing legacy behaviour it appends the identical JSON line to `channel_outbox_path(plan, instance)` for each name in `config.resolve(plan, event)`. When a config file EXISTS the routes are the allowlist: an event that no route delivers is not spooled, and `PIPELINE_NOTIFY_OUTBOX_EVENTS` is ignored for the instance queues. When no config file exists nothing changes at all. One instance's write failure is logged (type only) and must not stop the others or the legacy write. (2) Add `drain_channel_outboxes(plan_name: str = ALL_PLANS) -> dict[str, int]` which, per configured instance, drains that instance's outbox file(s) with the existing `_drain_one_outbox(path, as_sender(instance))` (ALL_PLANS globs `*<suffix>`) and returns delivered counts keyed by instance name. Never raises. Files for instances no longer in the config are left untouched (documented, not deleted). Reuse the existing rotation helper for instance files. Do NOT change `drain_outbox`'s signature, the legacy file name, or the record format. Do NOT import `notification_email`. The channel-type registry is cumulative -- later stories register more types. Your tests must assert only what YOU add (membership, behaviour), never the full registry contents or an exact count. Tests in new file `tests/unit/test_notification_outbox_routing.py` with fake types and a real config file under tmp_path: the two-team scenario end to end (team A plan completions only reach its email instance, its parked/failed only reach slack; team B everything reaches email and nothing reaches slack); a RETRY on one instance retries only that instance; REJECTED drops; a raising instance does not block another; no config file leaves legacy output byte-identical; a broken file spools to no instance; records appended during a drain survive. Do NOT modify any existing test. Success: new tests plus `test_notification_outbox_*` pass; `ruff check .` clean.",
          "dependencies": [
            "NC-03"
          ]
        },
        {
          "key": "NC-05",
          "summary": "Wire the instance drains into the scheduler and document config and extension",
          "description": "Wiring plus the documented config and extension contract, kept with the code that creates them.",
          "persona": "software-engineer",
          "risk": "medium",
          "files": [
            "pipeline/scheduler_daemon.py",
            "REFERENCE.md"
          ],
          "agent_instructions": "Read plan sections 3-5. In `pipeline/scheduler_daemon.py`, inside the existing `_drain_outbox_worker` (same bounded worker, same log-and-swallow), keep the existing `drain_outbox(ALL_PLANS, send_notification_email)` call exactly as is and add a following call to `notification_outbox.drain_channel_outboxes()`; each in its own try/except so one failing cannot skip the other. Resolve through the module (`notification_outbox.drain_outbox`, `notification_outbox.drain_channel_outboxes`, `notification_email.send_notification_email`) at call time -- `tests/unit/test_scheduler_drain_overlap.py` monkeypatches those module attributes and a `from ... import` binding would silently bypass the patch. In `REFERENCE.md`, in the existing section 'Notifications: plan_completed and the outbound e-mail channel', ADD (never inside a block pinned by `test_readme_reference_split.py`) a subsection 'Notification routing' documenting: the config file path and `PIPELINE_NOTIFY_CONFIG_PATH`, the schema with a worked two-team example (team A: plan completions to email, parked/failed to slack; team B: everything to email, nothing to slack), first-match-wins, deny-by-default, `_env` secret references and the rejection of inline secrets, fail-closed behaviour on a broken file, that a config file makes the routes the event allowlist, and that `match.owner` is reserved; and a subsection 'Adding a channel type' documenting the contract (`type_name`, `from_settings`, `send`), the DeliveryStatus semantics, one queue per instance, and that `send` never raises and never logs a payload, recipient or credential. Tests (new file `tests/unit/test_scheduler_channel_drain.py`): the tick calls both drains; a raising instance drain does not prevent the email drain and vice versa; a wedged instance drain does not start a second concurrent drain (reuse the overlap pattern). Run `tests/unit/test_readme_reference_split.py` and `tests/unit/test_docs_have_no_personal_home_path.py`. Do NOT modify any existing test. Success: new tests plus existing scheduler and docs suites pass; `ruff check .` clean.",
          "dependencies": [
            "NC-04"
          ]
        },
        {
          "key": "NC-06",
          "summary": "Make email a routable channel type",
          "description": "Email joins the same registry as every other channel, without changing the legacy env path.",
          "persona": "software-engineer",
          "risk": "medium",
          "files": [
            "pipeline/notification_email.py"
          ],
          "agent_instructions": "Read plan sections 3-5 and `pipeline/notification_email.py`. Extract the SMTP send core of `send_notification_email` into `_send_smtp(settings: EmailSettings, rendered: RenderedNotification) -> bool` where `EmailSettings` is a frozen dataclass (`host`, `port`, `user`, `password`, `from_addr`, `to_addrs: tuple[str, ...]`, `timeout`). `send_notification_email(record)` keeps its exact signature, enabled gate (`PIPELINE_NOTIFY_EMAIL_ENABLED` exactly `1`), env knobs, fail-closed messages and 'never raises' contract, and now builds `EmailSettings` from the environment and delegates; behaviour must be byte-identical and every existing `test_notification_email_*` test must pass UNEDITED. Add `EmailChannelType` (type_name `email`) registered with the channel registry: `from_settings` accepts `host`, `port`, `user`, `from`, `to` (a non-empty list of addresses, each validated to contain no CR/LF and exactly one `@`), `timeout`, and `password_env` (the NAME of an env var; the password is read at send time, never stored on the instance, never logged). `send` renders with `render_notification` and maps: success -> DELIVERED; SMTP 4xx / connection / timeout errors -> RETRY; SMTP 5xx recipient or auth rejection -> REJECTED with reason `smtp_rejected`; a malformed record -> REJECTED `malformed_record`; never raises; only the exception type is logged; TLS verification stays mandatory exactly as in the existing sender. Tests in new file `tests/unit/test_notification_email_channel.py`, faking only `smtplib.SMTP`: settings validation (missing host, empty `to`, CR/LF in an address, inline `password` rejected), each result mapping, the password read from the named env var and absent from every log record, multiple recipients. Do NOT modify any existing test. Success: new tests plus all existing email suites pass; `ruff check .` clean.",
          "dependencies": [
            "NC-05"
          ]
        },
        {
          "key": "NC-07",
          "summary": "Add the Slack incoming-webhook channel type",
          "description": "The first non-email implementation; handles a credential and the network, so it gets the security pass.",
          "persona": "software-engineer",
          "risk": "high",
          "files": [
            "pipeline/notification_slack.py",
            "REFERENCE.md"
          ],
          "agent_instructions": "Read plan sections 3-6 and `pipeline/notification_email.py` for the conventions to copy. Create `pipeline/notification_slack.py` with `SlackChannelType` (type_name `slack`) registered with the registry. Instance settings: `webhook_url_env` (the NAME of an env var holding the webhook URL -- a credential, read at send time) and `timeout` (default 20, must be > 0); an inline `webhook_url` is rejected by the NC-02 rule. `send` renders with `render_notification`, POSTs JSON `{\"text\": ...}` with stdlib `urllib` and a default verifying `ssl` context, and returns: 2xx -> DELIVERED; HTTP 429 -> RETRY with `retry_after_seconds` from a valid numeric `Retry-After`; 5xx, timeout, DNS or connection error -> RETRY; any other 4xx (revoked or malformed webhook) -> REJECTED `http_4xx`; the env var unset, a non-HTTPS URL or an unparseable URL -> RETRY `misconfigured` after logging ERROR with the variable NAME only (fixing the env recovers the queue); a malformed record -> REJECTED `malformed_record`. Security (all tested): scheme must be exactly `https`; redirects are NOT followed (an opener whose redirect handler raises, so a 30x never re-sends the body or URL); the webhook URL appears in no log line, exception text, DeliveryResult or test failure message; only the exception TYPE is logged; the payload is never logged at INFO or below; text escapes `&`, `<`, `>` and is capped at 3000 characters with a visible `[... truncated ...]` marker; `send` never raises. In `REFERENCE.md` add a 'Slack channel type' subsection AFTER 'Adding a channel type' with the settings table and a 'the webhook URL is a secret; reference it by env var name' warning. Tests in new file `tests/unit/test_notification_slack.py`, mocking ONLY the HTTP opener boundary: settings validation, env unset, non-HTTPS, an `http://` URL never opens a connection, redirect not followed, each status class, Retry-After (valid, negative, non-numeric), bad timeout, escaping, truncation, the URL absent from every captured log record and result, malformed record. Do NOT modify any existing test. Success: new tests plus the full suite pass; `ruff check .` clean.",
          "dependencies": [
            "NC-06"
          ]
        },
        {
          "key": "NC-08",
          "summary": "Verify routing and delivery against real sockets and files",
          "description": "Real-environment check so green mocked tests are not the only evidence, including the two-team scenario.",
          "persona": "software-engineer",
          "risk": "low",
          "files": [],
          "agent_instructions": "Read `pipeline/notification_slack.py`, `pipeline/notification_routing.py` and `pipeline/notification_outbox.py`. Add ONE new test file `tests/unit/test_notification_real_env.py` (no production change) that mocks nothing in the channel, routing or outbox code. (1) Start a real `http.server` on 127.0.0.1 in a thread; point a Slack instance's env var at its `http://` address; assert `send` returns a non-DELIVERED result AND the server received zero requests. (2) Start a real TLS listener with a throwaway self-signed certificate generated in the test with the `openssl` CLI (skip with a clear reason if `openssl` is not on PATH); assert an untrusted certificate yields RETRY, never DELIVERED, and the URL is absent from captured logs. (3) Multi-tenant end to end with a real config file under tmp_path, two fake channel types standing in for email and slack, and plans `team-a-build` and `team-b-build`: team A routes `plan_completed` to the email instance and parked/failed to the slack instance; team B routes everything to email and nothing to slack. Emit one event of each kind for each plan through `outbox_sink`, run `drain_channel_outboxes`, and assert exactly which instance received which events; then make the slack instance return RETRY and assert only its queue retains records across three drains. Print measured counts beside expected ones (use `capsys.disabled()`) per testing-config-gates.md. Do NOT modify production code or any existing test. Success: new tests pass offline in under 15 seconds; `ruff check .` clean. [no-new-tests]",
          "dependencies": [
            "NC-07"
          ]
        }
      ]
    }
  ]
}
```

## 10. Definition of Done

- NC-01..NC-08 merged through `approve_merge`, each with its tests observed
  failing first.
- NC-08 passes and prints measured counts, including the two-team scenario.
- `REFERENCE.md` documents the config, the routing rules and the extension
  contract; no webhook URL, password or recipient appears in any log or test
  output.
- Adding a channel type needs one module and a register call, and adding a team
  needs only a config edit, with no change to `notification_outbox.py`,
  `scheduler_daemon.py`, or the renderer.
