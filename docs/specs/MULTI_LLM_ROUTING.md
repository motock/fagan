# Multi-LLM Routing Policy Spec

Status: shipped, inert by default. The `routing` block in `model_registry.json`
is read by `app.role_registry.resolve_route` and consumed by
`pipeline.usage._route_dispatch_backend` (the `PIPELINE_BACKEND_DISPATCH=auto`
a-priori router). This document is the contract for both sides.

## 1. Schema

A `routing` block sits as a sibling of `providers` and `roles` in
`model_registry.json` (path: repo root, or `PIPELINE_MODEL_REGISTRY_PATH`).
Each key under `routing` is a **role** (e.g. `dispatch`); each role carries:

| Field | Type | Meaning |
| --- | --- | --- |
| `tiers` | map of name → `{provider, model}` | Named backend choices. `provider` must be a key under `providers`; `model` must be a friendly name under `providers.<provider>.models`. The resolved `RouteResolution.model` is that entry's concrete `tag`, never the friendly name. |
| `rules` | list of `{when, tier}` | Ordered, **first-match-wins** predicates evaluated against the story dict. |
| `default_tier` | string | Tier used when no rule matches. Must be declared under `tiers`. |

Worked example (the shipped `routing.dispatch` block, abridged):

```json
"routing": {
  "dispatch": {
    "tiers": {
      "local_default":  {"provider": "local",  "model": "gpt-oss-20b-high"},
      "claude_default": {"provider": "claude", "model": "sonnet"}
    },
    "rules": [
      {"when": {"persona": "security-engineer"}, "tier": "claude_default"},
      {"when": {"max_risk": "low"},    "tier": "local_default"},
      {"when": {"max_risk": "medium"}, "tier": "claude_default"},
      {"when": {"max_risk": "high"},   "tier": "claude_default"}
    ],
    "default_tier": "local_default"
  }
}
```

Rule order matters because `max_risk` is an **inclusive ceiling** (§2): the
`max_risk: "low"` rule must precede `max_risk: "medium"`, or the medium rule
would capture low-risk stories too.

## 2. Supported `when` predicates — exactly two

- `max_risk` — matches when the story's risk is **at or below** the named
  level (`low` < `medium` < `high`). A missing or unknown story risk is
  treated as the **highest** risk (fail closed).
- `persona` — exact string match on `story["persona"]`.

Any other predicate key is a **hard error**: `resolve_route` raises
`RoleRegistryError` naming the exact bad key. It is never silently ignored,
because a silently-skipped predicate makes a rule appear to apply when it
does not. Validation is fail-closed over the **whole block before any rule
evaluates**: an unknown predicate, unknown `max_risk` level, unknown tier, or
a `default_tier` not declared under `tiers` raises immediately, so a typo in
a later rule cannot lurk until the day it becomes the first match.

## 3. Resolution order

`resolve_route(role, story=..., plan_role_config=...)`:

1. **Plan routing** — `plan_role_config["routing"]`, when supplied, beats the
   registry (mirroring how `plan_role_config` beats the registry in
   `resolve_role`).
2. **Registry routing** — otherwise `model_registry.json`'s `routing` block.
3. **None** — a missing `routing` block, or a role absent from it, is *not*
   an error: `resolve_route` returns `None` and the caller keeps its existing
   behavior untouched. The registry is fully optional, like `load_registry()`.

Within a role: evaluate `rules` in order, take the first whose `when` matches;
otherwise `default_tier`; then resolve the tier through the same
provider/model validation `resolve_role` performs.

## 4. Deliberate fail-closed / fail-open asymmetry

The two sides of the policy disagree on error handling **on purpose**:

- `resolve_route` **fails closed**: a malformed block raises
  `RoleRegistryError` (§2). Tests and tooling that ask the registry a question
  deserve a loud answer, not a shrug.
- `_route_dispatch_backend` **fails open**: it wraps the `resolve_route` call
  in `except (RoleRegistryError, NotImplementedError, ValueError)`, logs
  `"dispatch routing policy error (...); using built-in backend selection"`,
  and falls through to the legacy risk-based routing below. This function
  runs on every dispatch tick from `advance_pipeline`, so a config typo must
  degrade to today's backend choice rather than freeze the pipeline — the
  same fail-open-on-config-garbage posture as `_role_resource_ok`'s except
  clause.

## 5. Relationship to the legacy `auto` path

`_route_dispatch_backend` is only consulted when
`PIPELINE_BACKEND_DISPATCH=auto`. When the registry yields a resolution, its
`provider` is returned verbatim — including a claude or third-backend choice
— but **only while the operator has not pinned a runtime ceiling**: a
`PIPELINE_LOCAL_MAX_RISK` set in the environment is an explicit operator
override the static policy block cannot see, so in that case the legacy gate
owns the whole decision. Precedence:

1. **Explicit runtime ceiling** — `PIPELINE_LOCAL_MAX_RISK` set in the env:
   the legacy gate decides (read at call time; owns the persona and
   unwinnable-scope checks too).
2. **Static routing policy** — env unset and the registry resolves: the
   policy's `provider` is returned verbatim.
3. **Built-in default** — env unset and the registry yields `None` (or
   raises, §4): the legacy path applies —

   - risk above `PIPELINE_LOCAL_MAX_RISK` (default `low`) → `"claude"`;
   - `_persona_requires_claude(story)` → `"claude"`;
   - `_story_has_unwinnable_local_scope(story)` → `"claude"`;
   - otherwise `"local"`.

The shipped block mirrors the legacy outcome at the default ceiling
(`low`→local, `medium`/`high`→claude, security persona→claude), so it is
inert: with the env unset it returns the same backend the legacy gate would,
and with the env set it is preempted entirely. It is a **static** policy: it
cannot see a runtime ceiling an operator raises later. To move a story class
off Claude, change the block's rules — do not raise
`PIPELINE_LOCAL_MAX_RISK`, which switches the whole dispatch back to the
legacy gate.

## 6. Safety overrides that still apply AFTER routing

Routing policy output is not final. `pipeline.dispatch._resolve_dispatch_backend`
applies two overrides **after** the router (both only when the story had no
explicit `backend`, so a prior escalation flip always wins as-is):

1. **Security-persona override** — `_persona_requires_claude(story)` forces
   `"claude"` regardless of dispatch mode. A routing rule cannot send a
   security story to a local backend; the override re-asserts Claude after
   the policy has spoken.
2. **Unwinnable-scope override** — `_story_has_unwinnable_local_scope(story)`
   forces `"claude"` for repo-wide, unscoped lint/fix sweeps (Mode 40
   retro #4).

So the policy is a way to route *ordinary* stories between backends; it is
not a way to route a security story — or an unwinnable sweep — off Claude.
