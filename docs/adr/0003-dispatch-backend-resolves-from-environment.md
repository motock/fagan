# 0003. Dispatch backend resolves from the environment, not the registry

- **Status:** Accepted
- **Date:** 2026-09-11

## Context

`role_registry.resolve_role` is the authoritative resolver for roles such as
review and overlord, and reads `roles.<role>` from the model registry. It is
natural - and wrong - to assume it also governs which backend actually executes
a dispatched story.

## Decision

Per-story dispatch execution resolves its backend from the
`PIPELINE_BACKEND_DISPATCH` environment variable, defaulting to `claude`, with
`auto` delegating to the routing table. The registry's `roles.dispatch` entry
feeds only advisory consumers: dashboard display and decompose-time story
sizing.

## Consequences

Auditing "what will actually run" requires reading the execution call site
(`pipeline/dispatch.py`, `pipeline/advance.py`, `pipeline/escalation.py`), not
the resolver. The two answer different questions and can legitimately disagree.

This is easy to get backwards, and was: a 2026-09 change routed a preflight
check through `resolve_role("dispatch")` on the assumption that the registry was
authoritative. The registry said `ollama` while dispatch actually ran `claude`,
so preflight reported a provider that would never be invoked - a false green in
the opposite direction from the one it was added to prevent. It was reverted.
Any check claiming to report the effective dispatch backend must mirror the
execution chain exactly.

Cost: two resolution paths that must be understood separately, and a standing
trap for anyone who learns one and assumes the other. The mitigation is this
record plus the comment at the preflight check itself.