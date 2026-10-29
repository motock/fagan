# Repo issue: a test asserts DK-03's transient pipeline status, so it goes red when the pipeline advances

Filed 2026-10-29. Found while applying DK-03's review feedback: after DK-03 was
re-reviewed and reached `pr_open`, `tests/unit/test_dk03_repo_issue_story.py`
went red on this checkout. Registered as `FU-05` in the
`harness-defect-followups` plan.

## Defect

Two tests in `tests/unit/test_dk03_repo_issue_story.py` assert a *runtime*
pipeline status:

- `test_dk03_is_parked_with_the_exact_reason` — `assert entry.get("status") == "parked"`
- `test_dk03_keeps_its_pr_and_branch` — `assert entry.get("status") == "parked"`, plus
  `entry.get("last_reviewed_sha", "614942d").startswith("614942d")`

Both read `$PLAN_DIR/smoke-stack.manifest.json` through `_dk03_entry()`, which
skips when the manifest is absent. `parked` is a transient state the pipeline is
*supposed* to leave: DK-03 went `parked` → `pr_open` (APPROVE, PR #1380,
`fe87df8`), and both assertions became false for doing exactly the right thing.
The pinned `last_reviewed_sha` prefix goes stale for the same reason.

The module is aware of the problem's cousin but not the problem: its docstring
already documents that `parked_reason` is *not* stable and accepts two shapes
for it, then still pins the status itself.

## Why CI never caught it

The helper skips when the manifest is missing, which is the CI condition. So the
suite is green in CI and red only on a checkout that holds live pipeline state —
a test that fails where nobody runs it in CI and passes where nobody would
notice. That is the same class as `.claude/rules/testing-config-gates.md`:
assert the resolution logic against a fixture, never today's configured value.

Verified 2026-10-29: with DK-03 at `pr_open`, exactly these two tests fail and
the other two in the module pass.

## Scope for the fix

1. Delete the two live-state tests, and the helpers only they use
   (`_dk03_entry`, `PARKED_REASON`, and `_pr_number` if either survivor does not
   use it).
2. Update the module docstring, which currently claims "DK-03 itself is
   ``parked`` (never ``done``)".
3. Keep the two durable tests:
   `test_followup_story_file_exists_with_its_own_key` and
   `test_followup_story_quotes_the_unresolved_base_branch_evidence`. They pin
   the version-controlled record, which is what the module was really guarding.
4. If the intent was to prove triage filed a follow-up, those two tests already
   do it. A future status assertion should drive the transition against a
   synthetic manifest fixture, never read the live entry.

Deleting tests needs the justification this file supplies: the assertions pin a
transient status that the system legitimately changes, and the record they were
guarding stays pinned by the retained tests. It is not a weakening to make a red
build green.

## Not in scope

- `stories/DK-03-repo-issue.md` and DK-03's manifest entry — both correct; only
  the assertions about them are wrong.
- Any other test file, and any replacement assertion that reads live plan state.
