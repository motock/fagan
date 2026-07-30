# Always-on checklist (guided decomposition) and TDD-split

**Status:** proposed 2026-07-20, revised same day to unify the planner's
provider/model resolution. Not yet implemented.
**Scope:** remove the on/off *toggles* for the two tech-lead features so they
are always enabled; for the planner, replace the `cloud`/`local` mode split
with a unified provider+model configuration (default `ollama/glm`). This is a
flag-removal + resolution-unification change, not a redesign of either
feature's mechanics.

**Decisions approved by the operator 2026-07-20:**
- **Commit the repo plist** (§10 option (a)) — the `PIPELINE_DECOMPOSE` /
  `PIPELINE_DECOMPOSE_CLOUD_MODEL` / `PIPELINE_TDD_SPLIT` keys are removed from
  the repo `launchd/…plist` in §4 and committed, so the glm default is durable
  and a future `cp` + reload no longer silently reverts to Claude.
- **Existing-test edits approved** (CLAUDE.md Step 4) — the existing tests that
  encode the now-removed `off`/`unset`/`cloud` behavior (listed in §5) may be
  modified, not just added to.

## 1. Goal

Both features have been validated in production over the last several sessions
(guided decomposition: PR #132/134/135/136/137; TDD-split: PR #150 and the
`project_tdd_split_experiment_result` memory). The operator no longer wants to
set `PIPELINE_TDD_SPLIT=on` / `PIPELINE_DECOMPOSE=cloud` to turn them on, and
no longer wants the ability to turn them off. Per the user's 2026-07-20
directives:

> I want just the toggle for on/off removed for both planner and tdd_split.
> Should always be enabled after this change. Keep the configurations to set
> the provider/model but the enabled/disabling I want gone.

> For decomp I want to be able to specify the provider (ollama vs lmstudio vs
> mlx etc.) as well as the model (sonnet vs glm vs etc.). The default for now
> should be ollama/glm.

So: **the enable/disable toggle is removed for both; both are always on. The
planner's provider AND model are independently configurable, defaulting to
`ollama/glm`.** TDD-split's `test_author` role already resolves provider+model
via the registry and is unchanged in its resolution (only its on/off flag is
removed).

## 2. Key findings to record before changing anything

**Finding A — verify the LOADED scheduler, not the repo plist.** The repo file
`launchd/com.claude.pipeline.advance-scheduler.plist` shows
`PIPELINE_BACKEND_REVIEW=claude` and `PIPELINE_DECOMPOSE=cloud`, which would
mean the planner and reviewer run on Claude. **That file is stale.** The
actually-loaded scheduler (verified 2026-07-20 via `launchctl print
gui/$(id -u)/com.claude.pipeline.advance-scheduler`) runs:

```
PIPELINE_BACKEND_DISPATCH    => local
PIPELINE_LOCAL_MODEL_DEFAULT => gpt-oss:20b
PIPELINE_BACKEND_REVIEW      => local       # NOT claude — repo file is stale
PIPELINE_LOCAL_REVIEW_MODEL  => glm-5.2:cloud
PIPELINE_DECOMPOSE           => local       # NOT cloud — repo file is stale
PIPELINE_DECOMPOSE_CLOUD_MODEL => sonnet    # inert under =local
PIPELINE_TDD_SPLIT           => on
```

This matches the 2026-07-20 deployment recorded in the
`project_scheduler_env_overrides_mcp_env` memory (lines 88-101): review and
decompose were moved Claude→local/glm that day and live-validated (PR #143
reviewed by glm). The repo plist was never committed with that edit (same
footgun the memory's 2026-07-18 postscript describes — "config is NOT durable
in git"). **Lesson re-learned: always `launchctl print` the loaded scheduler;
the repo plist is not authoritative for what production actually runs.**

**Finding B — the actual production 4-role config.** Under the loaded env
above, `_LOCAL_BACKEND_NAMES = {"local","ollama","lmstudio","mlx"}`
(`pipeline/config.py:152`), so the local-family overrides fire:

| Role | Production model today | How |
|---|---|---|
| implementer (dispatch) | ollama / gpt-oss:20b | `PIPELINE_LOCAL_MODEL_DEFAULT` |
| test_author (TDD-split) | ollama / glm-5.2:cloud | `PIPELINE_TDD_SPLIT=on` + registry `roles.test_author` |
| planner (checklist) | ollama / glm-5.2:cloud | `PIPELINE_DECOMPOSE=local` + registry `roles.planner` (the `mode="local"` branch honors the pin) |
| reviewer | ollama / glm-5.2:cloud | `PIPELINE_BACKEND_REVIEW=local` + `PIPELINE_LOCAL_REVIEW_MODEL` override (local-family gate fires) |

So all three tech-lead roles are already on glm; only the implementer is
gpt-oss. An earlier session's "4-role config: planner=glm, reviewer=glm,
test_author=glm, implementer=gpt-oss" was **correct** — I briefly doubted it by
reading the stale repo plist, then re-verified via `launchctl print`. **This
plan does NOT change production's model assignment** (see §6); it only removes
the on/off and cloud/local toggles, codifying the current loaded config as the
always-on default.

## 3. Precise scope — what is removed, what stays

### 3.1 TDD-split (`PIPELINE_TDD_SPLIT`)

`PIPELINE_TDD_SPLIT` is *purely* an on/off toggle (`off | on`, default `off`).
It has no mode dimension. Removing the toggle therefore means **removing the
env var entirely** — there is nothing else for it to select.

- **Removed:** the `PIPELINE_TDD_SPLIT` env var and its single read site at
  `pipeline/server.py:987`.
- **Stays:** the per-story `story["tdd_split"]` opt-in field. This is a
  plan-authoring field, not a global on/off flag, and the user's directive was
  specifically about the on/off toggle. The phase still requires
  `story["tdd_split"]` truthy (so a docs-only story with no tests to author can
  still opt out by leaving it unset). The same-model refusal in
  `_resolve_test_author_backend` (`pipeline/planner.py:287-293`) stays as the
  safety net — an unconfigured or same-model-as-dispatch `test_author` role
  still returns `(None, None)` and the phase skips, exactly as today.
- **Stays:** the `not resuming` and `test_author_marker.exists()` idempotency
  guards (server.py:992-993). Reworks still act on the same committed tests;
  they never get a fresh test-authoring pass.
- **Stays:** all provider/model config for the `test_author` role
  (`PIPELINE_BACKEND_TEST_AUTHOR`, plan `role_config.test_author`,
  `model_registry.json` `roles.test_author`). The `test_author` role already
  resolves provider+model via `role_registry.resolve_role` — no resolution
  change needed, only the flag removal.

After the change, the gate at `server.py:989-994` drops the
`tdd_split_mode == "on"` conjunct:

```python
# before
tdd_split_mode = os.environ.get("PIPELINE_TDD_SPLIT", "off").strip().lower()
test_author_marker = worktree_path / ".tdd_split_test_author_done"
if (
    tdd_split_mode == "on"
    and story.get("tdd_split")
    and not resuming
    and not test_author_marker.exists()
):

# after
test_author_marker = worktree_path / ".tdd_split_test_author_done"
if (
    story.get("tdd_split")
    and not resuming
    and not test_author_marker.exists()
):
```

### 3.2 Guided decomposition / planner — unified provider+model, default ollama/glm

`PIPELINE_DECOMPOSE` today does three jobs at once: on/off toggle (`off`),
mode selector (`cloud` = hardcoded Claude, `local` = registry-resolved), and
implicitly the model pin via `PIPELINE_DECOMPOSE_CLOUD_MODEL` in cloud mode.
The operator wants the on/off gone **and** provider+model independently
configurable with an `ollama/glm` default. That obsoletes the `cloud`/`local`
mode split — there is no longer a "cloud" path that hardcodes Claude. So
`PIPELINE_DECOMPOSE` is **removed entirely**, replaced by unified
`role_registry` resolution identical in shape to how the `review` and
`test_author` roles already work.

- **Removed:**
  - the `PIPELINE_DECOMPOSE` env var and its two read sites
    (`server.py:1014` initial, `server.py:1108` rework);
  - the `PIPELINE_DECOMPOSE_CLOUD_MODEL` env var and its read at
    `planner.py:114` (the `cloud` branch that hardcodes Claude is gone, so this
    model pin has no job left);
  - the `mode` parameter from `_resolve_planner_backend`, `_run_planner`, and
    `_run_rework_planner` (`planner.py:97-235`) — there is no longer a mode to
    select.
- **Stays (the provider/model config the operator wants kept):**
  - `PIPELINE_BACKEND_PLANNER` — the planner's **provider** pin
    (`claude | ollama | lmstudio | mlx | local`), already read at
    `planner.py:121` via `resolve_role`'s `PIPELINE_BACKEND_<ROLE>` lookup.
  - **`PIPELINE_LOCAL_PLANNER_MODEL` — NEW**, the planner's **model** pin,
    mirroring `PIPELINE_LOCAL_REVIEW_MODEL` (`review.py:70-72`): a
    top-priority model override that wins over the registry/`role_config` model
    when the resolved provider is local-family (so a bare Ollama tag never
    leaks into a Claude planner). This is the missing knob — today the planner
    has a provider env var but no model env var.
  - plan `role_config.planner` (`{provider, model}`) — per-plan override,
    highest priority.
  - `model_registry.json` `roles.planner` (`{provider, model}`) — the
    configured default, currently `{"provider": "ollama", "model": "glm"}`
    (PR #151). This already takes effect today under the loaded
    `PIPELINE_DECOMPOSE=local`; the plan just removes the `local`/`cloud`/`off`
    selector so the registry is the only resolution path.
- **Stays:** `PIPELINE_DECOMPOSE_SCRATCHPAD` (`server.py:1021-1023`) — the
  cross-step-memory behavior knob. It is neither an on/off toggle nor a
  provider/model knob, and shares only the `PIPELINE_DECOMPOSE_` prefix; the
  prefix becomes slightly misnamed after `PIPELINE_DECOMPOSE` is removed, but
  renaming is cosmetic and out of scope.
- **Stays:** the `dispatch_backend in _LOCAL_BACKEND_NAMES` gate
  (`server.py:1027`, `:1109`) — the planner still only runs for local-family
  dispatch; Claude dispatch doesn't need the crutch.

#### 3.2.1 Resolution contract (`_resolve_planner_backend`, rewritten)

Today the function has two branches: `cloud` → hardcoded Claude; `local` →
registry/env, with a `if not provider_override: return dispatch_backend,
local_model` short-circuit that mirrors dispatch when nothing is configured.
After the change there is one path:

```python
def _resolve_planner_backend(
    dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
) -> tuple[str, str]:
    """Resolve (provider, model) for the planner role. Always-on; no mode.

    Provider priority: plan role_config.planner.provider ->
    PIPELINE_BACKEND_PLANNER -> registry roles.planner.provider ->
    default "ollama".
    Model priority: PIPELINE_LOCAL_PLANNER_MODEL (local-family only, top) ->
    plan role_config.planner.model -> registry roles.planner.model ->
    concrete default tag for ollama/glm.

    The registry already pins roles.planner = ollama/glm (PR #151), so a
    stock install resolves to ollama/glm-5.2:cloud with no env vars set.
    The default_provider="ollama" + glm model_fallback cover an install with
    no registry entry at all, so the ollama/glm default holds either way.
    """
    registry = role_registry.load_registry()
    resolution = role_registry.resolve_role(
        "planner", plan_role_config=plan_role_config, registry=registry,
        default_provider="ollama",
        model_fallback=lambda: _default_planner_model_tag(registry),
    )
    # Mirror review.py:69-72 — a bare local model tag only applies when the
    # resolved provider is local-family; never leak it into a Claude planner.
    if resolution.provider in _LOCAL_BACKEND_NAMES:
        override = os.environ.get("PIPELINE_LOCAL_PLANNER_MODEL")
        if override:
            resolution = resolution.with_model(override)  # or rebuild
    return resolution.provider, resolution.model
```

`_default_planner_model_tag(registry)` must return the **concrete** tag
(`glm-5.2:cloud`), not the friendly name `glm` — `resolve_role`'s
`model_fallback` path (`role_registry.py:146`) returns the fallback verbatim
without resolving it against `providers.<p>.models`, so a friendly name there
would be passed raw to the driver. Resolve it from
`registry["providers"]["ollama"]["models"]["glm"]["tag"]`, or fall back to a
module constant. (When the registry has `roles.planner.model` — the stock case
— `resolve_role` takes the `registry_model` branch at line 134 and resolves
the friendly name to the concrete tag itself, so the fallback only fires for
an install with no `roles.planner` entry at all.)

The `dispatch_backend`/`local_model` args are retained in the signature for
call-site stability and so a future same-model refusal (see §8) could compare
against them, but the **mirror-dispatch fallback is removed** — the planner no
longer silently copies dispatch's provider/model when unconfigured; it uses
the `ollama/glm` default instead.

#### 3.2.2 The dispatch gate (initial + rework), rewritten

With `PIPELINE_DECOMPOSE` gone, the gate no longer reads a mode; the planner
is always-on for local-family dispatch:

```python
# before (server.py:1014, 1025-1030)
decompose_mode = os.environ.get("PIPELINE_DECOMPOSE", "off").strip().lower()
scratchpad_on = (
    os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower() != "off"
)
plan_path = worktree_path / ".agent_plan.md"
if (
    decompose_mode in ("cloud", "local")
    and dispatch_backend in _LOCAL_BACKEND_NAMES
    and not resuming
    and not plan_path.exists()
):
    plan_text = _run_planner(
        story.get("agent_instructions", ""), mode=decompose_mode, ...

# after
scratchpad_on = (
    os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower() != "off"
)
plan_path = worktree_path / ".agent_plan.md"
if (
    dispatch_backend in _LOCAL_BACKEND_NAMES
    and not resuming
    and not plan_path.exists()
):
    plan_text = _run_planner(
        story.get("agent_instructions", ""), ...,  # no mode=
    )
```

The rework-planner gate at `server.py:1107-1115` gets the identical treatment:
drop the `decompose_mode` read and the `decompose_mode in ("cloud", "local")`
conjunct, drop `mode=` from the `_run_rework_planner` call.

## 4. Code edits (exact)

| File | Line(s) | Edit |
|---|---|---|
| `pipeline/server.py` | 987 | Delete `tdd_split_mode = os.environ.get("PIPELINE_TDD_SPLIT", …)`. |
| `pipeline/server.py` | 989-994 | Drop the `tdd_split_mode == "on"` conjunct. |
| `pipeline/server.py` | 1014 | Delete `decompose_mode = os.environ.get("PIPELINE_DECOMPOSE", …)`. |
| `pipeline/server.py` | 1025-1036 | Drop `decompose_mode in ("cloud","local")` conjunct; drop `mode=decompose_mode` from `_run_planner` call. |
| `pipeline/server.py` | 1107-1115 | Same for the rework-planner gate and `_run_rework_planner` call. |
| `pipeline/planner.py` | 97-130 | Rewrite `_resolve_planner_backend`: remove `mode` param + `cloud` branch + mirror-dispatch short-circuit; always `resolve_role("planner", default_provider="ollama", model_fallback=glm-tag)`; add `PIPELINE_LOCAL_PLANNER_MODEL` override gated on local-family. |
| `pipeline/planner.py` | 112-116 | Delete the `if mode == "cloud": return "claude", PIPELINE_DECOMPOSE_CLOUD_MODEL …` branch. |
| `pipeline/planner.py` | 133-160, 215-235 | Drop `mode` param from `_run_planner` and `_run_rework_planner` signatures + their `_resolve_planner_backend` calls. |
| `launchd/...advance-scheduler.plist` | 21-26 | Remove `PIPELINE_DECOMPOSE` and `PIPELINE_DECOMPOSE_CLOUD_MODEL` keys. |
| `launchd/...advance-scheduler.plist` | 57-58 | Remove the `PIPELINE_TDD_SPLIT` key. |
| `model_registry.json` | 26-29 | No change. `roles.planner = ollama/glm` and `roles.test_author = ollama/glm` stay — they ARE the provider/model config, and the planner one now actually takes effect. |

`pipeline/review.py` needs **no** edit (reviewer resolution is unchanged;
Finding B is documented only). `role_registry.py` needs **no** edit
(`resolve_role` already supports `default_provider` and the `PIPELINE_BACKEND_
<ROLE>` + registry + `role_config` priority chain).

## 5. Tests affected — EXISTING-TEST EDITS APPROVED 2026-07-20 (CLAUDE.md Step 4)

Removing the on/off toggles and the `cloud`/`local` split invalidates a large
block of existing tests. **The operator approved editing these existing tests
on 2026-07-20** (not just adding new ones). The implementer may modify the
tests listed below; each modification must preserve the test's *intent* where
that intent still applies (e.g. the story-field gate) and replace the
now-obsolete "off / unset = no-op" intent with the new "always-on" assertions
from the "New tests to add" list.

Tests that **must change** (they assert now-removed behavior):

- `test_pipeline_mcp_server.py:12333` — "PIPELINE_DECOMPOSE off (default) must
  leave the existing rework path" (`off` default gone).
- `:12417` — "PIPELINE_DECOMPOSE unset must be a strict no-op: the planner is
  never [called]" (unset now runs the planner for local-family dispatch).
- `:12348` / `:12426` — "planner / rework planner must not run when
  PIPELINE_DECOMPOSE is off" (`off` gone).
- `:12818` — "PIPELINE_TDD_SPLIT unset must be a strict no-op" (var gone;
  unset now runs the phase for an opted-in, non-resuming story).
- Every test that sets `PIPELINE_DECOMPOSE=cloud` to exercise the planner
  (`:12281`, `:12379`, `:12445`, `:12499`, `:12534`, `:12569`, `:12614`,
  `:12648`, `:12676`, `:12711`, `:12772`) — the `cloud` path is removed, so
  these must switch to driving the planner via the registry/`role_config`/env
  (e.g. set `roles.planner` in a temp registry, or `PIPELINE_BACKEND_PLANNER` +
  `PIPELINE_LOCAL_PLANNER_MODEL`). Each must be re-checked; some assert
  cloud-specific resolution (`:11639`/`:11646` for `PIPELINE_DECOMPOSE_CLOUD_
  MODEL`) that no longer exists.
- `:11639`/`:11646` — "PIPELINE_DECOMPOSE_CLOUD_MODEL lets an operator pin the
  cloud planner" — the var is removed; this test is deleted or repurposed to
  `PIPELINE_LOCAL_PLANNER_MODEL`.

Tests that **stay valid** (story-field / marker / fail-open semantics,
unchanged) — re-check each:
- `:12846` (TDD-split story-field gate), `:12875`, `:12927`, `:12964`
  (TDD-split success/fail-open/marker) — `setenv("PIPELINE_TDD_SPLIT","on")`
  lines become dead but harmless; remove for cleanliness.

**New tests to add** (TDD, red first per CLAUDE.md Step 3):
- Planner resolves to `ollama/glm-5.2:cloud` with **no env vars and no
  role_config** (the registry default) — red against current code (which would
  mirror-dispatch or hit the cloud branch).
- Planner resolves to `ollama/glm` even with **no `roles.planner` registry
  entry** (the `default_provider="ollama"` + glm-tag fallback) — red against
  current code.
- `PIPELINE_BACKEND_PLANNER=mlx` pins the provider; `PIPELINE_LOCAL_PLANNER_
  MODEL=<tag>` pins the model; together they override the registry — red
  against current code (no model env var exists today).
- `PIPELINE_LOCAL_PLANNER_MODEL` is **ignored when the resolved provider is
  Claude** (mirror review.py's local-family gate — no bare Ollama tag leaks
  into a Claude planner) — red against current code.
- plan `role_config.planner = {provider, model}` wins over env and registry.
- Planner always runs for a local-family dispatch when `PIPELINE_DECOMPOSE` is
  unset (the always-on flip) — red against current code.
- Same-model refusal for `test_author` still skips the phase with
  `PIPELINE_TDD_SPLIT` unset (TDD-split safety net survives flag removal).
- A garbage `PIPELINE_BACKEND_PLANNER` value (unknown provider) fails open to
  no planner, never crashes dispatch.

## 6. Behavioral change callout (production)

**Production does NOT change models.** Per Finding B, the loaded scheduler
already runs the planner on `ollama/glm-5.2:cloud` (via `PIPELINE_DECOMPOSE=
local` + the registry pin) and the reviewer on `ollama/glm-5.2:cloud` (via
`PIPELINE_BACKEND_REVIEW=local` + the review-model override). This plan
removes the `PIPELINE_DECOMPOSE` / `PIPELINE_TDD_SPLIT` / `PIPELINE_DECOMPOSE_
CLOUD_MODEL` env vars and the `cloud`/`local`/`off` mode logic, codifying the
**current loaded config** as the always-on, registry-resolved default
(`ollama/glm`). The concrete model each role resolves to is unchanged.

What **does** change:
- The planner can no longer be disabled, and the `cloud`-hardcoded-Claude path
  is removed from the code. An operator who later sets
  `role_config.planner = {provider: claude, model: sonnet}` at the plan level
  (or a `roles.planner` Claude entry in the registry) gets Claude per-plan —
  this is now a config knob, not a global flag. (Today's `PIPELINE_DECOMPOSE=
  cloud` path is gone; the equivalent is a role_config/registry entry.)
- `PIPELINE_DECOMPOSE_CLOUD_MODEL` is gone. Its job (pinning the Claude model in
  cloud mode) is replaced by the unified `role_config.planner.model` /
  `PIPELINE_LOCAL_PLANNER_MODEL` / registry `roles.planner.model` resolution.

**Validation note (not a blocker):** glm-as-planner has been **live in
production since 2026-07-20** (the DECOMPOSE=local deployment), so this is not
a new model switch — it is the flag-removal that makes the existing live
behavior the only behavior. The guided-decomposition n=4 formal validation
(PR #132/134/135/136/137) used Claude as tech lead, so glm-as-planner has not
been *formally* validated against a benchmark — but it has been running
unattended-by-the-benchmark on real dispatches since 2026-07-20. The standing
guided-decomposition caveat (`project_guided_decomposition_plan` memory: "good
enough with a human watching, not yet safe unattended") still applies, to the
feature as a whole, regardless of which model authors the checklist.

**Reviewer is unchanged** (already glm in production per Finding B).

## 7. Documentation updates (Definition of Done)

- `README.md:567-599` (guided-decomposition prose) — rewrite: no longer
  "opt-in and off by default (`PIPELINE_DECOMPOSE=off`)"; now always-on for
  local-family dispatch, provider+model configurable via `role_config.planner`
  / `PIPELINE_BACKEND_PLANNER` / `PIPELINE_LOCAL_PLANNER_MODEL` /
  `model_registry.json roles.planner`, default `ollama/glm`. Drop all
  `cloud`/`local`/`off` mode language and the `PIPELINE_DECOMPOSE_CLOUD_MODEL`
  references. The fail-open contract stays but is phrased as "fails open to no
  checklist," not "exactly like `PIPELINE_DECOMPOSE=off`."
- `README.md:666` (env-var table) — **delete** the `PIPELINE_DECOMPOSE` row.
- `README.md:556` — drop the `PIPELINE_DECOMPOSE_CLOUD_MODEL` mention; **delete
  its table row** if one exists (grep the table for it).
- `README.md:664` (`PIPELINE_BACKEND_PLANNER` row) — update: no longer "mode=
  local path"; it's the planner provider pin, period (default now ollama, not
  mirror-dispatch).
- **Add** a `PIPELINE_LOCAL_PLANNER_MODEL` table row mirroring the
  `PIPELINE_LOCAL_REVIEW_MODEL` row's description (top-priority model override,
  local-family only).
- `README.md:667` (`PIPELINE_DECOMPOSE_SCRATCHPAD` row) — drop "No effect when
  `PIPELINE_DECOMPOSE=off`"; rephrase to "No effect when the planner is not
  configured / fails open."
- `README.md` — **add** a short "## TDD-split (test-author phase)" prose
  section + document the per-story `tdd_split` opt-in field. `PIPELINE_TDD_SPLIT`
  was never documented in README (grep found no mention); the feature is now
  always-on and deserves a section mirroring the guided-decomposition one.
- `launchd/...advance-scheduler.plist` — remove `PIPELINE_DECOMPOSE`,
  `PIPELINE_DECOMPOSE_CLOUD_MODEL`, `PIPELINE_TDD_SPLIT` keys (§4). The plist
  is a separate copy in `~/Library/LaunchAgents/` — `cp` before `launchctl
  unload/load` (per `project_scheduler_env_overrides_mcp_env` memory). After
  reload, confirm via `get_role_config` that `planner` resolves to
  `ollama/glm-5.2:cloud`.

## 8. Out of scope

- Adding a `dispatch_backend in _LOCAL_BACKEND_NAMES` gate to the TDD-split
  phase (it has none today; the planner does). Behavior change beyond "remove
  the toggle."
- A same-model refusal for the **planner** (the `test_author` role has one;
  the planner does not). If the operator pins the planner to the same model as
  dispatch, no refusal fires. Pre-existing behavior; out of scope.
- Switching the production **reviewer** from Claude to glm (Finding B). The
  `PIPELINE_LOCAL_REVIEW_MODEL` override is inert today because
  `PIPELINE_BACKEND_REVIEW=claude`; making review run on glm requires changing
  `PIPELINE_BACKEND_REVIEW` to a local-family provider (or `local`). That is a
  separate decision with its own validation implications, not part of this
  plan.
- Renaming `PIPELINE_DECOMPOSE_SCRATCHPAD` (its prefix becomes slightly
  misnamed after `PIPELINE_DECOMPOSE` is removed). Cosmetic.
- Removing the per-story `tdd_split` opt-in field (the user's directive was
  about the global on/off toggle, not per-story authoring).
- The `PIPELINE_BACKEND_DECOMPOSE` env var (the `decompose_plan` *tool*'s
  backend) — distinct from `PIPELINE_DECOMPOSE`, untouched.

## 9. Rollout

Per CLAUDE.md, implementation is filed as pipeline stories (not done ad hoc),
TDD, with glm-authored tests + gpt-oss implementation + glm review (the
verified production config for the implementer/reviewer roles). Suggested
story split — the planner story is now the larger of the two:

1. **Planner: unified provider+model, always-on, default ollama/glm.** Rewrite
   `_resolve_planner_backend` (drop `mode`/`cloud`/mirror-dispatch; add
   `PIPELINE_LOCAL_PLANNER_MODEL` + ollama/glm default). Drop `decompose_mode`
   from both gates and both `_run_planner`/`_run_rework_planner` signatures.
   Remove `PIPELINE_DECOMPOSE` + `PIPELINE_DECOMPOSE_CLOUD_MODEL` reads. New
   red tests (§5). Update the §5 "off"/"cloud"-encoding tests (with user
   approval). README + plist updates for the planner. Production model
   assignment is unchanged (planner already glm — §6).
2. **TDD-split: always-on.** Delete `PIPELINE_TDD_SPLIT` read at `server.py:
   987`, drop the `== "on"` conjunct. New red tests for unset=runs + same-model
   refusal still skips. Update the §5 `PIPELINE_TDD_SPLIT` no-op tests (with
   user approval). Remove the plist key. README TDD-split section.

Both are independently reviewable PRs. Run the full suite (`pytest`, 1229
baseline) + `ruff check` after each; route through `review_story` →
`approve_merge` (CI-gated). Week usage is at 99% (resets 2026-07-21 ~09:00) —
do not dispatch until reset; hand-implement + glm review is the interim path
if needed before then.

## 10. Operator decision — RESOLVED 2026-07-20

**Option (a) confirmed: commit the repo plist.** The `PIPELINE_DECOMPOSE` /
`PIPELINE_DECOMPOSE_CLOUD_MODEL` / `PIPELINE_TDD_SPLIT` keys are removed from
the repo `launchd/…plist` (§4) and committed alongside the code change, so the
glm default is durable. The implementer must also `cp` the committed repo
plist to `~/Library/LaunchAgents/com.claude.pipeline.advance-scheduler.plist`
and `launchctl bootout` + `bootstrap` (per the
`project_scheduler_env_overrides_mcp_env` memory), then verify via
`launchctl print` that `planner` resolves to `ollama/glm-5.2:cloud` and the
removed keys are absent.