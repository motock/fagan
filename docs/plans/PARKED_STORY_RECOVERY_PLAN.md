# Parked-story recovery plan

Status: shipped as plan `parked-story-recovery` — 7/7 stories merged (PRs
#1238-#1244). Tier: Claude-class dispatch at the time (`claude/sonnet`, no
`PIPELINE_BACKEND_DISPATCH`); each story was sized for that tier.

## 1. Why

Between 2026-10-23 and 2026-10-24 six stories parked on harness gaps rather
than defects in their code (CL-12, CL-14, CL-20, CL-23, CL-24, plus 429s on
CL-14's escalation). Every fix was a human-in-the-loop step with a clear
right answer, so the harness and the overlord should be able to take it.

| What happened | Stories | Root cause |
| --- | --- | --- |
| Agent needs a path outside the declared `files` | CL-14, CL-20, CL-23, CL-24 | The plan's `files` did not match where the code has to go. The agent diagnosed it correctly, called `request_decision`, was permission-denied, and stopped. The harness read the empty turn as a crash. |
| Escalated dispatch died at step 0 with 429 | CL-14 | Transport errors consumed the rework budget; the story parked as 'agent keeps parking/crashing'. |
| Rebase conflict at the merge gate | CL-12, CL-20 | Two additive blocks at one insertion point; the gate cannot resolve conflicts. Deferred, see section 5. |
| Review would not re-run after a scope fix | CL-14, CL-20, CL-24 | The gate needs a changed commit to the flagged files; the fix was a cosmetic comment commit. |

## 2. Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| How the agent reports a scope conflict | A one-line marker in its final message: `SCOPE-CONFLICT: <path> :: <why>` | Needs no new permission. The scope-gate remedy already tells the agent to 'end your turn stating the conflict'; the marker makes that machine-readable. Granting `request_decision` to headless agents is a wider change to the permission model and is left to its own decision. |
| Who decides to widen scope | The overlord proposes `amend_scope`; deterministic code in the executor decides | A ruling is advisory. The executor refuses anything outside the same package, under `scripts/`, `.github/`, `.claude/` or config files, new top-level directories, high-risk stories, and more than 3 amendments per story. Deny by default. |
| Review after an amendment | The executor clears the five review fields and sets `tests_passed` when the last green check matches HEAD | Removes the cosmetic-commit workaround without a new tool. |
| Transport failures | Retried without charging `rework_attempts`; capped at 5, then parked with an accurate reason | A 429 at step 0 says nothing about the agent. |
| Ingest-time check | A non-blocking `scope_headroom` warning when a declared file has under 50 lines of headroom | Catches the CL-14 shape (`dispatch.py` at 999 lines) before dispatch. |

## 3. Stories

| Key | Summary | Files | Risk |
| --- | --- | --- | --- |
| HR-01 | Define the scope-conflict marker and teach the scope-gate remedy to use it | `scope_conflict.py` (new), `scope_gate.py` | low |
| HR-02 | Park a no-commit turn that reports a scope conflict without charging the rework budget | `no_progress.py` (new), `story_status.py` | medium |
| HR-03 | Guardrails and executor for `amend_scope` | `triage_scope_actions.py` (new) | high |
| HR-04 | Wire `amend_scope` into triage, the overlord policy and the evidence prompt | `triage.py`, `parsers.py`, `overlord-policy.md`, `REFERENCE.md` | high |
| HR-05 | Do not charge transport failures to the rework budget | `no_progress.py` | medium |
| HR-06 | Warn at ingest when a declared file has no line-limit headroom | `scope_headroom.py` (new), `ingest.py` | low |
| HR-07 | Harden the `amend_scope` guardrail: protected modules, no root files, control characters, single audit record | `triage_scope_actions.py` | high |

Order: HR-01 then HR-02 then HR-03 then HR-07 then HR-04; HR-05 after HR-02; HR-06 is
independent. `story_status.py` (981 lines) and `triage.py` (946 lines) have
little headroom, so HR-02 puts all logic in `no_progress.py` and leaves a
four-line hook, and HR-05 extends `no_progress.py` only.

## 4. Security notes

HR-07 was added after the human review of HR-03 (2026-10-24) found that the
guardrail allowed root-level files and safety-gate modules, and that the
executor and the dispatcher would both write an audit record. HR-04 depends
on it, so nothing can call `amend_scope` until the hardening has landed.


`amend_scope` widens a hard gate, so HR-03 and HR-04 are `risk: high` and get
the security-engineer pass. The guardrails live in code, not in the overlord
prompt. Notifications and the decisions log carry paths and outcome tokens
only, never file contents.

## 5. Not in this plan

- **Rebase-conflict resolver.** A resolver that merges additive conflicts
  automatically touches code the author did not review; it needs its own
  design (and probably an ADR) before any story.
- **Granting `request_decision` to dispatched agents.** Separate permission
  decision.
- **Making a permanent scope change survive re-ingest.** A re-ingest refreshes
  `files` from the plan file, so an amendment must also be made in the plan.
  HR-04 documents this.

## 6. Ingest-ready plan JSON

```json
{
  "repo_root": "/Users/jessecarroll/git/fagan",
  "epics": [
    {
      "summary": "Parked-story recovery: scope conflicts, transport failures, overlord amend_scope",
      "stories": [
        {
          "key": "HR-01",
          "summary": "Define the scope-conflict marker and teach the scope-gate remedy to use it",
          "risk": "low",
          "files": [
            "pipeline/scope_conflict.py",
            "pipeline/scope_gate.py"
          ],
          "dependencies": [],
          "agent_instructions": "Context: when the scope gate flags a path the brief genuinely needs, the remedy text tells the agent to call `request_decision`, or, if that tool is unavailable, to 'end your turn stating the conflict'. Dispatched agents cannot call request_decision (permission denied), and nothing reads the stated conflict, so the harness counts the empty turn as a crash. This story defines a machine-readable marker; later stories read it.\n\nCreate `pipeline/scope_conflict.py` (pure, no I/O): `SCOPE_CONFLICT_PREFIX = \"SCOPE-CONFLICT:\"`; a frozen dataclass `ScopeConflict(path: str, reason: str)`; `parse_scope_conflict(text: str) -> list[ScopeConflict]`. A marker is one line `SCOPE-CONFLICT: <repo-relative path> :: <reason>`. Rules: at most 5 markers are returned (extras dropped); the reason is stripped, control characters removed and truncated to 200 characters; a marker whose path is empty, absolute, contains a backslash, contains a `..` component or has a NUL is skipped (not an error); duplicate paths keep the first; non-marker lines and a None/empty input return []. Never raise.\n\nIn `pipeline/scope_gate.py`, extend `SCOPE_GATE_REMEDY` by replacing only the sentence 'if that tool is unavailable, end your turn stating the conflict.' with: 'if that tool is unavailable, do not revert; end your turn with one line per path in the form `SCOPE-CONFLICT: <path> :: <why the brief needs it>`.' Keep every other word of the remedy. The existing tests tests/unit/test_scope_gate_remedy_runnable.py and tests/unit/test_scope_gate_nested_test_paths.py pin that the remedy contains `request_decision`, `restore_file`, `last commit`, no destructive git command and not the phrase 'stop and report the conflict'; the new sentence must satisfy all of them. Import the prefix from scope_conflict rather than repeating the literal if that does not create an import cycle; otherwise keep the literal and assert in a test that the two agree.\n\nTests (new file tests/unit/test_scope_conflict_marker.py): one marker parses; five accepted and a sixth dropped; absolute, `..`, backslash, empty paths skipped; reason truncated at 200 and control characters stripped; duplicate path keeps the first; None, empty and marker-free text return []; the remedy contains the marker form and `request_decision`. Test-first, in NEW test files only. Do NOT modify any existing test. Success: new tests plus the full suite pass and `ruff check .` is clean. Line limit: every file stays under 1000 lines (a line-limit test enforces this); the hook files named below have little headroom, so put logic in the new module and keep the hook to a few lines.",
          "persona": "software-engineer"
        },
        {
          "key": "HR-02",
          "summary": "Park a no-commit turn that reports a scope conflict without charging the rework budget",
          "risk": "medium",
          "files": [
            "pipeline/no_progress.py",
            "pipeline/story_status.py"
          ],
          "dependencies": [
            "HR-01"
          ],
          "agent_instructions": "Context: in `pipeline/story_status.py`, in the branch guarded by `if head_res.stdout.strip() == story[\"last_reviewed_sha\"]:` (the Mode 27 no-new-commit guard), the first statements increment `story[\"rework_attempts\"]` and eventually park with 'agent keeps parking/crashing without writing code'. An agent that correctly reports a scope conflict (HR-01 marker) and stops is classified as a crash. story_status.py is ~981 lines, so the hook there must be at most ~6 lines.\n\nCreate `pipeline/no_progress.py` with `classify_no_progress(plan_name, story_key, story, worktree, manifest, manifest_path) -> dict | None`. It reads the last DONE summary with `build_detect._last_done_summary(Path(worktree) / \"agent.log\")`, parses it with `parse_scope_conflict`, and if at least one conflict is found: sets `story[\"scope_conflict\"] = {\"paths\": [...], \"reasons\": {path: reason}, \"recorded_at\": <UTC ISO>}`, sets `story[\"status\"] = \"parked\"` and `story[\"parked_reason\"] = \"scope conflict: <comma-joined paths>\"`, does NOT change `rework_attempts`, writes the manifest atomically, sends one `_notify_user` with `event=\"story_parked\"` (include the correlation id when the story has one), and returns `{\"status\": \"parked\", \"reason\": \"scope_conflict\"}`. With no marker it returns None and touches nothing. Wrap the read in try/except for OSError and log at WARNING (type only, no paths); a failure returns None so the existing behaviour is unchanged. Use the same lazy-import / `_ServerRef` conventions the neighbouring story_status.py code uses for `_notify_user` and `_atomic_write_json`; read `pipeline/dispatch_context.py` for the pattern.\n\nHook in story_status.py: immediately inside the guard, BEFORE `attempts = story.get(\"rework_attempts\", 0) + 1`, call `classify_no_progress(...)` and `return` its result when it is not None. Nothing else in story_status.py changes.\n\nTests (new file tests/unit/test_no_progress_scope_conflict.py), using a tmp worktree with an agent.log containing a `[step 4] DONE:` line: a marker parks the story with the reason, records `scope_conflict`, leaves `rework_attempts` unchanged and notifies once; no marker returns None; an unreadable or missing log returns None; two markers record both paths; an end-to-end test through the story_status guard shows a marker run does not increment `rework_attempts` while a marker-free no-commit run still does. Test-first, in NEW test files only. Do NOT modify any existing test. Success: new tests plus the full suite pass and `ruff check .` is clean. Line limit: every file stays under 1000 lines (a line-limit test enforces this); the hook files named below have little headroom, so put logic in the new module and keep the hook to a few lines.",
          "persona": "software-engineer"
        },
        {
          "key": "HR-03",
          "summary": "Add deterministic guardrails and an executor for an amend_scope triage action",
          "risk": "high",
          "files": [
            "pipeline/triage_scope_actions.py"
          ],
          "dependencies": [
            "HR-02"
          ],
          "agent_instructions": "Context: a story's `files` list is a hard scope gate. When an agent reports a scope conflict (`story[\"scope_conflict\"]`, from HR-02) the overlord should be able to rule that the path belongs in scope. The ruling is advisory; this story builds the code that DECIDES whether the amendment is allowed, so a bad ruling cannot widen scope arbitrarily. Follow the shape of `pipeline/triage_patch_acceptance.py` (`_execute_patch_acceptance(plan_name, story_key, story, ruling, manifest, manifest_path)`).\n\nCreate `pipeline/triage_scope_actions.py` with:\n1. `check_scope_amendment(story: dict, paths: list[str]) -> tuple[list[str], dict[str, str]]` returning (allowed, refusals-by-path with a fixed-token reason). Pure. A path is REFUSED when: the story's `risk` is `high`; the path is already declared; it is absolute, has a backslash or a `..` component; it starts with `scripts/`, `.github/`, `.claude/` or `docs/adr/`, or is a dotfile, `*.env`, `*.plist`, `pyproject.toml`, `requirements*.txt` or `Dockerfile*`; its directory is not equal to the directory of at least one already-declared production file (same package only; new top-level directories are refused); or approving it would make the story's total amendments exceed 3 (count `story.get(\"scope_amendments\", [])` plus the new ones). Test paths are never in play. Everything not explicitly allowed is refused (deny by default).\n2. `_execute_amend_scope(plan_name, story_key, story, ruling, manifest, manifest_path) -> str`. Requires `story[\"scope_conflict\"]`; with none, park with reason 'amend_scope ruled but no scope conflict is recorded'. Runs the guardrail over the recorded paths. If ANY path is refused, amend nothing, park the story (keep it parked) with a reason naming the refusal tokens (never raw reasons or file contents), and return `park_for_human`. Otherwise: append the paths to `story[\"files\"]`; append `{path, reason, ruling_rationale (first 300 chars), at}` entries to `story[\"scope_amendments\"]`; clear review state so the review gate runs on the unchanged branch: pop `review_verdict`, `last_reviewed_sha`, `last_reviewed_logic_fingerprint`, `last_review_findings`, `review_feedback`; pop `scope_conflict`; set `story[\"status\"] = \"tests_passed\"` ONLY when `story[\"last_test_check\"]` has returncode 0 and its `sha` equals the worktree's HEAD, otherwise set `todo`. Write the manifest atomically, send one notification with `event=\"scope_amended\"` listing paths, and record the action in the decisions log the same way the neighbouring executors do. Return a short action string consistent with the other executors.\nAudit: the notification and log must carry paths and outcome tokens only, never file contents.\n\nTests (new file tests/unit/test_triage_scope_actions.py): a sibling file in the same package is allowed; high-risk story refused; `scripts/x.sh`, `.github/w.yml`, `pyproject.toml`, `.env` refused; a new top-level directory refused; `../x.py` and absolute paths refused; a fourth cumulative amendment refused; already-declared path refused; one refused path among allowed ones amends nothing; no recorded conflict parks; success appends to `files`, records `scope_amendments`, clears the five review fields and `scope_conflict`, sets `tests_passed` on a green matching check and `todo` otherwise. Test-first, in NEW test files only. Do NOT modify any existing test. Success: new tests plus the full suite pass and `ruff check .` is clean. Line limit: every file stays under 1000 lines (a line-limit test enforces this); the hook files named below have little headroom, so put logic in the new module and keep the hook to a few lines.",
          "persona": "software-engineer"
        },
        {
          "key": "HR-04",
          "summary": "Wire amend_scope into triage, the overlord policy and the evidence prompt",
          "risk": "high",
          "files": [
            "pipeline/triage.py",
            "pipeline/parsers.py",
            "overlord-policy.md",
            "REFERENCE.md"
          ],
          "dependencies": [
            "HR-03",
            "HR-07"
          ],
          "agent_instructions": "Context: HR-03 added `_execute_amend_scope`. This story makes triage able to choose and run it. pipeline/triage.py is ~946 lines, so the hook must be small. REFERENCE.md is large: edit with anchored `str_replace`, never line numbers.\n\n1. `pipeline/triage.py`: import `_execute_amend_scope` from `triage_scope_actions` next to the existing `_execute_patch_acceptance` import (keep the `# noqa` style). `pipeline/parsers.py` is ALSO in scope: add `\"amend_scope\"` to the `TRIAGE_ACTIONS` frozenset there (around line 36). Without it `_normalize_action` rewrites the ruling to `park_for_human`, so the action never reaches the dispatcher. This one-line edit is pre-authorized; do not revert it. In the action dispatcher (the chain beginning `if action == \"split_story\":`), add `if action == \"amend_scope\": return _execute_amend_scope(plan_name, story_key, story, ruling, manifest, manifest_path)` before the `park_for_human` branch. Add `\"scope_conflict\"` to the evidence key list in `collect_triage_evidence` so the overlord sees the reported paths and reasons. Follow the same autonomy handling the existing manifest-only actions use (`mark_done`, `split_story`, `patch_acceptance`): executed in `gated` and `full`, notify-only in `dry-run`. An unrecognized action still fails closed to `park_for_human`.\n2. `overlord-policy.md`: in the action list under 'Failure triage' add `amend_scope` (the story is parked on a reported scope conflict and the path is directly required by the brief; the executor, not the overlord, enforces the guardrails and refuses anything outside the same package, under scripts/.github/.claude/config files, new top-level directories, high-risk stories, or beyond 3 amendments); add the row 'agent reported a scope conflict (`scope_conflict` recorded) and the path is needed by the brief | `amend_scope`' to the 'Parked-story resolution' table; add `amend_scope` to the gated manifest-only action list in the autonomy paragraph. Update the sentence 'All four ACTION values are defined' only if it is now false; do not reword unrelated text.\n3. `REFERENCE.md`: in the existing triage / overlord actions documentation add a short `amend_scope` entry with the guardrails and the note that a plan re-ingest refreshes `files` from the plan file, so a permanent change belongs in the plan too. Locate the section by unique text first.\nExisting tests that enumerate triage actions or the policy text: run the full suite first; if one pins the old action list or the 'All four' sentence, update only that assertion and state the justification in the commit message (the new action is the requested behavior change). Tests (new file tests/unit/test_triage_amend_scope_wiring.py): a ruling of amend_scope reaches `_execute_amend_scope` with the right arguments; dry-run does not execute; an unknown action still parks; the evidence text includes the recorded scope_conflict paths; the policy file lists `amend_scope` exactly once in the action list. Test-first, in NEW test files only. Do NOT modify any existing test. Success: new tests plus the full suite pass and `ruff check .` is clean. Line limit: every file stays under 1000 lines (a line-limit test enforces this); the hook files named below have little headroom, so put logic in the new module and keep the hook to a few lines.\n\nAUDIT RECORD (decided in review): `_execute_amend_scope` no longer records itself; it leaves `story[\"_amended_paths\"]`. In `pipeline/triage.py`, in the dispatcher that calls `execute_ruling` then `_record_execution`, pop it next to `_split_children` (`paths = story.pop(\"_amended_paths\", None)`; when set, `extra[\"amended_paths\"] = paths`) so the single record carries the real autonomy mode. Add a test that an amend_scope ruling produces exactly one decisions-log record with the autonomy mode and the amended paths.",
          "persona": "software-engineer"
        },
        {
          "key": "HR-05",
          "summary": "Do not charge transport failures to the rework budget",
          "risk": "medium",
          "files": [
            "pipeline/no_progress.py"
          ],
          "dependencies": [
            "HR-02"
          ],
          "agent_instructions": "Context: when a dispatch dies at step 0 with `LLM call failed: Client error '429 Too Many Requests'` (or another transport failure) the agent never ran, yet the Mode 27 guard counts it as a rework attempt and, after two, parks the story as 'agent keeps parking/crashing without writing code'. Live on CL-14 (2026-10-23, two ollama escalation redispatches).\n\nExtend `classify_no_progress` in `pipeline/no_progress.py` (created by HR-02; do not edit story_status.py, the hook already calls it). Add a pure helper `is_transport_failure(log_tail: str) -> bool` that is true only when the current run's log shows an LLM transport failure and NO tool step executed after it: it matches `LLM call failed:` followed by a `429`, `5xx`, `timed out`, `ConnectError` or `ConnectionError` token in the last `[boot]`-delimited segment of agent.log, and is false when any `[step N] <tool>:` line other than step 0's failure line exists in that segment. In `classify_no_progress`, after the scope-conflict check and when `is_transport_failure` is true: increment `story[\"transport_retries\"]` instead of `rework_attempts`, set `story[\"status\"] = \"changes_requested\"` so the scheduler redispatches, write the manifest, notify once with `event=\"transport_retry\"` (token only, no URL or body), and return `{\"status\": \"changes_requested\", \"reason\": \"transport_failure\"}`. When `transport_retries` reaches 5, park with the accurate reason `transport failure: 5 consecutive dispatches never reached the model` and return `{\"status\": \"parked\", \"reason\": \"transport_failure_exhausted\"}`. A successful redispatch that writes a commit is unaffected; `transport_retries` is only read here.\n\nTests (extend nothing; new file tests/unit/test_no_progress_transport.py) with synthetic agent.log segments: a 429 at step 0 with no steps returns the retry result and leaves `rework_attempts` unchanged; a 429 followed by real tool steps in the same segment is NOT a transport failure; a prior segment's 429 followed by a new `[boot]` with real steps is NOT; the fifth consecutive retry parks with the accurate reason; a scope-conflict marker takes precedence; a log with no failure returns None. Test-first, in NEW test files only. Do NOT modify any existing test. Success: new tests plus the full suite pass and `ruff check .` is clean. Line limit: every file stays under 1000 lines (a line-limit test enforces this); the hook files named below have little headroom, so put logic in the new module and keep the hook to a few lines.",
          "persona": "software-engineer"
        },
        {
          "key": "HR-06",
          "summary": "Warn at ingest when a story declares a file with no line-limit headroom",
          "risk": "low",
          "files": [
            "pipeline/scope_headroom.py",
            "pipeline/ingest.py"
          ],
          "dependencies": [],
          "agent_instructions": "Context: CL-14 was declared against `pipeline/dispatch.py` at 999 lines with a 1000-line gate, so any addition forced an out-of-scope split and parked the story. A cheap ingest-time warning catches this class of plan defect.\n\nCreate `pipeline/scope_headroom.py` with `scope_headroom_warning(story: dict, repo_root: str, limit: int = 1000, margin: int = 50) -> str | None`: for each declared production `.py` file in `story[\"files\"]` that exists under `repo_root`, count lines; if `limit - lines < margin`, return one warning string naming each such path with its line count and headroom, and ending 'declare the sibling module the new code will move to, or split the story'. Ignore missing files, non-`.py` files and test paths (use the same test-path helper `pipeline/scope_gate.py` exposes). A story with no `files` returns None. Never raise.\n\nIn `pipeline/ingest.py`, in the per-story loop that already calls `_story_sizing_warning` and `_notify_user(..., event=\"sizing_risk\", ...)`, add a second non-blocking notification: `event=\"scope_headroom\"` with the warning, and a `logging.getLogger(\"pipeline\").warning`. Add at most ~8 lines; ingest.py is ~886 lines. Advisory only: ingest never fails because of it.\n\nTests (new file tests/unit/test_scope_headroom.py) with tmp files: 999-line file warns with headroom 1; 900-line file does not; exactly limit-margin boundary; two files only the full one named; missing file, `.md` file and test path ignored; no `files` returns None; an ingest of a plan declaring a near-limit file emits one `scope_headroom` notification and still succeeds. Test-first, in NEW test files only. Do NOT modify any existing test. Success: new tests plus the full suite pass and `ruff check .` is clean. Line limit: every file stays under 1000 lines (a line-limit test enforces this); the hook files named below have little headroom, so put logic in the new module and keep the hook to a few lines.",
          "persona": "software-engineer"
        },
        {
          "key": "HR-07",
          "persona": "software-engineer",
          "risk": "high",
          "files": [
            "pipeline/triage_scope_actions.py"
          ],
          "dependencies": [
            "HR-03"
          ],
          "summary": "Harden the amend_scope guardrail: protected modules, no root files, NUL, single audit record",
          "agent_instructions": "Context: HR-03 merged `pipeline/triage_scope_actions.py` (`check_scope_amendment`, `_execute_amend_scope`) and passed review, but a human review (2026-10-24) found four defects, confirmed by running the guardrail:\n1. Root-level files are amendable whenever the story declares any root-level file (e.g. REFERENCE.md): `setup.py`, `Makefile` and `CLAUDE.md` came back allowed because their directory `.` equals a declared directory.\n2. The 'same package' rule is effectively `pipeline/`, so safety-gate modules can be amended into scope: `pipeline/scope_gate.py`, `pipeline/branch_guard.py` and the executor module itself were allowed (only the amendment cap stopped them in the probe). That would let a reported 'conflict' widen the gate that polices it (ADR 0004: safety gates fail closed).\n3. A NUL character in a path passes `_is_malformed`.\n4. `_execute_amend_scope` calls `_record_execution(..., \"gated\", ...)` itself, but the triage dispatcher (`pipeline/triage.py`, the function that calls `execute_ruling` then `_record_execution(plan_name, story_key, action, result, PIPELINE_AUTONOMY, ...)`) already records every ruling with the real autonomy mode, so each amendment would be logged twice and once with the wrong mode.\n\nFix, in `pipeline/triage_scope_actions.py` only:\n- Refuse any path whose directory is `.` (root level) with token `root_level_path`, checked before the declared-directory rule.\n- Add `PROTECTED_MODULES`, a frozenset of repo-relative paths that can never be amended: `pipeline/scope_gate.py`, `pipeline/scope_conflict.py`, `pipeline/no_progress.py`, `pipeline/branch_guard.py`, `pipeline/triage.py`, `pipeline/triage_scope_actions.py`, `pipeline/overlord.py`, `pipeline/review_orchestrator.py`, `pipeline/merge.py`, `pipeline/advance_merge.py`, `pipeline/dispatch_lease.py`, `pipeline/pin_guard.py`, `pipeline/security_audit.py`. Refuse them with token `protected_module`. Check it before the declared-directory and amendment-limit rules, so the token reported is the real reason.\n- `_is_malformed` also rejects any control character (ord < 32 or 127), including NUL.\n- Remove the `_record_execution` call and the `_record_execution` `_ModuleRef` from the executor. Instead set `story[\"_amended_paths\"] = list(allowed)` just before returning; the dispatcher (wired in the next story) pops it into the record's extras, as it does for `_split_children`. Do not touch `pipeline/triage.py` in this story.\nExisting tests in tests/unit/test_triage_scope_actions.py: run them first. The record assertion in `test_should_write_manifest_notify_and_record_paths_only` may pin the executor's own `_record_execution` call; if so, change ONLY that assertion to check `story[\"_amended_paths\"]` instead, and state in the commit message that the requested behavior change is moving the audit record to the dispatcher. Do not otherwise modify existing tests; if an existing test uses a path now protected or root-level, change only that path literal to an unprotected sibling such as `pipeline/helper_a.py` and justify it in the commit message.\nNew tests (new file tests/unit/test_triage_scope_actions_hardening.py): `setup.py`, `Makefile`, `CLAUDE.md` refused with `root_level_path` when a root file is declared; each of the 13 protected paths refused with `protected_module` when a sibling is declared; a NUL and a newline in a path refused; a normal sibling still allowed; a protected path among allowed ones amends nothing; success sets `_amended_paths` and makes no `_record_execution` call. Test-first, in NEW test files only. Success: new tests plus the full suite pass and `ruff check .` is clean. Every file stays under 1000 lines (a line-limit test enforces this)."
        }
      ]
    }
  ]
}
```

## 7. Definition of done

- All six stories merged through `approve_merge`; the full suite and ruff pass.
- A scope-conflict marker in an agent's final message parks the story without
  incrementing `rework_attempts`, and an `amend_scope` ruling on an allowed
  path returns the story to review with no extra commit.
- A 429 at step 0 no longer consumes the rework budget.
- A plan declaring a file within 50 lines of the limit emits a
  `scope_headroom` notification at ingest.
