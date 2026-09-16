# Reviewer brief — the two parked `chat-worktree-apply` PRs

> Prepared 2026-09-15 for human review of **PR #760 (WAP-7)** and **PR #761 (WAP-9)**.
> Both are parked at `risk: high`. Neither should be merged or closed on the
> strength of the park reason alone — see "What the park reasons got wrong".

## Why these two are parked

The high-risk merge gate held both, citing missing PR checks. That hold was
correct as a *policy* action (high-risk, security-relevant surface) but it was
made on partly wrong evidence, and the park reason on record has since been
overwritten twice by a triage bug, so the manifest no longer says anything
useful about either PR.

**Current measured state (verified 2026-09-15):**

| | PR #760 (WAP-7) | PR #761 (WAP-9) |
|---|---|---|
| State | OPEN, `MERGEABLE`, `CLEAN` | OPEN, `MERGEABLE`, `CLEAN` |
| CI | 5/5 green (lint + py3.12/3.13/3.14 ubuntu + 3.14 macos) | 5/5 green (same matrix) |
| review verdict | `APPROVE` | `APPROVE` |
| Branch vs master | 8 commits, +1802/−4 across 5 files | 4 commits, +1053 across 4 files |
| Manifest `parked_reason` | `triage attempts (2) at or above cap (2)` | same |

Neither branch has been rebased onto current master, and `gh pr checks` may need
a fresh run before merge; the gate rebases and re-polls on its own.

## What the park reasons got wrong

Three pieces of evidence the overlord ruled on do not survive checking. Two of
them understate the branches, which is the dangerous direction:

1. **"branch shows 0 new commits vs main … nothing is mergeable regardless."**
   False. Both branches carry substantial work (table above). The
   `_worktree_has_new_commits` probe returns `True` for both worktrees today.
   This claim was used as *reinforcing* evidence in the rulings on WAP-7 and
   WAP-9; the holds do not rest on it alone, but a reviewer reading only the
   decision log would conclude there is nothing here to review.
2. **"zero PR checks" (WAP-9) / "no PR checks" (WAP-7).** Unsupported at park
   time. Before the park, #761 had green runs at 05:28Z and 05:44Z, and #760
   at 06:00Z. The gate said `proceed` at 06:07:50Z (#761) and 06:16:21Z
   (#760), then `park` 74s and 57s later on the same change, citing absent
   checks — same PRs, opposite rulings.

   The recorded snapshot is `merge_park_evidence = {"pr_checks": null}`. That
   `null` means the gather wrote nothing, and **the gate cannot say why**: the
   prompt renders a failed gather and a genuine "no checks" as the identical
   string `PR CHECKS: (none)` (`pipeline/merge.py`), and
   `_populate_pr_checks_once` swallows every exception silently. Re-running the
   same gather against the live WAP-9 story today returns
   `{'state': 'pass', 'error': ''}`, so the trigger is not reproducible from the
   current code. What *is* established: the overlord asserted "no PR checks" as
   fact while the pipeline only knew it could not read them. That is the defect
   filed as TRIFID-4; it does not change your review, but it does mean the park
   reason is not evidence about these PRs either way.
3. **WAP-9 "edited a pre-existing test assertion"**
   (`test_chat_origin_proposes_and_stores_the_record`) — does not exist on
   master. That test lives only in WAP-9's own **new** file
   `tests/unit/test_propose_patch_route.py:402`. WAP-9 modifies no existing
   test. This justification point is fabricated and should carry no weight.

By contrast, the rulings' *substantive* concerns are real and are what this
brief asks you to check.

---

## PR #760 — WAP-7: the apply engine

**What it builds.** `pipeline/worktree_patch.py` `apply_patch` (+263): the
component that writes into a story's worktree and enforces the deny-list
protecting `CLAUDE.md`, `.mcp.json`, `.git/config`, `agent.log` and friends.

Reviewed design, as written (all verified in the diff):

- Ordered gate list — plan lock around the **whole** body (so the 60s tick can't
  redispatch mid-apply), manifest read, stuck-only gate, HMAC confirmation
  token + single-use status, worktree-is-a-directory, path resolution, then
  `git apply --check`, then `git apply`, then flip the record to `applied` on
  success only. A failed apply leaves the record pending (retryable).
- `git apply` is invoked with an argv list, no shell, no `--3way`.
- Deny list applied to **both** the new-side and old-side hunk paths.

**Two real defects this branch fixes in already-merged code:**

- **Old-side bypass.** Master's `is_denied_relative_path` denies on
  `components[-1]`, but the OLD side of a file block (`--- a/CLAUDE.md`) was
  never passed to it. A patch that rewrites or deletes a denied file via the
  old side could slip through the predicate.
- **C-quoted path bypass.** Master's predicate compares the final component by
  exact equality and does no C-unquoting. A git-quoted spelling whose final
  component is `CLAUDE.md"` is not equal to `CLAUDE.md`, so it evaded the deny
  list while `git apply` C-unquotes the header and writes the real `CLAUDE.md`.
  The branch adds `_c_unquote` and runs the deny check on the unquoted spelling,
  and fails closed on a surviving quote character in
  `pipeline/workspace_fs.py`.

**What to verify:**

1. **The out-of-scope edit — `pipeline/workspace_fs.py` (+8, a quote-character
   failure).** This is the item the triage ruling called out as drift. It is
   *not* test-gaming: it is a fail-closed hardening of the shared read-path
   safety layer and it is directly motivated by the C-quote bypass above. But it
   was outside the story's assigned scope and it changes behavior for every
   read-path caller in the repo. Confirm (a) no legitimate caller can pass a
   path containing `"`, and (b) master today genuinely lacks this guard — it
   does; `git show origin/master:pipeline/workspace_fs.py` has control-char,
   backslash, absolute and `..` checks, but no quote check.
2. **The commit sequence, not just the final diff.** The branch interleaves
   three `WIP (parked on off-task drift)` checkpoints with the real work. Read
   the sequence before the diff — the drift guard tripped mid-run.
3. **Test weight.** ~1535 lines of new tests (919 + 314 + 302). Confirm the
   deny-list and C-unquote tests exercise the *resolver entry point*, not the
   predicate in isolation, and that the old-side case has a negative test.
4. **Symlink semantics.** `resolve_write_target` refuses a symlink even when
   every hop stays inside the worktree. Confirm that is intended for the
   create-file case.

**Salvage note.** Even if you reject the branch wholesale, the two bypasses
above are unlanded fixes to *merged* code. They should not be dropped silently.

---

## PR #761 — WAP-9: the propose route and chat tool

**What it builds.** `POST /api/worktree/patch/propose`
(`app/dashboard.py` +113), `ProposePatchRequest`
(`app/dashboard_models.py` +12), the `propose_patch` chat tool
(`app/chat.py` +8), and ~920 lines of tests.

Reviewed design, as written:

- **Chat-reachable by design.** The origin gate is an allow-list
  (`chat | ui`) — proposing is how the model hands work to a human — while the
  *apply* routes are UI-only. This is the deliberate opposite of WAP-3, which
  made `POST /api/plans/{plan}/ingest` **refuse** chat origin.
- **The confirmation token is withheld from any non-UI origin.** A chat-origin
  caller still creates the record (retrievable by patch id) but gets a
  token-free envelope, because `app/chat.py` returns the JSON straight to the
  model.
- Stuck-only (409 for `in_progress`/`running`), worktree-must-exist, diff
  validation via `validate_for_propose`, and absolute worktree paths redacted
  out of error details before they reach the caller.

**What to verify:**

1. **The asymmetry with WAP-3 is intended and safe.** One route on the same file
   refuses chat origin; the adjacent one allows it. Confirm the token-withholding
   is what makes admitting chat safe, and that nothing else on the record is
   sensitive.
2. **The token's disclosure surface is complete *today* but not durably.**
   There is currently no route that returns a stored patch record —
   `get_patch_record` has no production caller, and no GET route exists. So
   withholding the token at propose is sufficient right now. This is a
   **forward-looking invariant**: when WAP-10 lands the UI-only GET/apply routes,
   they must require `ORIGIN_UI`, or the withheld token becomes reachable by
   the constrained party. Worth a comment or a test pinning it, since the
   guarantee is currently incidental.
3. **`validate_for_propose` and the deny list.** The route docstring states the
   deny list is apply-side only — a patch touching `.git` is accepted at propose
   so a human can inspect what the model tried. Confirm that is the intended
   reading of the plan doc, and that the apply side really is the enforcement
   point (WAP-7, above).
4. **Test weight.** ~920 lines for one route. Confirm the 403/404/409/413/400
   paths are each asserted, and that the token-absent assertion is on the
   *response body* (not on an internal call).
5. **Naming Nit.** The repo already has
   `POST /api/plans/{plan}/stories/{key}/patch` (`app/dashboard.py:909`), which
   means "patch the story record" — unrelated to this diff-concept. Not
   blocking; check the dashboard UI won't confuse the two.

---

## Decision points for the human

1. **WAP-7's `pipeline/workspace_fs.py` edit** — accept, reject, or split it into
   its own reviewed change? It is a real hardening on a shared safety layer, made
   out of scope.
2. **Does WAP-9's chat-reachable propose route + token withholding satisfy you as
   a security boundary**, given the apply side is still UI-only and not yet
   landed?
3. **Should either be rebased and re-run through the gate?** If you'd rather not
   merge either, note that WAP-7 and WAP-9 gate six downstream stories
   (WAP-8, 10, 11, 12, 13, 14). Closing them stalls the rest of the plan.
4. **The harness bugs that obscured this review** are now filed as the
   `triage-evidence-fidelity` plan (TRIFID-1..4): the missing `park_for_human`
   handler that clobbers `parked_reason`, the per-tick cap path that clobbers it
   a second time, and the unreadable-CI evidence defect. None of them changes
   the substance of what you decide here — but note that TRIFID-2 stops triage
   from touching merge holds, which is what makes these two stories
   *re-adjudicable* again once their `parked_reason` is restored. Restoring it
   is deliberately left as an explicit human step (see the decision point
   above), because `PIPELINE_AUTONOMY=full` means the gate could then merge
   them unattended.
