# Follow-up: the `advance_all_plans` dashboard route answers 400 for a refusal

Filed 2026-10-29. Found on `BR-01`'s branch, where a dispatched agent had
authored the fix in an out-of-scope commit (`055032a`) — `app/dashboard_routes_plan.py`
is not in BR-01's declared `files`, so that commit was dropped when BR-01 was
rebuilt on current master. The defect itself is real and still present.

## Defect

`app/dashboard_routes_plan.py` — every plan-control route calls
`app.dashboard_refusal.raise_if_forbidden` on the service result: `save_plan`
(:37), `ingest_plan` (:59, :62), `advance_pipeline` (:72), `checkpoint` (:97).
The one exception is the fleet-wide route:

```python
def advance_all_plans() -> dict[str, Any]:
    """Delegates to _service.advance_all_plans()."""
    result = _service.advance_all_plans()
    if not result.get("ok"):
```

A refusal dict is `{"forbidden": True, "action": ...}` with **no `ok` key**, so
`not result.get("ok")` is true and the refusal is converted into the generic
400 "Unknown error" branch instead of 403. A caller that is not authorized to
advance every plan is told its request was malformed.

This is the same shape as the defect the `save_plan` route already had and had
fixed (see `tests/unit/test_plan_authoring_enforcement.py`'s
`test_dashboard_save_route_answers_403_for_a_refused_caller`, and its docstring
line: "the dashboard `save` route answers 403 for a refusal BEFORE the JSON
branch"). The advance routes were never given the same treatment.

## Why it matters

The status code is the contract a caller reads. A 400 sends an operator looking
for a malformed request; a 403 tells them to fix permissions and names the
action. Both routes already return the refusal's `action` elsewhere, so the
information exists — this route discards it. It also diverges from
`advance_pipeline` one function above, which does it correctly.

## Scope for the fix

1. Call `raise_if_forbidden(result)` in `advance_all_plans` before the
   `result.get("ok")` branch, matching `advance_pipeline` and the siblings.
   Keep the existing 400 path for a genuine non-`ok` result that is not a
   refusal.
2. Tests, both directions, in the enforcement test file:
   - a bound non-operator caller gets **403**, the body names the refused
     action, and neither the service's advance work nor any dispatch happens
     (assert the service double was not called, or that no plan advanced);
   - an allowed caller with a genuinely failing advance still gets whatever
     status that path already returns — the refusal branch must not swallow it;
   - `advance_pipeline` keeps its current 403 behaviour (regression guard, since
     it is the sibling this one is being made to match).
3. Ordering: the refusal check must run **before** any work, not after — the
   same "403 before 400" ordering the `save` route pins.

## Not in scope

- BR-01's `EventBus.delivers` work, which is unrelated and merging separately.
- Any change to `pipeline/authorization.py` or the refusal dict shape; the
  policy already denies correctly, the route just reads the answer wrong.
