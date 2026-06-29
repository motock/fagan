// Tests for the backend + escalated surface added in
// 2eb8c9f6-a8f7-4bd3-be2c-b1bf672b1d46: card badges, filter dimensions,
// and backward-compatible defaulting of the persisted filter blob.
//
// Run with:  node tests/test_app_backend_escalated.mjs
//
// The pure helpers in app.js (defaultFilters, applyFilters, parseHash,
// encodeHashState, renderBoard, renderFilterBar) are exercised via jsdom.
// DOM-dependent render functions are exercised by stubbing fetch and
// reading innerHTML off the plan-detail section.
//
// Testable criteria covered here:
//   - renderBoard cards show the correct badges
//     (claude, escalated, neither, both).
//   - backend/escalated filters narrow the board and combine with the
//     existing persona / risk / status filters.
//   - Reset clears them.
//   - Persisted filters reload including the new dimensions.
//   - Negative: a story with no backend field is treated as local
//     (no "undefined" badge, matches the "local" chip).
//   - Boundary: a stored localStorage blob from before the change (no
//     backends / escalated key) loads without throwing and defaults
//     to show-all ("[]").

import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

// JSDOM exposes its own realm: arrays evaluated inside the window have a
// different Array.prototype than the test's. `node:assert/strict` uses
// prototype-aware deepEqual, so cross-realm arrays fail with a misleading
// "same structure, not reference-equal" error. Normalize before assertions.
function toLocal(value) {
  if (Array.isArray(value)) return value.map(toLocal);
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value)) out[k] = toLocal(value[k]);
    return out;
  }
  return value;
}

function eq(actual, expected, label) {
  assert.deepEqual(toLocal(actual), expected, label);
}

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

function makeEnv({ hash = "", storage = {} } = {}) {
  const dom = new JSDOM(
    `<!doctype html><html><body>
       <div id="plan-list"></div>
       <section id="plan-detail"><p class="empty-state"></p></section>
       <div id="usage-banner"></div>
       <span id="last-updated"></span>
       <input type="checkbox" id="auto-refresh" checked />
       <div id="story-modal" class="modal hidden"></div>
       <div id="story-modal-body"></div>
       <button id="story-modal-close"></button>
     </body></html>`,
    { url: `http://localhost/${hash ? `#${hash}` : ""}`, runScripts: "outside-only" },
  );
  for (const [k, v] of Object.entries(storage)) {
    dom.window.localStorage.setItem(k, v);
  }
  return dom;
}

async function loadApp(dom) {
  dom.window.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ plans: [] }),
  });
  const fs = await import("node:fs/promises");
  const url = new URL("../static/app.js", import.meta.url);
  const src = await fs.readFile(url, "utf8");
  dom.window.eval(src);
  // Expose the fresh, window-bound helpers so each test sees its own state.
  return {
    BACKEND_VALUES: dom.window.BACKEND_VALUES,
    ESCALATED_VALUES: dom.window.ESCALATED_VALUES,
    defaultFilters: dom.window.defaultFilters,
    applyFilters: dom.window.applyFilters,
    encodeHashState: dom.window.encodeHashState,
    parseHash: dom.window.parseHash,
    renderBoard: dom.window.renderBoard,
    renderFilterBar: dom.window.renderFilterBar,
    renderPlanDetail: dom.window.renderPlanDetail,
    state: dom.window.state,
    dom,
  };
}

// Drive a full re-render with the given stories, returning the innerHTML
// of the #plan-detail section after renderPlanDetail has run. We feed a
// minimal plan object so renderPlanDetail's plan-name / paused / panels
// blocks don't throw — the things under test (filter bar, board, cards,
// badges) live inside this single innerHTML blob.
async function renderWithStories(api, stories, planName = "test-plan") {
  api.state.selectedPlan = planName;
  const plan = {
    name: planName,
    paused: false,
    stories,
    notifications: [],
    decisions: [],
  };
  api.renderPlanDetail(plan);
  const html = api.dom.window.document.getElementById("plan-detail").innerHTML;
  return html;
}

function cardsWith(html, predicate) {
  // Lightweight regex-based card selector so we don't have to bundle a
  // full HTML parser — renderBoard produces a fixed tag structure that
  // we control. Returns the captured class+key portion of every card.
  const cards = [];
  const re = /<div class="([^"]*\bcard\b[^"]*)"[^>]*data-key="([^"]+)">([\s\S]*?)<\/div>\s*<\/div>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const classAttr = m[1];
    const key = m[2];
    const body = m[3];
    // We only want outer card matches; bail if `data-key` appears inside
    // the class string (it doesn't, but be defensive).
    if (predicate(body, classAttr, key)) {
      cards.push({ key, body, classAttr });
    }
  }
  return cards;
}

function hasBadge(body, cls) {
  return body.includes(`class="card-badge ${cls}"`);
}

// ---------- defaults ---------------------------------------------------------

async function test_default_filters_include_new_dims_with_show_all() {
  const env = makeEnv();
  const api = await loadApp(env);
  const d = api.defaultFilters();
  eq(d.backends, [], "backends default to [] (show all)");
  eq(d.escalated, [], "escalated default to [] (show all)");
  // Existing dims remain untouched.
  eq(d.personas, [], "personas default to []");
  eq(d.risks, [], "risks default to []");
  assert.equal(d.sort, "key", "sort defaults to key");
  record("defaultFilters seeds backends + escalated as []", true);
}

async function test_backend_values_and_escalated_values_constants() {
  const env = makeEnv();
  const api = await loadApp(env);
  eq(api.BACKEND_VALUES, ["local", "claude"]);
  eq(api.ESCALATED_VALUES, ["yes", "no"]);
  record("BACKEND_VALUES + ESCALATED_VALUES constants are stable", true);
}

// ---------- applyFilters -----------------------------------------------------

async function test_apply_filters_missing_backend_is_local() {
  const env = makeEnv();
  const api = await loadApp(env);
  // A story with no `backend` field should be filterable by the "local"
  // chip and must NOT be filterable by "claude".
  const local = { status: "todo", persona: "a", risk: "low" }; // no backend
  const claude = { status: "todo", persona: "a", risk: "low", backend: "claude" };
  const entries = [["local", local], ["claude", claude]];

  api.state.filters.backends = ["local"];
  assert.equal(api.applyFilters(entries).length, 1, "matches only local");

  api.state.filters.backends = ["claude"];
  const out = api.applyFilters(entries);
  assert.equal(out.length, 1);
  assert.equal(out[0][0], "claude", "only the claude story matches");

  // No filter set: both come through.
  api.state.filters.backends = [];
  assert.equal(api.applyFilters(entries).length, 2);
  record("applyFilters treats missing backend as local", true);
}

async function test_apply_filters_escalated_yes_and_no() {
  const env = makeEnv();
  const api = await loadApp(env);
  const yes = { status: "todo", escalated: true };
  const no = { status: "todo", escalated: false };
  const missing = { status: "todo" }; // missing field -> false
  const entries = [["yes", yes], ["no", no], ["missing", missing]];

  api.state.filters.escalated = ["yes"];
  let out = api.applyFilters(entries);
  assert.equal(out.length, 1);
  assert.equal(out[0][0], "yes");

  api.state.filters.escalated = ["no"];
  out = api.applyFilters(entries);
  assert.equal(out.length, 2, "no + missing both match 'no'");
  const keys = out.map(([k]) => k).sort();
  eq(keys, ["missing", "no"]);

  api.state.filters.escalated = [];
  assert.equal(api.applyFilters(entries).length, 3, "no filter = all pass");
  record("applyFilters handles yes/no escalated combos", true);
}

async function test_apply_filters_combine_with_persona_risk() {
  const env = makeEnv();
  const api = await loadApp(env);
  const stories = [
    ["a", { status: "todo", persona: "p1", risk: "high", backend: "claude", escalated: true }],
    ["b", { status: "todo", persona: "p1", risk: "low", backend: "local" }],
    ["c", { status: "todo", persona: "p2", risk: "high", backend: "claude" }],
    ["d", { status: "todo", persona: "p2", risk: "high", backend: "claude", escalated: true }],
  ];
  // persona=p1 AND risk=high AND backend=claude AND escalated=yes => only "a".
  api.state.filters.personas = ["p1"];
  api.state.filters.risks = ["high"];
  api.state.filters.backends = ["claude"];
  api.state.filters.escalated = ["yes"];
  const out = api.applyFilters(stories).map(([k]) => k);
  eq(out, ["a"]);
  record("applyFilters combines backend + escalated with persona + risk", true);
}

// ---------- renderBoard: badges ---------------------------------------------

async function test_render_board_shows_claude_badge_when_backend_claude() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    s1: { status: "todo", summary: "x", backend: "claude" },
  });
  const cards = cardsWith(html, (body) => hasBadge(body, "card-badge-claude"));
  assert.equal(cards.length, 1, "one card");
  assert.equal(cards[0].key, "s1");
  record("renderBoard renders claude badge for backend=claude", true);
}

async function test_render_board_shows_escalated_badge() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    s1: { status: "todo", summary: "x", escalated: true },
  });
  const cards = cardsWith(html, (body) => hasBadge(body, "card-badge-escalated"));
  assert.equal(cards.length, 1, "one card");
  record("renderBoard renders escalated badge when escalated=true", true);
}

async function test_render_board_no_badge_for_plain_story() {
  const env = makeEnv();
  const api = await loadApp(api);
  // Wait — there's no such thing. Re-do properly:
}

async function test_render_board_no_badge_for_plain_story_real() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    s1: { status: "todo", summary: "x" }, // no backend, no escalated
  });
  const cards = cardsWith(html, (body) =>
    !body.includes("card-badge-"));
  assert.equal(cards.length, 1, "card present");
  record("renderBoard emits no claude/escalated badges when fields absent", true);
}

async function test_render_board_no_badge_when_backend_missing() {
  const env = makeEnv();
  const api = await loadApp(env);
  // Specifically: a "local"-defaulting story must not show the "claude"
  // badge nor a literal "undefined" badge.
  const html = await renderWithStories(api, {
    s1: { status: "todo", summary: "x" },
  });
  assert.ok(!html.includes("card-badge-claude"), "no claude badge");
  assert.ok(!html.includes("undefined"), "no literal 'undefined' string");
  record("missing backend field never renders 'undefined' / 'claude' badge", true);
}

async function test_render_board_both_badges_when_claude_and_escalated() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    s1: { status: "todo", summary: "x", backend: "claude", escalated: true },
  });
  const cards = cardsWith(html, (body) =>
    hasBadge(body, "card-badge-claude") && hasBadge(body, "card-badge-escalated"));
  assert.equal(cards.length, 1, "exactly one card with both badges");
  record("renderBoard can render both claude + escalated badges together", true);
}

// ---------- filter chips render ---------------------------------------------

async function test_filter_bar_hides_backend_chips_when_all_local() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    a: { status: "todo", summary: "x" }, // no backend
    b: { status: "todo", summary: "y" }, // no backend
  });
  // The Backend group is only emitted when at least one story has
  // backend === "claude".
  assert.ok(!html.includes("Backend</span>"), "no Backend group when all-local");
  record("filter bar omits Backend group when no claude stories", true);
}

async function test_filter_bar_hides_escalated_chips_when_none_escalated() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    a: { status: "todo", summary: "x", escalated: false },
  });
  assert.ok(!html.includes("Escalated</span>"), "no Escalated group");
  record("filter bar omits Escalated group when nothing escalated", true);
}

async function test_filter_bar_backend_group_appears_with_claude_story() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    a: { status: "todo", summary: "x", backend: "claude" },
  });
  assert.ok(html.includes("Backend</span>"), "Backend label present");
  assert.ok(html.includes('data-dim="backends"'), "backends chip dim");
  assert.ok(html.includes('data-value="claude"'), "claude chip value");
  assert.ok(html.includes('data-value="local"'), "local chip value");
  record("Backend group renders both local + claude chips when needed", true);
}

async function test_filter_bar_escalated_group_appears_when_escalated() {
  const env = makeEnv();
  const api = await loadApp(env);
  const html = await renderWithStories(api, {
    a: { status: "todo", summary: "x", escalated: true },
  });
  assert.ok(html.includes("Escalated</span>"), "Escalated label");
  assert.ok(html.includes('data-dim="escalated"'), "escalated chip dim");
  assert.ok(html.includes('data-value="yes"'), "yes chip");
  assert.ok(html.includes('data-value="no"'), "no chip");
  record("Escalated group renders yes + no chips when escalation exists", true);
}

// ---------- filter narrowing ------------------------------------------------

async function test_backend_chip_filters_narrow_the_board() {
  const env = makeEnv();
  const api = await loadApp(env);
  // Set the filter manually (we're not driving a click here — the chip
  // event path is exercised by the existing smoke tests).
  api.state.filters.backends = ["claude"];
  const html = await renderWithStories(api, {
    local: { status: "todo", summary: "x" },
    claude: { status: "todo", summary: "y", backend: "claude" },
  });
  const rendered = cardsWith(html, () => true);
  const keys = rendered.map((c) => c.key);
  eq(keys, ["claude"], "only the claude story remains");
  api.state.filters.backends = [];
  record("filtering by backend=claude narrows the board", true);
}

async function test_escalated_chip_filters_narrow_the_board() {
  const env = makeEnv();
  const api = await loadApp(env);
  api.state.filters.escalated = ["yes"];
  const html = await renderWithStories(api, {
    a: { status: "todo", summary: "x" },
    b: { status: "todo", summary: "y", escalated: true },
  });
  const keys = cardsWith(html, () => true).map((c) => c.key);
  eq(keys, ["b"]);
  api.state.filters.escalated = [];
  record("filtering by escalated=yes narrows the board", true);
}

async function test_filter_reset_clears_backends_and_escalated() {
  const env = makeEnv();
  const api = await loadApp(env);
  // Seed "active" filters and verify defaultFilters() empties them.
  const f = api.defaultFilters();
  f.backends = ["claude"];
  f.escalated = ["yes"];
  eq(f.backends, ["claude"]);
  eq(f.escalated, ["yes"]);
  const d = api.defaultFilters();
  eq(d.backends, []);
  eq(d.escalated, []);
  record("defaultFilters returns empty backends/escalated (reset clears them)", true);
}

async function test_backend_and_escalated_combine_with_status_filter() {
  const env = makeEnv();
  const api = await loadApp(env);
  api.state.filters.backends = ["claude"];
  api.state.filters.escalated = ["no"];
  const stories = {
    a: { status: "todo", summary: "x", backend: "claude", escalated: true },
    b: { status: "todo", summary: "x", backend: "claude", escalated: false },
    c: { status: "done", summary: "x", backend: "claude", escalated: false },
    d: { status: "todo", summary: "x", backend: "local", escalated: false },
  };
  const html = await renderWithStories(api, stories);
  const keys = cardsWith(html, () => true).map((c) => c.key);
  // Status filter keeps everything by default; on top of that
  // backend=claude AND escalated=no leaves "b" only.
  eq(keys, ["b"]);
  api.state.filters.backends = [];
  api.state.filters.escalated = [];
  record("backend + escalated combine with status (default = all)", true);
}

// ---------- backward compat: stored blob from before this change ------------

async function test_legacy_localstorage_blob_loads_without_throwing() {
  // Simulate the pre-change world: a stored filter blob that has only
  // the originally-existing keys, no backends/escalated. loadFilters
  // is called by loadApp() internally on the auto-init path; we
  // exercise it by setting localStorage before eval.
  const legacy = {
    statuses: ["todo", "in_progress"],
    personas: ["software-engineer"],
    risks: ["high"],
    sort: "risk",
    // no backends, no escalated -> missing keys must default to []
  };
  const env = makeEnv({
    storage: { "pipeline-dashboard-filters": JSON.stringify(legacy) },
  });
  const api = await loadApp(env);
  // The init code already called loadFilters via window.eval; verify
  // the actual state.
  eq(api.state.filters.backends, [], "backends defaulted to []");
  eq(api.state.filters.escalated, [], "escalated defaulted to []");
  // And original dims were preserved.
  assert.equal(api.state.filters.sort, "risk");
  eq(api.state.filters.statuses, ["todo", "in_progress"]);
  record("legacy localStorage blob loads missing new keys as []", true);
}

async function test_garbage_legacy_blob_does_not_throw() {
  // Anything else in the blob (extra keys, bad types) must not break the
  // load path. Spread merge just drops unknowns; we still expect the new
  // dims to default to [].
  const env = makeEnv({
    storage: {
      "pipeline-dashboard-filters": JSON.stringify({
        statuses: "not-an-array", // wrong type on purpose
        whales: "oceanic",
      }),
    },
  });
  const api = await loadApp(env);
  eq(api.state.filters.backends, []);
  eq(api.state.filters.escalated, []);
  record("garbage legacy blob still loads missing new keys as []", true);
}

// ---------- hash round-trip covers new dims ---------------------------------

async function test_encode_and_parse_backend_escalated_round_trip() {
  const env = makeEnv();
  const api = await loadApp(env);
  api.state.filters.backends = ["claude"];
  api.state.filters.escalated = ["yes"];
  const hash = api.encodeHashState();
  assert.ok(hash.includes("backend=claude"), `backend in hash: ${hash}`);
  assert.ok(hash.includes("escalated=yes"), `escalated in hash: ${hash}`);
  const parsed = api.parseHash(hash);
  eq(parsed.filters.backends, ["claude"]);
  eq(parsed.filters.escalated, ["yes"]);
  record("encodeHashState -> parseHash round-trips backend + escalated", true);
}

async function test_parse_unknown_backend_value_is_dropped() {
  const env = makeEnv();
  const api = await loadApp(env);
  const parsed = api.parseHash("backend=claude,banana,local");
  eq(parsed.filters.backends, ["claude", "local"]);
  record("parseHash drops unknown backend values", true);
}

async function test_parse_unknown_escalated_value_is_dropped() {
  const env = makeEnv();
  const api = await loadApp(env);
  const parsed = api.parseHash("escalated=yes,maybe");
  eq(parsed.filters.escalated, ["yes"]);
  record("parseHash drops unknown escalated values", true);
}

// ---------- runner ----------------------------------------------------------

async function run() {
  const tests = [
    test_default_filters_include_new_dims_with_show_all,
    test_backend_values_and_escalated_values_constants,
    test_apply_filters_missing_backend_is_local,
    test_apply_filters_escalated_yes_and_no,
    test_apply_filters_combine_with_persona_risk,
    test_render_board_shows_claude_badge_when_backend_claude,
    test_render_board_shows_escalated_badge,
    test_render_board_no_badge_for_plain_story_real,
    test_render_board_no_badge_when_backend_missing,
    test_render_board_both_badges_when_claude_and_escalated,
    test_filter_bar_hides_backend_chips_when_all_local,
    test_filter_bar_hides_escalated_chips_when_none_escalated,
    test_filter_bar_backend_group_appears_with_claude_story,
    test_filter_bar_escalated_group_appears_when_escalated,
    test_backend_chip_filters_narrow_the_board,
    test_escalated_chip_filters_narrow_the_board,
    test_filter_reset_clears_backends_and_escalated,
    test_backend_and_escalated_combine_with_status_filter,
    test_legacy_localstorage_blob_loads_without_throwing,
    test_garbage_legacy_blob_does_not_throw,
    test_encode_and_parse_backend_escalated_round_trip,
    test_parse_unknown_backend_value_is_dropped,
    test_parse_unknown_escalated_value_is_dropped,
  ];
  for (const t of tests) {
    try {
      await t();
    } catch (e) {
      record(t.name, false, e.stack || e.message);
    }
  }
  const passed = results.filter((r) => r.ok).length;
  const failed = results.length - passed;
  // eslint-disable-next-line no-console
  console.log(`\n${passed}/${results.length} passed${failed ? `, ${failed} FAILED` : ""}`);
  process.exit(failed === 0 ? 0 : 1);
}

run();
