// Tests for the Diff section wired into static/app/render/story-modal.js.
//
// Run with:  node tests/unit/test_story_diff_modal.mjs

import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MODAL_PATH = path.join(HERE, "..", "..", "static", "app", "render", "story-modal.js");

function makeEl() {
  const el = {
    dataset: {}, innerHTML: "", textContent: "", options: [], value: "",
    classList: { _s: new Set(["hidden"]),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    listeners: {},
    addEventListener(t, fn) { this.listeners[t] = fn; },
    querySelector() { return null; },
    appendChild() {},
  };
  return el;
}

const els = { "story-modal": makeEl(), "story-modal-body": makeEl() };
// The body stands in for the diff slot container: querySelector returns slot
// stubs for the diff selectors only.
const slot = makeEl();
const summaryHost = makeEl();
els["story-modal-body"].querySelector = (sel) => {
  if (sel === "[data-story-diff-slot]") return slot;
  return null;
};
slot.querySelector = (sel) => (sel === "[data-story-diff-empty]" ? summaryHost : null);

globalThis.window = globalThis;
globalThis.document = {
  getElementById: (id) => els[id] || null,
  createElement: () => makeEl(),
  addEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
};
globalThis.localStorage = { getItem: () => null, setItem() {} };

const calls = [];
let pending = [];
globalThis.fetch = (url) => {
  calls.push(url);
  if (String(url).includes("/diff")) {
    return new Promise((resolve) => pending.push({ url, resolve }));
  }
  return Promise.resolve({ ok: false, json: async () => ({}) });
};
const respond = (i, body) => pending[i].resolve({ ok: true, json: async () => body });
const diffCalls = () => calls.filter((u) => String(u).includes("/diff"));
const tick = () => new Promise((r) => setImmediate(r));

const { state } = await import(`${pathToFileURL(path.join(HERE, "..", "..", "static", "app", "state.js")).href}`);
state.selectedPlan = "plan-a";
const { initStoryModal, showStoryModal } = await import(`${pathToFileURL(MODAL_PATH).href}?t=${Date.now()}`);
initStoryModal({ backendErrorEl: () => null, notifSeverityColor: { info: "--c-x" } });

const SUMMARY = {
  available: true, totals: { files: 1, added: 1, removed: 0 }, truncated: false,
  files: [{ path: "a.py", status: "M", added: 1, removed: 0 }],
};
let passed = 0;
const failures = [];
async function test(name, fn) {
  calls.length = 0; pending = []; slot.innerHTML = ""; summaryHost.innerHTML = "";
  try { await fn(); passed += 1; } catch (e) { failures.push(`${name}: ${e.message}`); }
}
const open = (key) => showStoryModal("plan-a", { summary: "s" }, key, []);

await test("should_place_diff_slot_after_notifications_slot", async () => {
  open("WTD-6");
  const html = els["story-modal-body"].innerHTML;
  assert.ok(html.indexOf("data-notifications-slot") < html.indexOf("data-story-diff-slot"));
  assert.ok(html.indexOf("data-story-diff-slot") < html.indexOf("dsh-modal-log-section"));
  assert.ok(html.includes("No diff available."));
});

await test("should_issue_exactly_one_summary_request_on_open", async () => {
  open("WTD-6");
  await tick();
  assert.equal(diffCalls().length, 1);
  assert.equal(diffCalls()[0], "/api/plans/plan-a/stories/WTD-6/diff");
});

await test("should_render_summary_and_fetch_file_once_on_click", async () => {
  open("WTD-6");
  respond(0, SUMMARY);
  await tick();
  assert.ok(slot.innerHTML.includes("story-diff-summary"));
  const row = {};
  slot.querySelectorAll = () => [row];
  slot.listeners.click({ target: { closest: () => row } });
  await tick();
  assert.equal(diffCalls().length, 2);
  assert.equal(diffCalls()[1], "/api/plans/plan-a/stories/WTD-6/diff?path=a.py");
  respond(1, { available: true, path: "a.py", diff: "+x\n" });
  await tick();
  assert.ok(slot.innerHTML.includes("story-diff-file-diff"));
});

await test("should_ignore_stale_response_for_another_story", async () => {
  open("WTD-6");
  open("WTD-7");
  respond(0, SUMMARY);
  await tick();
  assert.ok(!slot.innerHTML.includes("story-diff-summary"));
});

await test("should_not_issue_second_request_when_time_advances", async () => {
  open("WTD-6");
  respond(0, SUMMARY);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(diffCalls().length, 1);
});

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`${passed} passed`);
