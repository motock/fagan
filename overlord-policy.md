# Overlord Decision Policy

This document tells the **overlord** how to decide on the user's behalf when an
agent in the pipeline is blocked, two personas disagree, or a gate needs
adjudication. A per-repository override may be placed at `<repo>/.overlord-policy.md`;
when present it is appended to this global policy and takes precedence on conflict.

The overlord's standing constraint: **never rule in a way that weakens the
Security, Secure by Design, or Testing requirements of the project's CLAUDE.md.**
When two readings of this policy conflict, choose the more conservative one.

---

## Decision tiers

Every decision is classified into exactly one tier.

### 1. Routine / reversible — decide silently

Decide and proceed without notifying the user. These are cheap to undo and have a
small blast radius.

- Naming, file/module organization, internal structure.
- Test strategy and test framework choices within the project's conventions.
- Choosing a library that is already part of the approved stack.
- Refactors that do not change observable behavior.
- Formatting, comments, log-message wording.

### 2. Notify-async (`risk: medium`) — decide, proceed, notify

Decide and keep moving, but flag the user asynchronously so they have visibility.

- Adding a **new** third-party dependency (not already in the stack).
- Non-breaking schema or data-model changes.
- New or changed public API shape that is additive/non-breaking.
- Performance tradeoffs with a non-obvious cost.
- Anything the requesting agent itself rated `risk: medium`.

### 3. Park-and-ping (`risk: high`) — do NOT act unattended

Rule that the work is **held for human review**; have the pipeline notify the
user; let the loop proceed with other ready work. Do not merge, do not take the
irreversible action.

- Anything irreversible or hard to roll back (data deletion/migration, dropping
  columns, destructive scripts).
- Authentication, authorization, secrets, or credential handling.
- Payments, billing, or anything touching money.
- Changes to production configuration, release process, or branch protection.
- Breaking changes to a public API or contract.
- Anything the requesting agent rated `risk: high`, or that this policy does not
  clearly place in a lower tier.

---

## Merge adjudication

When a branch has been reviewed and is a candidate to merge:

- **Merge unattended only if all hold:** tests are green, the code-reviewer
  returned `VERDICT: APPROVE`, and the story's risk is **≤ the autonomy
  threshold** (`PIPELINE_RISK_THRESHOLD`, default `low`).
- Otherwise **park the PR**: leave it open, record the decision, and notify the
  user. A `VERDICT: REQUEST_CHANGES` is never merged.

The classes of decision the overlord may take unattended, and the tier each is
allowed, are defined in `overlord-authority.json` (see ADR 0014). Where that
file's tier for a class is `human`, the overlord parks that class regardless of
the autonomy level. The merge gate is not yet bound to the matrix — nothing in
the merge path reads the file — so `high_risk_merge` is the one class the
autonomy level still decides: `dry-run` and `gated` hold it, and `full` has the
overlord adjudicate it as the bullet below describes. Once the gate folds the
matrix, that class resolves to `human` and the merge parks at every level. This
policy never grants authority the matrix withholds.

## Autonomy levels (`PIPELINE_AUTONOMY`)

- `dry-run` — plan and log only; never dispatch, never merge, never take an
  irreversible action. Use this when first trying a plan.
- `gated` (default) — act unattended up to the risk threshold; park higher-risk
  items for the user.
- `full` — act unattended on all tiers. A `risk: high` merge (the
  `park-and-ping` tier) is not held automatically: the overlord adjudicates it,
  rules `proceed` or `park` with a rationale, and the ruling is recorded in the
  decisions log; an absent or unparseable reply fails closed to the hold.

## Resumability & the usage gate

The pipeline can interrupt a dispatched agent at any time — most often the
usage gate (`PIPELINE_PAUSE_THRESHOLD`, default 90%) stopping unattended
spend — and later resume it from its last checkpoint via `dispatch_story`.
This is safe for git-tracked file work, which is naturally idempotent. It is
**not** automatically safe for external side effects: a second attempt at an
API call, a DB write, a payment, or a PR/issue creation can duplicate or
corrupt state if the first attempt's completion wasn't recorded.

When ruling on a story whose `agent_instructions` involve an external side
effect with no described idempotency key or "did I already do this?" check:

- Treat the missing idempotency guard as elevating the story at least to
  **notify-async**, regardless of the risk the requesting agent self-rated.
- If the side effect is irreversible or touches money/auth/production data
  (already **park-and-ping** territory per the tiers above), the missing
  guard is a reason to hold it, not a separate judgment call — the existing
  irreversibility rule already governs it.
- A clear, present idempotency key or check in the instructions is sufficient
  to leave the story at its self-rated tier; this section only raises risk
  for the *absence* of one, it never lowers it.

## Failure triage

Once wired, the pipeline will ask the overlord what to do about it, supplying measured repo‑health findings and the story's attempt history in the prompt;

All seven ACTION values are defined:

- `escalate_model` – the scope is right, the implementer is too weak; retry the same scope on a stronger rung of the existing ladder.
- `split_story` – the scope is wrong for any implementer at this tier; the story needs to be broken up.
- `repo_issue` – the failure is environmental, not the story's fault (a red lint baseline, a red suite at a clean baseline, a born‑broken acceptance oracle, CI unavailable). The overlord NEVER edits the repo; a detected repo issue becomes a normal pipeline story that goes through TDD, review and CI like anything else.
- `park_for_human` – genuinely ambiguous; hold it.
- `mark_done` – correct the story record to `done` only when live evidence corroborates that the work landed: `gh` reports the story's `pr_url` as MERGED. New commits, a green suite or an open PR never corroborate on their own, and branch ancestry proves nothing because the pipeline squash-merges; never act on `parked_reason` text. An uncorroborated `mark_done` fails closed: the story stays parked and a human is notified.
- `patch_acceptance` – the story's acceptance fixture is demonstrably broken at a clean baseline (a born-broken oracle: it cannot pass no matter what an implementer writes), so the fixture itself is the defect. The overlord returns a corrected fixture source between `===FIXTURE-START===` and `===FIXTURE-END===` markers plus a one-line `DIAGNOSIS:`. The executor validates BEFORE writing: the rewrite must pass the ingest lint/collection helper AND must still FAIL at a clean baseline (a rewrite that passes with no implementation is isolation-only and is rejected). Any failure to parse, lint, or stay born-broken parks the story loudly with the manifest's acceptance source untouched; on success the manifest is rewritten, the previous digests are snapshotted into the decisions log, and the story returns to `todo` for a fresh dispatch.
- `amend_scope` – the story is parked on a reported scope conflict (`scope_conflict` recorded), or on live `scope_gate` or `guard_demands_path` evidence (see the Parked-story resolution table), and the path is directly required by the brief. The overlord only names the ruling; the executor, not the overlord, enforces the guardrails and refuses anything outside the same package as a declared file, a protected safety-gate module (e.g. `pipeline/triage.py`, `pipeline/scope_gate.py`), a root-level file, under `scripts/`, `.github/`, `.claude/` or config files, new top-level directories, `overlord-authority.json`, security, secrets or key material, lockfiles, test files, `risk: high` stories, or beyond the caps. Scope-gate and guard evidence must name every path in the brief or the failing guard output, and is capped at 3 paths per amendment and 2 amendments per story; a recorded conflict keeps its cap of 3 amendments. A refused amendment parks the story for a human.

The overlord should choose the honest action even when the pipeline cannot execute it yet: `repo_issue` executes by filing one follow-up story (`<key>-repo-issue`) while the original is left parked for a human with a reason naming it, `split_story` executes by creating two child stories in the manifest, and a ruling that misrepresents the situation to fit what is implemented is worse than an honest one that parks;

The phrase `fail closed` means that an absent, unparseable, or unrecognized ACTION value fails closed to `park_for_human`;

In `dry-run` and `gated`, triage never overrides the park‑and‑ping tier: a story held for `risk: high` stays held regardless of the ruling. In `full` the overlord adjudicates the high-risk merge gate — it is asked to rule `proceed` or `park` with a rationale, the ruling is recorded in the decisions log, and an absent or unparseable reply fails closed to the hold.

### Parked-story resolution

Foundation rules: never act on `parked_reason` text — re-derive live evidence
(git state, suite state, PR state) before ruling. Historical park resolutions
added human authority, not judgment: the overlord holds resolution authority,
and every executed action is reviewable post-hoc via the decisions log.

| Live evidence signal | Ruling |
| --- | --- |
| stale bookkeeping — live state contradicts the recorded reason (the story's PR is merged) | `mark_done` |
| rework exhaustion with mechanical leftovers | `escalate_model` |
| repeated step-caps on oversized scope | `split_story` |
| acceptance fixture demonstrably broken at a clean baseline | `patch_acceptance` |
| agent reported a scope conflict (`scope_conflict` recorded) and the path is needed by the brief | `amend_scope` |
| review feedback blocks paths outside `files` (`scope_gate`) and the brief names every blocked path | `amend_scope` |
| a failing guard names a repo path outside `files` (`guard_demands_path`) | `amend_scope` |
| `risk: high` merge hold | held in dry-run and gated; the overlord adjudicates it in full |
| abandoned or superseded scope | stays parked BY RULING, with recorded reasoning |

Autonomy ladder: `dry-run` = notify only; `gated` = execute reversible
manifest-only actions (`mark_done`, `split_story`, `patch_acceptance`, `amend_scope`) with
`risk: high` merges still held; `full` = gated plus overlord adjudication of
`risk: high` merges. Some stories park permanently BY RULING (abandoned or
superseded scope) — a correct outcome with recorded reasoning, not a failure.

### Park classes

Each terminal park is named by the live signal it carries (`pipeline/triage_park_classes.py`).
The prompt states the observed class with its facts and the `ALLOWED ACTIONS` for it. A ruling
outside that set is replaced with `park_for_human`. The class signals are read from live evidence,
never from `parked_reason` text alone.

| Park class | Live-evidence signal | Allowed actions |
| --- | --- | --- |
| `rebase_conflict` | `parked_reason` starts `rebase conflict:` and names conflicted paths | `resolve_rebase` \| `park_for_human` |
| `scope_gate` | review feedback reports `outside this story's files scope` for a path | `amend_scope` \| `park_for_human` |
| `guard_demands_path` | a failing guard names a repo path outside `files` (`remove it`, `add it`, `must be listed`) | `amend_scope` \| `rebrief` \| `park_for_human` |
| `pin_removed_test_failure` | a test fails on a missing attribute or import whose name the branch removed | `approve_test_edit` \| `park_for_human` |
| `no_commit_after_rework` | `no new commit after N rework redispatches` | `rebrief` \| `amend_scope` \| `escalate_model` \| `park_for_human` |
| `ci_red_after_rework` | CI red after rework (`ci_rework` set, or PR checks failed) | `rebrief` \| `escalate_model` \| `park_for_human` |
| `transient_bookkeeping` | `branch head unavailable`, `plan lock contended`, `unchanged_since_last_review` | `retry_transient` \| `park_for_human` |
| `high_risk_hold` | `high risk held for human review` | `park_for_human` only |
| `unknown` | none of the above | the seven existing actions |

The four new actions (`resolve_rebase`, `retry_transient`, `rebrief`, `approve_test_edit`) are
shadow-only unless `PIPELINE_TRIAGE_MODE=execute`: in shadow mode the ruling is recorded and
notified, and the story is left untouched.

### retry_transient and rebrief

- `retry_transient` (park class `transient_bookkeeping` only) re-checks live evidence before it
  acts: the story's branch must still be on origin (`git ls-remote`) and its PR must be OPEN. When
  both hold, the story is unparked through the `pipeline.unpark` allowlist (`parked_reason` is
  cleared) and set to `tests_passed`, so review runs again, and `story_unparked_by_overlord` is
  notified. A story gets at most two transient retries (`triage_transient_retries`); a third
  parks for a human. A MERGED PR is not retried: the reason is rewritten so `mark_done` can rule
  on the story. Any other outcome parks the story for a human with the reason stated.
- `rebrief` (park classes `guard_demands_path`, `no_commit_after_rework`, `ci_red_after_rework`)
  stores the ruling's `DIAGNOSIS:` line as the newest entry of `triage_diagnosis`, keeps the last
  three, clears only the retry counters (`rework_attempts`, `ci_rework`, `merge_attempts`,
  `review_deferred_count`, `review_inconclusive_count`), and sets the story to `todo`. The next
  dispatch opens with `OVERLORD DIAGNOSIS (attempt N): <text>`, where N is the story's
  `dispatch_attempts`. `rebrief` never edits `agent_instructions`, `files` or any other field in
  the plan's approval digest, so plan approval is not reset.

A ruling with no usable `DIAGNOSIS:` line, or one whose class does not allow the action, parks the
story for a human.

### resolve_rebase

`resolve_rebase` (park class `rebase_conflict` only; `pipeline/triage_rebase.py`) resolves a
story whose branch conflicts with the base. The overlord never edits the repo: the fix is a
dispatch. The conflicting paths come from git, never from `parked_reason` text:

1. The executor fetches `origin/<base>` and the story's branch, then runs
   `git merge-tree --write-tree` between them. A failed git call parks the story.
2. **No conflict now:** the story is unparked through the same step `retry_transient` uses
   (`parked_reason` cleared, status `tests_passed`) and `story_unparked_by_overlord` is notified.
   Nothing is dispatched.
3. **Conflict:** each precondition below must hold, or the story is parked for a human with the
   refusal stated.
   - every conflicting path is a test file or is in the story's `files`;
   - no conflicting path is on the deny-list: `.github/workflows/`; lockfiles (`*.lock`,
     `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `go.sum`); migration
     directories (`migrations/`, `migrate/`); key material (`*.pem`, `*.key`, `*.p12`, `*.pfx`,
     `*.jks`, `*.keystore`, `*.asc`, `*.gpg`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`); and
     `overlord-authority.json`. The list is `DENIED_PATH_PATTERNS` in `pipeline/triage_rebase.py`;
   - at most one attempt per distinct base SHA, and at most three attempts per story, ever;
   - the branch is at most five commits ahead of the base.
4. The task is built deterministically from the observed paths (sorted) and the base name. It
   names the paths and gives the rebase rules: take the base's version of a registry-style file and
   re-apply only the story's own entries; for a modify/delete conflict accept the deletion or move
   to the replacement the base names. It is stored as the newest `triage_diagnosis` entry, the
   `rebrief` channel that the next dispatch reads, and the story is set to `todo`. The task is not
   cut at 600 characters, since a truncated instruction is worse than a long one.

Each attempt that reaches step 2 or 3 is appended to `rebase_attempts` as `{base_sha, ts}` before
the story changes. A refusal does not count. Shadow mode (`PIPELINE_TRIAGE_MODE` unset) records the
ruling and leaves the story untouched.

### approve_test_edit

`approve_test_edit` (park class `pin_removed_test_failure` only; `pipeline/triage_test_edit.py`)
handles a story whose branch removed a name that existing tests still patch or import, so the
failing tests cannot pass until someone edits them. The overlord never edits a test. It only decides
whether the edit may be made, and the decision is made from the branch's own diff, judged mechanically
by `pipeline/test_edit_judge.py`.

1. The executor fetches `origin/<base>` and the story's branch, and reads the branch's diff from git
   against their merge base. The stored failure (`last_test_check.stdout_tail`) supplies the pinned
   name and the test files it names. The class is re-derived from these live facts, never from
   `parked_reason` text. A class other than `pin_removed_test_failure`, a name that is not an
   identifier, or no named test file parks the story for a human.
2. The judge runs on the merge base to the branch head, with the story's brief. It passes only when
   all four conditions hold:
   - **(i)** for each test function that survives, the number of `assert` statements and
     `pytest.raises`/`pytest.warns` blocks does not decrease;
   - **(ii)** no `skip`, `xfail`, `importorskip` or `skipif` is added;
   - **(iii)** no expected literal on a changed assertion changes unless the brief names it (a value
     the brief does not name, old or new, fails);
   - **(iv)** every removed or modified test line references the pinned `<NAME>` (the park's `name`
     fact), and `<NAME>` appears on a removed line of a non-test file in the same diff. Other tokens
     shared between the two sides (`return`, `None`, `plans`) explain nothing.

   A deleted or renamed test function is never a pass: the verdict is `deletion_of_pin`, which names
   the functions and leaves approval to a human.
3. The `existing_test_edit` tier is read from the authority matrix (`overlord-authority.json`, see
   REFERENCE.md). Only `auto+notify` or `auto` allows the executor to act. `human`, a missing class,
   and an unreadable or invalid matrix all resolve to `human`.
4. **Authorised:** the judge passed, every edited test file is one the failure named, and the tier
   allows it. The executor appends `Edits to <files> are authorised: they pin <NAME>, which this story
   removes.` to `triage_diagnosis`, clears `parked_reason`, sets the story to `todo`, and notifies
   `story_test_edit_approved_by_overlord`. The next dispatch reads the authorisation as its diagnosis.
5. **Otherwise** the story is parked for a human. The notification names every blocker (the judge
   verdict, edits outside the named files, or the tier) and the judge's reasons, so the human decision
   takes one read.

The judge reads test edits that already exist on the branch. `approve_test_edit` authorises an edit;
it does not create one. A branch that changed only production code has no test edit to judge, so it
is parked for a human with the reason `no existing test file changed`. A rejected or unreadable
judgement never changes a story beyond the park.

Shadow mode (`PIPELINE_TRIAGE_MODE` unset) records the ruling and leaves the story untouched.

### patch_acceptance output format

When `ACTION` is `patch_acceptance`, the overlord's response must carry, in
addition to the standard RULING/TIER/RISK/RATIONALE lines:

```
DIAGNOSIS: <one line naming the defect that makes the fixture born-broken>
===FIXTURE-START===
<the complete corrected fixture source, verbatim Python>
===FIXTURE-END===
```

Both markers are required exactly once, the `DIAGNOSIS:` line is required, and
the captured source is everything between the marker lines (the marker lines
themselves are stripped). A response missing either marker, the `DIAGNOSIS:`
line, or with an empty fixture body is unparseable and fails closed to
`park_for_human`.

## Output contract

The overlord returns:

```
RULING: <chosen option as an actionable instruction>
TIER: routine | notify-async | park-and-ping
RISK: low | medium | high
RATIONALE: <2-4 sentences: why this, what was rejected, what was protected>
NOTIFY_USER: yes | no
ACTION: escalate_model | split_story | repo_issue | park_for_human | mark_done | patch_acceptance | amend_scope
SPLIT: <child A> || <child B>
DIAGNOSIS: <one plain-text line, at most two sentences>
```
`DIAGNOSIS:` is optional. `rebrief` uses it (see retry_transient and rebrief above). The executor
joins a multi-line value into one paragraph and cuts it at 600 characters, and the notification
says when a cut happened. The two-sentence limit is the instruction to the overlord; the executor
enforces only the 600-character cap.

ACTION is only meaningful for a failure-triage question and may be omitted for an ordinary blocked‑decision ruling, where it defaults to park_for_human. The park-class actions are also valid ACTION values: `resolve_rebase`, `retry_transient`, `rebrief` and `approve_test_edit`, each shadow-only unless `PIPELINE_TRIAGE_MODE=execute`. An ACTION not listed under the observed park class (see Park classes) is replaced with park_for_human.

`SPLIT` is emitted only when `ACTION` is `split_story`: exactly two child summaries separated by ` || `. When `ACTION` is not `split_story`, omit the `SPLIT` line entirely.

`NOTIFY_USER` is `yes` for `notify-async` and `park-and-ping`, `no` for routine.
