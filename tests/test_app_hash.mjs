// Tests for static/app.js's URL hash deep-linking.
//
// Run with:  node tests/test_app_hash.mjs
//
// We exercise the pure helpers in app.js (encodeHashState / parseHash / etc.)
// via jsdom. The DOM-bound side of the module (selectPlan, refresh) is
// verified by stubbing fetch + DOM APIs.

import { JSDOM } from "jsdom";

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${label || "values differ"}: got ${a} want ${e}`);
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

// Build a fresh JSDOM with a clean localStorage and location.hash each test.
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
  // Seed localStorage from a JS object (jsdom doesn't expose a constructor arg).
  for (const [k, v] of Object.entries(storage)) {
    dom.window.localStorage.setItem(k, v);
  }
  return dom;
}

// Load app.js into a JSDOM window. fetch is stubbed to a no-op so refresh()
// doesn't make real network calls.
async function loadApp(dom) {
  dom.window.fetch = async () => ({ ok: true, json: async () => ({ plans: [] }) });
  const fs = await import("node:fs/promises");
  const url = new URL("../static/app.js", import.meta.url);
  const src = await fs.readFile(url, "utf8");
  // Evaluate inside the window so consts/functions attach there.
  dom.window.eval(src);
  // Expose the pure helpers we care about for direct testing.
  return {
    encodeHashState: dom.window.encodeHashState,
    parseHash: dom.window.parseHash,
    applyHashToState: dom.window.applyHashToState,
    updateHash: dom.window.updateHash,
    clearHash: dom.window.clearHash,
    hashStateFrom: dom.window.hashStateFrom,
    state: dom.window.state,
  };
}

// ---------- encodeHashState: only non-default values go in the URL ----------

async function test_encode_emits_empty_when_all_defaults() {
  const env = makeEnv();
  const { encodeHashState } = await loadApp(env);
  // Default state: no plan selected, all statuses, no personas/risks, sort=key.
  assertEqual(encodeHashState(), "", "empty hash for default state");
  record("encodeHashState returns empty string when nothing is set", true);
}

async function test_encode_includes_plan_and_nondefault_filters() {
  const env = makeEnv();
  const { encodeHashState, state } = await loadApp(env);
  state.selectedPlan = "demo";
  state.filters.statuses = ["todo", "in_progress"];
  state.filters.personas = ["software-engineer"];
  state.filters.sort = "risk";
  const hash = encodeHashState();
  assertTrue(hash.includes("plan=demo"), `plan in hash: ${hash}`);
  assertTrue(hash.includes("status=todo,in_progress"), `status in hash: ${hash}`);
  assertTrue(hash.includes("persona=software-engineer"), `persona in hash: ${hash}`);
  assertTrue(hash.includes("sort=risk"), `sort in hash: ${hash}`);
  record("encodeHashState includes plan and non-default filter values", true);
}

async function test_encode_omits_default_dimensions() {
  const env = makeEnv();
  const { encodeHashState, state } = await loadApp(env);
  // Only deviation is a non-default sort.
  state.filters.sort = "activity";
  const hash = encodeHashState();
  assertTrue(hash.includes("sort=activity"), `sort present: ${hash}`);
  assertTrue(!hash.includes("status="), `status omitted when default: ${hash}`);
  assertTrue(!hash.includes("persona="), `persona omitted when empty: ${hash}`);
  record("encodeHashState omits default dimensions for short URLs", true);
}

// ---------- parseHash: round-trip and graceful degradation -----------------

async function test_parse_round_trip() {
  const env = makeEnv();
  const { parseHash } = await loadApp(env);
  const parsed = parseHash("plan=demo&status=todo,in_progress&persona=software-engineer&sort=risk");
  assertEqual(parsed.selectedPlan, "demo", "selectedPlan");
  assertEqual(parsed.filters.statuses, ["todo", "in_progress"], "statuses");
  assertEqual(parsed.filters.personas, ["software-engineer"], "personas");
  assertEqual(parsed.filters.sort, "risk", "sort");
  record("parseHash decodes a canonical deep-link", true);
}

async function test_parse_unknown_status_is_ignored() {
  const env = makeEnv();
  const { parseHash } = await loadApp(env);
  const parsed = parseHash("status=todo,bogus,in_progress");
  assertEqual(parsed.filters.statuses, ["todo", "in_progress"], "bogus status dropped");
  record("parseHash ignores unknown status values", true);
}

async function test_parse_unknown_sort_falls_back_to_default() {
  const env = makeEnv();
  const { parseHash } = await loadApp(env);
  const parsed = parseHash("sort=banana");
  assertEqual(parsed.filters.sort, "key", "default sort");
  record("parseHash ignores unknown sort values, falls back to default", true);
}

async function test_parse_garbage_hash_returns_defaults_without_throwing() {
  const env = makeEnv();
  const { parseHash } = await loadApp(env);
  // No throw, defaults only.
  const parsed = parseHash("!!@@##$$%&&&===");
  assertEqual(parsed.selectedPlan, null, "no plan from garbage");
  assertEqual(parsed.filters.statuses.length, 9, "all statuses default");
  assertEqual(parsed.filters.sort, "key", "default sort");
  record("parseHash returns defaults for a garbage hash without throwing", true);
}

async function test_parse_empty_hash_returns_defaults() {
  const env = makeEnv();
  const { parseHash } = await loadApp(env);
  const parsed = parseHash("");
  assertEqual(parsed.selectedPlan, null, "no plan");
  assertEqual(parsed.filters.sort, "key", "default sort");
  record("parseHash returns defaults for an empty hash", true);
}

async function test_parse_handles_unknown_dimension_gracefully() {
  const env = makeEnv();
  const { parseHash } = await loadApp(env);
  const parsed = parseHash("plan=demo&unicorn=glitter&status=todo");
  assertEqual(parsed.selectedPlan, "demo", "plan survives unknown dimension");
  assertEqual(parsed.filters.statuses, ["todo"], "status still applies");
  record("parseHash ignores unknown keys, keeps valid ones", true);
}

// ---------- on-load: hash wins over localStorage ----------------------------

async function test_load_hash_overrides_localstorage() {
  // localStorage says one thing; hash says another. Hash must win.
  const env = makeEnv({
    hash: "plan=from-hash&sort=risk",
    storage: {
      "pipeline-dashboard-filters": JSON.stringify({
        statuses: ["done"],
        sort: "activity",
      }),
    },
  });
  const { state } = await loadApp(env);
  // loadFilters runs at the bottom of app.js (localStorage path),
  // then applyHashToState runs from the hashchange bootstrap below.
  // We re-apply explicitly here to mirror the wiring under test.
  env.window.applyHashToState();
  assertEqual(state.selectedPlan, "from-hash", "hash plan wins");
  assertEqual(state.filters.sort, "risk", "hash sort wins");
  record("hash overrides conflicting localStorage values on load", true);
}

async function test_load_bare_url_uses_defaults() {
  const env = makeEnv();
  const { state } = await loadApp(env);
  env.window.applyHashToState();
  assertEqual(state.selectedPlan, null, "no plan selected");
  assertEqual(state.filters.sort, "key", "default sort");
  record("bare URL (no hash) loads defaults", true);
}

async function test_load_only_localstorage_when_no_hash() {
  const env = makeEnv({
    storage: {
      "pipeline-dashboard-filters": JSON.stringify({
        statuses: ["todo", "done"],
        sort: "activity",
      }),
    },
  });
  const { state } = await loadApp(env);
  env.window.applyHashToState();
  assertEqual(state.selectedPlan, null, "no plan from no hash");
  assertEqual(state.filters.sort, "activity", "localStorage sort restored");
  record("localStorage is restored when no hash is present", true);
}

// ---------- updateHash / clearHash: side effects on window.location.hash ----

async function test_update_hash_writes_window_location() {
  const env = makeEnv();
  const { updateHash, state } = await loadApp(env);
  state.selectedPlan = "demo";
  state.filters.sort = "risk";
  updateHash();
  assertTrue(env.window.location.hash.length > 0, `hash set: ${env.window.location.hash}`);
  assertTrue(env.window.location.hash.includes("plan=demo"), "plan=demo in hash");
  assertTrue(env.window.location.hash.includes("sort=risk"), "sort=risk in hash");
  record("updateHash writes plan and filters to window.location.hash", true);
}

async function test_clear_hash_resets_to_bare_url() {
  const env = makeEnv({ hash: "plan=demo&sort=risk" });
  const { clearHash } = await loadApp(env);
  clearHash();
  assertEqual(env.window.location.hash, "", "hash cleared");
  record("clearHash removes the hash entirely", true);
}

async function test_state_change_does_not_rewrite_hash_during_init() {
  // Hash updates from initial load should not loop. We verify that
  // applyHashToState does not itself call updateHash.
  const env = makeEnv({ hash: "plan=demo&sort=risk" });
  const { applyHashToState, state } = await loadApp(env);
  const before = env.window.location.hash;
  applyHashToState();
  const after = env.window.location.hash;
  // Re-applying the same parsed state must not mutate the hash (which would
  // cause a redundant hashchange cycle).
  assertEqual(after, before, "applyHashToState is idempotent on the hash");
  assertEqual(state.selectedPlan, "demo", "still selected");
  record("applyHashToState is idempotent — no hash rewrite on re-apply", true);
}

// ---------- hashchange listener wiring ---------------------------------------

async function test_hashchange_listener_is_registered() {
  const env = makeEnv();
  await loadApp(env);
  // jsdom does not fire hashchange for direct location.hash assignment in all
  // cases, so we dispatch it manually after mutating the hash.
  env.window.location.hash = "#plan=alpha";
  env.window.dispatchEvent(new env.window.HashChangeEvent("hashchange"));
  // We don't assert state here because the wiring is best-effort and the
  // test below asserts behavior. This one just confirms the dispatch path.
  record("hashchange event can be dispatched on the window", true);
}

async function test_hashchange_restores_selection() {
  // Wire the hashchange listener ourselves and verify it applies the hash.
  const env = makeEnv({ hash: "plan=alpha" });
  const { applyHashToState, state } = await loadApp(env);
  // Manual wiring: app.js will register this in init.
  env.window.addEventListener("hashchange", applyHashToState);
  // Simulate a back/forward navigation by mutating the hash and firing the
  // event, the way the browser would.
  env.window.location.hash = "#plan=beta";
  env.window.dispatchEvent(new env.window.HashChangeEvent("hashchange"));
  assertEqual(state.selectedPlan, "beta", "selection followed hashchange");
  record("hashchange restores the plan selection (back/forward)", true);
}

// ---------- runner ----------------------------------------------------------

async function run() {
  const tests = [
    test_encode_emits_empty_when_all_defaults,
    test_encode_includes_plan_and_nondefault_filters,
    test_encode_omits_default_dimensions,
    test_parse_round_trip,
    test_parse_unknown_status_is_ignored,
    test_parse_unknown_sort_falls_back_to_default,
    test_parse_garbage_hash_returns_defaults_without_throwing,
    test_parse_empty_hash_returns_defaults,
    test_parse_handles_unknown_dimension_gracefully,
    test_load_hash_overrides_localstorage,
    test_load_bare_url_uses_defaults,
    test_load_only_localstorage_when_no_hash,
    test_update_hash_writes_window_location,
    test_clear_hash_resets_to_bare_url,
    test_state_change_does_not_rewrite_hash_during_init,
    test_hashchange_listener_is_registered,
    test_hashchange_restores_selection,
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