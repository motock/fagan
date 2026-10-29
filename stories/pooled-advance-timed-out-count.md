# Follow-up: the pooled advance pass miscounts `timed_out`

Filed 2026-10-29, after `TP-02` merged (`agent/tp-02`, PR #1371). Found while
verifying TP-02 before its merge; TP-02's pooling behaviour is correct, so this
is a separate, small story rather than a rework of that one.

## Defect

`pipeline/advance_pool.py` counts the pass's `timed_out` value as:

```python
timed_out = sum(
    1 for result in results.values()
    if isinstance(result, dict) and result.get("skipped") == "deadline"
)
```

That counts plans that were **skipped** because no time remained in the pass
(`{"ok": True, "skipped": "deadline"}`), and counts **nothing** for a plan whose
child was actually killed at its own deadline. A real timeout is reported by
`advance_plan_subprocess` as `{"ok": False, "error": "advance timed out",
"timed_out": True}`, which this predicate never matches.

`_log_pass`'s own docstring says the counts are "what an operator needs to tune
the pool" and that `grep 'timed_out='` "finds every truncated pass". A plan cut
off by `PIPELINE_ADVANCE_PLAN_TIMEOUT_SECONDS` truncates exactly as much as one
skipped for the window, and it is the case an operator tuning that timeout most
needs to see. So the field misreports on the knob it exists to inform.

Worse in combination: the two truncations want **different** knobs. A
`skipped: deadline` plan means the pass window is too small (the reconcile
join timeout); a `timed_out: True` plan means the per-plan budget is too small
(`PIPELINE_ADVANCE_PLAN_TIMEOUT_SECONDS`, default 600). Collapsing them, or
reporting only one, points the operator at the wrong knob.

## Why no test caught it

`tests/unit/test_advance_all_plans_pooled.py` asserts `timed_out=0` on an
all-success pass, and its deadline test asserts only the `skipped: "deadline"`
result payloads. Nothing drives a `{"timed_out": True}` result through
`advance_all_plans_pooled`, so the counting predicate is never exercised on the
case it gets wrong.

## Scope for the fix

1. Count the two truncations separately, and name them for what they are —
   e.g. `skipped=<n> timed_out=<n>` in the message, with both as structured
   `extra` fields. Keep `plans=`, `workers=`, `elapsed_s=` unchanged so existing
   greps keep working.
2. Add the missing negative cases in `tests/unit/test_advance_all_plans_pooled.py`
   by stubbing `advance_plan_subprocess`:
   - one plan returns `{"ok": False, "timed_out": True}` → counted as a timeout,
     not as a skip, and the pass still reports the other plans normally;
   - one plan is skipped for the deadline and another genuinely times out →
     both counters are `1`, which is the case that currently reports `1`;
   - an exception-derived failure (`{"ok": False, "error": ...}` with no
     `timed_out` key) increments neither counter.
3. Confirm the count is per-pass and resets each pass (it is recomputed inside
   `advance_all_plans_pooled`, so a test that runs two passes back to back pins
   that it does not accumulate).

Do not change the result payloads the function returns: `advance_plan_subprocess`
already reports the distinction faithfully, and callers may read `timed_out`
per plan.

## Not in scope

- TP-02's pooling, rotation, deadline arithmetic, privilege gating or child
  isolation — all verified correct.
- `PIPELINE_MERGE_REVERIFY_DETACHED`, the stale-bytecode false pass
  (`stories/merge-reverify-stale-bytecode.md`) and the 20-node concurrency flake
  (`stories/OT-03-repo-issue.md`). Separate harness defects.
