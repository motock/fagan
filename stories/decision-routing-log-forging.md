# Repo issue: an unvalidated plan `owner` is interpolated into the notifications log

Filed 2026-10-29. Found by the security review of `ST-03` (`control-plane-store`),
which blocked on it — but the code is not ST-03's. `pipeline/service_decisions.py`
came from `RT-02` (PR #1374, commit `2402cc7`), which merged to master *during*
ST-03's review cycles. ST-03 never touches the file and does not declare it, so
the dispatched agent could not fix the finding no matter how many rework cycles
it was given; it exhausted its budget and parked. The finding is real and should
be fixed here, on its own story, rather than through a story that cannot reach it.

## Defect

`pipeline/service_decisions.py:32-58` (`_routing_target`) reads `owner` and
`delegates` from the plan manifest and puts them into the routing result with
nothing but `str(...).strip()` and a truthiness check:

```python
owner = manifest.get("owner")
delegates = manifest.get("delegates")
return {
    "owner": str(owner).strip() if owner else LOCAL_OPERATOR.subject,
    "delegates": [str(d) for d in delegates if d] if isinstance(delegates, list) else [],
}
```

No charset or length bound, and non-`str` values are coerced with `str()`. The
value is then interpolated into a free-text notification at
`pipeline/service_decisions.py:138`:

```python
f"see decisions log (routed to {routed_to['owner']})",
```

`pipeline/persistence.py:242-246` (`_write_directly`) appends exactly
`f"{ts} {message}\n"` to `<plan>.notifications.log`, which is read back through
`get_notifications` (`pipeline/service.py:268`, `pipeline/store.py:186`). A
`\n` in `owner` therefore forges an additional line in an append-only,
security-relevant evidence log; CR/ANSI sequences spoof it, and a
multi-megabyte value buries surrounding records.

The ingest path does not close this either: `pipeline/ownership.py:27-29`
validates only that `owner` is a non-empty string, with no charset bound, and
`resolve_ownership` (`pipeline/ownership.py:45`) persists whatever it is handed.

This is the exact class the repo already guards for a weaker input —
`pipeline/actor.py:44-49` rejects an arbitrary inbound correlation id because
"echoing an arbitrary header … into logs is log forging", via
`CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")`. The new sink is
inconsistent with that bar: validate at the boundary, and never put unvalidated
data into a line-oriented log.

## Severity

Defense-in-depth, not privilege escalation. Reaching it needs a manifest whose
`owner` carries the payload: either a local-operator ingest naming it (the
first-ingest rule in `resolve_ownership` is the only way a plan file names a
different owner), or a direct edit of the manifest JSON. Anyone who can edit the
manifest can also append to the notifications log directly, so the marginal risk
is modest — but the routing field is now a second, unvalidated path into an
evidence log, and the module's own docstrings claim the opposite ("the owner is a
name, never the raw target").

## Scope for the fix

1. Validate in `_routing_target` before returning: accept only a subject shape
   matching `^[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}$` (mirroring `CORRELATION_ID_RE`
   at a larger bound), reject newlines, CR and ANSI sequences, and cap both the
   number of `delegates` and each entry's length. On any failure fall back to
   `LOCAL_OPERATOR.subject` — deny-to-safe-default, which is what the same
   function already does for an unreadable manifest.
2. Do not let the fallback change control flow: the notification must still be
   sent, naming the local operator, exactly as it does today for a plan with no
   owner.
3. Consider the same shape bound at ingest in
   `pipeline/ownership.py:ownership_from_plan`, so a hostile `owner` is rejected
   where it enters rather than only where it is rendered. If ingest rejects it,
   the routing fallback above still has to hold for manifests already on disk.
4. Tests, both directions:
   - a hostile owner (`"a\nb"`, `"\x1b[2J"`, a 10,000-character value,
     `{"x": 1}`) routes to `LOCAL_OPERATOR.subject`, and the rendered
     notification is a single line (assert on the message, not just the dict);
   - `delegates` entries are bounded and capped the same way;
   - a valid owner (`"alice"`, `"team@example.com"`, `"ops:oncall"`) is
     unchanged — the guard must not silently downgrade ordinary subjects;
   - an owner that is already `LOCAL_OPERATOR.subject` is unchanged.

## Not in scope

- The review-gate behaviour that put this finding on ST-03: the gate diffed
  against live master, which had gained RT-02's file mid-story. That is a
  separate harness defect.
- `RT-02`'s routing semantics themselves — who a decision routes to, and the
  `match.owner` notification delivery that landed in `f49b743`. Only the
  unvalidated interpolation is at issue here.
