# Dashboard sidebar settings realignment plan

> Draft plan document, 2026-10-21. **Not ingested.** This is a persisted
> design/scope document to revisit later and decompose into an ingestable
> plan (per `.claude/rules/pipeline-story-schema.md`) when we're ready to
> execute. Nothing here has been preflighted against the local-dispatch
> checklist; that happens at decomposition time. (The known test-pin
> conflicts are inventoried below so the impact run has a head start.)

## Goal

The dashboard's two settings destinations — **Config** and **Workspace** —
currently render as orphan UI outside the sidebar. Move them into the
sidebar itself, pinned to its bottom under a section label, so the sidebar
becomes the single navigational spine for all four non-plan destinations
(Comms, Overview, Config, Workspace). Secondary: give the Workspace nav
item real informational value by showing the active repository in its meta
line, give both settings views the shared content-slot flex rule they're
missing, and make the settings views deep-linkable via URL hash.

## Problem (diagnosed 2026-10-21)

`<main>` in `static/index.html:22-117` is a single flex row
(`static/style.css:246-249`). Its children, in order, are
`#plan-list` (240px sidebar), `#config-nav`, `#workspace-nav`, then the four
view sections (`index.html:23-25`). The two settings navs are **siblings of
the sidebar, not children of it**, and a grep of `style.css` confirms
`#config-nav` / `#workspace-nav` have *zero* CSS rules — each renders as a
bare `.plan-item` card in its own unstyled thin column sandwiched between
the 240px sidebar and the content. They look like plan rows that escaped.

Meanwhile Comms and Overview — the other two non-plan destinations — are
injected **inside** `#plan-list` (`static/app/render/plan-list.js:251-273`).
So the four navigational peers are split across two different layout homes
in two different visual forms.

Secondary defects:

- Only `#plan-detail` (`style.css:353`) and `#comms-view`
  (`style.css:989-993`) get `flex: 1` in the content slot. `#config-view`
  and `#workspace-view` have no flex rule, so a shown settings view sizes to
  its content rather than filling the slot.
- `static/app/routing.js` has hash keys for plan + filters only, so
  browser refresh / back drops the user out of a settings view entirely.
- The nav meta copy is system vocabulary ("roles & env", "active repo"),
  and the Workspace item shows no state — even though workspace selection
  re-scopes the entire plan list, the board gives no persistent indication
  of which repo it's scoped to.

## Current architecture (verified 2026-10-21)

- **Nav wiring**: `main.js:806-814` (`wireConfigNav`, an IIFE) appends a
  `.plan-item.config-item` to `#config-nav`; `main.js:835-896`
  (`wireWorkspaceView`) appends a matching item to `#workspace-nav` and
  binds the click that sets `state.workspaceActive`.
- **View switching**: `main.js:176-203` (`_applyActiveView`) toggles
  `.hidden` across `#plan-detail` / `#config-view` / `#workspace-view` /
  `#comms-view` from `state.configActive` / `state.workspaceActive` /
  `state.commsActive`.
- **Sidebar injection precedent**: Comms (accent pill, first) and Overview
  (plain `.plan-item`, second) are built and appended by
  `render/plan-list.js:251-273`; `.plan-list-section-label`
  (`style.css:344-351`) is the existing section-label style ("PLANS").
- **Workspace state**: `static/app/workspace.js` exports
  `fetchActiveWorkspace()` (already imported in `main.js:34`), so the
  active repo path is fetchable today without a new endpoint.

## Proposed design

### 1. Delete the orphan navs; inject settings items into `#plan-list`

- Remove `<nav id="config-nav">` and `<nav id="workspace-nav">` from
  `index.html`. Keep `#config-view` / `#workspace-view` unchanged.
- Inject two items (Workspace, then Config) into `#plan-list`, following
  the existing Comms/Overview injection pattern. They sit in a bottom-
  pinned group separated by a `.plan-list-section-label` (text: "SYSTEM");
  the group gets `margin-top: auto` in a flex-column sidebar so it anchors
  to the bottom edge while the plan rows scroll above it. Settings are a
  foot, not a head — reached rarely and deliberately, and
  bottom-anchored settings matches the conventional spot, so user's muscle
  memory transfers.
- Active-state styling reuses the existing `.plan-item.active` treatment
  (accent left border); clicks set the same `state.*Active` flags and call
  the same view loaders as today's wiring. Rename the meta lines: Config →
  "dispatch · review · models", Workspace → the active repo's basename
  (see below).

### 2. Workspace item shows the active repo

Replace the static "active repo" meta with the basename of the active
workspace path (e.g. `fagan` for `~/git/fagan`), fetched
via the existing `fetchActiveWorkspace()` and refreshed on successful
workspace select. The nav item becomes the board's persistent scope
indicator.

### 3. Shared content-slot flex rule

Extend the `#comms-view` rule at `style.css:989-993` so `#config-view` and
`#workspace-view` share `flex: 1; min-width: 0` (and appropriate overflow),
so all four views fill the content slot identically.

### 4. Deep-linkable settings views

Add a `view` hash key to `static/app/routing.js` (`#view=config` /
`#view=workspace`) encoded/decoded alongside the existing plan/filter
keys, so refresh and back/forward behave the same on settings views as on
plan views.

## Known pre-existing-test conflicts (inventory for the impact run)

This repo's history says grep is not enough — the full preflight impact
run (`.claude/rules/local-dispatch-preflight.md` §1) is still mandatory —
but these conflicts are already visible and must be pre-authorized or
designed around when stories are written:

1. **`tests/unit/test_comms_landing_redesign.py:441`** — asserts
   `main.js` is byte-identical to a pinned SHA-256
   (`_UNCHANGED_JS_SHA256[MAIN_JS]`; `app.js` is pinned the same way at
   line 433). Any story touching `main.js` needs this hash re-pinned, a
   pre-authorized test edit with literal before/after values per CLAUDE.md
   Step 4. The file's own comment history shows it has been re-pinned at
   least four times for exactly this kind of change, so this is routine,
   not exceptional. **Alternative:** if the settings injection can live
   entirely in `render/plan-list.js` (which is *not* byte-frozen), main.js
   changes shrink to deletion of `wireConfigNav`/`wireWorkspaceView` —
   which still trips the pin. Decide the split, then re-pin once.
2. **`tests/test_app_workspace_picker.mjs`** — asserts `#workspace-nav`
   exists in `index.html` (lines 331, 404) and tests the
   click-wiring-on-`#workspace-nav` behavior (lines 469-506). Deleting the
   nav element requires updating these expectations to the new host item
   inside `#plan-list`.
3. **`tests/unit/test_dashboard_comms_css.py`** — references
   `#workspace-nav` beside `#config-nav` (lines 43-44); check whether its
   CSS-region assertions tolerate the navs' removal.
4. **`tests/unit/test_dashboard_planlist_nav_css.py`** and siblings —
   verify none assert the *absence* of extra items in `#plan-list` or pin
   the sidebar's exact child count.

## Sizing notes for decomposition time

- **`style.css` is 1292 lines** — over the ~1000-line file-size gate
  (`agent-dispatch-story-sizing.md`). Stories editing it should append
  rules or extend a known selector list rather than edit the interior;
  consider routing CSS stories to a stronger tier.
- **`main.js` is 951 lines** — near the gate, and byte-SHA-pinned besides.
  Prefer hosting new behavior in `render/plan-list.js` (371 lines, not
  pinned) and keeping main.js edits to deletions of the old wiring.
- **Deletion-story survivor list required**: removing `#config-nav` /
  `#workspace-nav` must name the survivors explicitly: `#config-view`,
  `#workspace-view`, `#plan-list`, the Comms/Overview items, the
  `.config-item` class usage, and all of `workspace.js` (its picker logic
  is unrelated to the nav's location).
- Story-by-concern split to consider: (a) move the two nav items into the
  sidebar + delete orphan navs + re-pin the SHA; (b) content-slot flex
  rule (CSS-appendable, smallest); (c) active-repo meta in the Workspace
  item; (d) `view` hash key in routing.js. (b)-(d) are independent of
  each other; all depend on (a) only for the item's new home.

## Acceptance ideas (mechanically checkable)

- `grep -c "config-nav\|workspace-nav" static/index.html` returns `0`.
- A markup test asserts the Config and Workspace items exist as children
  of `#plan-list`, after the last plan row region.
- A DOM test asserts clicking the Workspace item sets
  `state.workspaceActive` and un-hides `#workspace-view` (same behavior as
  today's `test_app_workspace_picker.mjs`, retargeted to the new host).
- A markup test asserts the Workspace item's meta shows the basename of a
  stubbed active-path response, not the literal "active repo".
- `#config` / `#workspace` hash round-trips: set hash → settings view
  shown; select settings view → hash updated. Follow the existing
  `parseHash`/`encodeHashState` test patterns.
- CSS text test asserts `#config-view` and `#workspace-view` appear in the
  `flex: 1` rule alongside `#comms-view`.

## Open questions

1. Label for the section above the settings group: "SYSTEM" (used above)
   vs "SETTINGS" — the group also semantically covers repo scope, which is
   not quite "settings".
2. Should the Workspace item's meta show the full path (truncated) or the
   basename? Basename is cleaner; full path is unambiguous when two
   workspaces share a basename — could use basename + `title` tooltip with
   the full path.
3. Order within the group: Workspace then Config (scope-then-machinery)
   or Config then Workspace?
4. Keep the `.plan-item` visual form for settings items, or give them a
   quieter variant (dimmer, no bottom border) to distinguish "machine
   surface" from "work surface"? The Comms pill precedent says special ==
   entry-point; settings are not entry points, so plainer is defensible.
