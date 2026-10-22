# Plan: `PIPELINE_AUTO_MERGE` opt-out flag — human-in-the-loop merges

> Authored 2026-10-21. **Not ingested.** Persisted here to revisit later.
> Status: drafted, pending decomposition review and `ingest_plan`.

## Problem

A project lead who wants a human to review every open PR before it lands has
no process-wide way to keep the pipeline fully automatic **except** merging.
Today:

- `PIPELINE_AUTONOMY=gated` + `PIPELINE_RISK_THRESHOLD` still merges any
  story at or below the threshold unattended (default `low` −> low-risk PRs
  land on green CI with no human look).
- `PIPELINE_AUTONOMY=dry-run` parks all merges but also stops dispatch and
  every other action — it is a full stop, not a merge-only gate.
- Marking every story `risk: "high"` in each plan is the only workaround, and
  it pollutes risk semantics (every park reason reads "high risk held for
  human review" even for trivial stories).

## Proposed design

Add one env var: **`PIPELINE_AUTO_MERGE`** (default `1`, matching the
current behavior; set to `0` to require a human merge for every story).

Insertion point: `pipeline/merge.py::_merge_decision`, immediately after the
`dry-run` check and before all risk handling:

```python
if not PIPELINE_AUTO_MERGE:
    return {"action": "park", "reason": "auto-merge disabled (PIPELINE_AUTO_MERGE=0)"}
```

Semantics and deliberate choices:

- Parks **every** otherwise-mergeable story regardless of autonomy level —
  in `full` mode high-risk stories must also park directly (no overlord
  adjudication): the operator has asked for a human merge, so adjudication
  is moot. Fail-secure: an unset/odd value keeps today's behavior, which is
  the default install's behavior anyway.
- **Does not touch `approve_merge`** — the explicit human merge path never
  consults `_merge_decision` (verified in `_approve_merge_impl`), so the
  human loop under this flag is: pipeline parks each PR −> `story_parked`
  notification already fires with the reason −> human reviews the PR −>
  human runs `approve_merge` (full rebase + CI + acceptance re-run) or
  merges by hand.
- No re-adjudication concern: `_readjudicate_parked_merge_hold` only
  re-examines the exact "high risk held for human review" reason under
  `full` autonomy, so these parks stay parked until the human acts.
- Naming follows the sibling opt-out flags `PIPELINE_MERGE_CI_GATE` /
  `PIPELINE_MERGE_BUILD_GATE`.
- The parked notification embeds the PR URL (confirmed 2026-10-21).
  Implementation is a message change in
  `pipeline/advance_merge.py::_adjudicate_merges`'s park branch — append
  `story["pr_url"]` to the `story_parked` notice text when present. This
  covers every park reason (high-risk holds included), which is the point:
  a human about to review a held PR needs the link. `_notify_user`'s fixed
  kwargs (`persistence.py:201`) are deliberately NOT extended — the
  `# Forward the optional dispatch context` loop and the record/bus payload
  shapes stay untouched; the URL rides in the free-text message that the
  log, record and bus payload already carry.

## Implementer tier assumption

Assumed **Claude-class** for both stories. If this is ever retargeted at a
non-Claude tier: story 2 edits `REFERENCE.md` (~1330 lines), which is **not
a weak-tier file** (`agent-dispatch-story-sizing.md` + preflight rule 4);
route it to cloud open-source or Claude, and run the full local-dispatch
preflight on both stories before ingesting.

## Plan payload (for `save_plan` / `ingest_plan` when revisited)

```json
{
  "repo_root": "/Users/jessecarroll/git/fagan",
  "epics": [
    {
      "summary": "Human-gated merges: PIPELINE_AUTO_MERGE opt-out flag",
      "stories": [
        {
          "key": "AUTO-MERGE-FLAG-1",
          "summary": "Add PIPELINE_AUTO_MERGE off-switch to the unattended merge gate",
          "risk": "medium",
          "files": ["pipeline/config.py", "pipeline/merge.py", "pipeline/config_provenance.py", "pipeline/advance_merge.py"],
          "agent_instructions": "Add a new env var PIPELINE_AUTO_MERGE (default \"1\"): read it in pipeline/config.py alongside PIPELINE_AUTONOMY (pattern: os.environ.get(\"PIPELINE_AUTO_MERGE\", \"1\") != \"0\"), export it in that module's __all__, and add an EnvVarSpec(\"PIPELINE_AUTO_MERGE\", \"1\") entry in pipeline/config_provenance.py. In pipeline/merge.py::_merge_decision, insert immediately after the PIPELINE_AUTONOMY == \"dry-run\" check and BEFORE any risk/threshold handling: `if not PIPELINE_AUTO_MERGE: return {\"action\": \"park\", \"reason\": \"auto-merge disabled (PIPELINE_AUTO_MERGE=0)\"}`. NOTE: merge.py reads PIPELINE_AUTONOMY from pipeline.server via `from .server import ...` inside the function — import PIPELINE_AUTO_MERGE the same way so test monkeypatching on pipeline.server keeps working; also re-export the name from pipeline/server.py's import block (it already imports from pipeline.config) so that import resolves. SECOND CHANGE (same PR): in pipeline/advance_merge.py::_adjudicate_merges' park branch (the `_notify_user(plan_name, f\"{key} parked: {decision['reason']}\" ...)` call), append the story's PR URL to the message when present, e.g. `f\"{key} parked: {decision['reason']}\" + (f\" — {story['pr_url']}\" if story.get(\"pr_url\") else \"\")`, so a human reviewing a held PR gets the link. Do NOT extend _notify_user's kwargs. TDD: write the failing tests FIRST in a new test module following the repo's existing _merge_decision test style. Tests to write (positive AND negative): (1) flag off + APPROVED + risk=low + autonomy=gated -> action park with reason \"auto-merge disabled (PIPELINE_AUTO_MERGE=0)\"; (2) flag off + autonomy=full + risk=high -> park directly WITHOUT invoking the overlord adjudication path (assert _adjudicate_high_risk_merge is never called and no adjudication record is appended); (3) flag off + not-APPROVED story -> existing \"not approved\" park still wins (flag must not mask the verdict); (4) flag unset/default + risk=low -> still merges (regression guard on today's behavior); (5) flag off must NOT affect _approve_merge_impl: the human merge path must still be able to merge (assert _merge_decision is not consulted on that path, or drive _approve_merge_impl with the flag off and a suitable stub); (6) park branch with a story carrying pr_url -> the emitted story_parked notification message contains that URL; (7) park branch with no pr_url -> message unchanged and no dangling separator. Do NOT modify any existing test; the new behavior is strictly additive and today nothing asserts on PIPELINE_AUTO_MERGE. Success criterion: `.venv/bin/python -m pytest -q` passes and `.venv/bin/python -m ruff check .` is clean."
        },
        {
          "key": "AUTO-MERGE-FLAG-2",
          "summary": "Document PIPELINE_AUTO_MERGE in README and REFERENCE",
          "risk": "low",
          "dependencies": ["Add PIPELINE_AUTO_MERGE off-switch to the unattended merge gate"],
          "files": ["README.md", "REFERENCE.md"],
          "agent_instructions": "Document the new PIPELINE_AUTO_MERGE env var. In REFERENCE.md: add a row to the env-var table near PIPELINE_AUTONOMY / PIPELINE_RISK_THRESHOLD (around the \"| `PIPELINE_AUTONOMY` | `gated` ...\" row, search for PIPELINE_RISK_THRESHOLD to anchor the insertion) in the same table format: `| `PIPELINE_AUTO_MERGE` | `1` | Set to `0` to park every reviewed PR for a human merge; the pipeline keeps dispatching and reviewing, merges happen only via `approve_merge` or by hand |`. Also add one bullet to the \"Autonomy levels:\" list beneath: `PIPELINE_AUTO_MERGE=0 overrides every level above at the merge gate only — nothing else is gated.` In README.md: append one sentence to the section around line 249 (\"Watch it merge — with PIPELINE_AUTONOMY=gated ...\") explaining that `PIPELINE_AUTO_MERGE=0` keeps everything automatic except merging, with merges done by a human via `approve_merge`. REFERENCE.md is a cumulative doc with verbatim-section tests; insert the table row and bullet as NEW lines between stable anchors, and do not edit any other line. One small test only (locate the new row by the unique text PIPELINE_AUTO_MERGE and assert its cells), consistent with doc-only change scope."
        }
      ]
    }
  ]
}
```

## Open questions — resolved 2026-10-21

1. Flag name: settled — **`PIPELINE_AUTO_MERGE`** (opt-out shape matching
   the sibling `PIPELINE_MERGE_*_GATE` flags).
2. Unattended merge paths outside `_adjudicate_merges`: verified none
   2026-10-21; re-verify at ingest time if the merge module has grown.
3. PR URL in the park notification: confirmed yes — implemented as part of
   story 1 (message change in `_adjudicate_merges`, tests 6-7 above); the
   parked notice will read
   `<key> parked: auto-merge disabled (PIPELINE_AUTO_MERGE=0) — <pr_url>`.
