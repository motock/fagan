// Tests for the pause/resume control in the plan detail header.
//
// Run with:  node tests/unit/test_plan_controls.mjs
//
// Covers static/app/render/plan-controls.js:
//   - togglePlanPaused(planName, currentlyPaused) POSTs to /pause or /resume,
//     refetches through the injected refresh on success, and returns
//     {ok:true} or {ok:false, status} without ever rethrowing.
//   - mountPlanControls(slot, plan) renders one button for approved plans
//     (and for paused plans, so a paused plan can always be resumed), with the
//     fixed messages for 403 / 404 / anything else, and disables the button
//     while a request is in flight.
//
// Harness: a hand-rolled window / document / fetch stub (jsdom is not
// installed in this repo, see tests/test_app_workspace_wiring.mjs). state.js
// writes window.state at import time, so the window stub is installed before
// the module under test is imported.

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail: ok ? "" : detail || "" });
  // eslint-disable-next-line no-console
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : ` — ${detail}`}`);
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${label || "values differ"}: got ${a} want ${e}`);
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

// ---------- DOM stubs ----------

function makeElement(tagName) {
  const el = {
    tagName: String(tagName).toUpperCase(),
    children: [],
    listeners: {},
    dataset: {},
    attributes: {},
    className: "",
    disabled: false,
    _text: "",
    get textContent() {
      return this._text + this.children.map((c) => c.textContent).join("");
    },
    set textContent(value) {
      this.children = [];
      this._text = String(value);
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    },
    // Fire every click listener; return the first promise so tests can await
    // the handler's completion.
    click() {
      let first;
      for (const fn of this.listeners.click || []) {
        const out = fn({ type: "click", target: this });
        if (first === undefined) first = out;
      }
      return first;
    },
  };
  return el;
}

// Every element created through document.createElement, so a test can prove
// that no <script> node was ever built from a plan name.
let createdElements = [];
let mountedSlots = [];

const documentStub = {
  body: makeElement("body"),
  activeElement: null,
  createElement(tagName) {
    const el = makeElement(tagName);
    createdElements.push(el);
    return el;
  },
  querySelectorAll(selector) {
    if (selector !== "[data-plan-control-slot]") return [];
    return mountedSlots;
  },
};

function makeSlot(planName) {
  const slot = makeElement("span");
  slot.dataset.planName = planName;
  slot.dataset.planControlSlot = "";
  mountedSlots.push(slot);
  return slot;
}

function buttonIn(slot) {
  return slot.children.find((c) => c.tagName === "BUTTON");
}

function messageIn(slot) {
  return slot.children.find((c) => c.tagName === "SPAN" && c.className === "plan-control-msg");
}

// ---------- fetch stub ----------

// Each test installs a responder: (url, init) => Promise<{ok, status, json}>.
// Requests to /auth/status (the login probe api.js makes after any non-OK
// response) are answered 404 and kept out of the POST log.
let responder = null;
let posts = [];

function installFetch() {
  globalThis.fetch = async (url, init = {}) => {
    if (url === "/auth/status") {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    posts.push(`${init.method || "GET"} ${url}`);
    return responder(url, init);
  };
}

const okResponse = () => ({ ok: true, status: 200, json: async () => ({ ok: true }) });
const statusResponse = (status) => ({ ok: false, status, json: async () => ({ detail: "secret <b>internal</b>" }) });

globalThis.document = documentStub;
globalThis.window = {
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { pathname: "/", search: "", assign: () => {} },
  fetch: (...args) => globalThis.fetch(...args),
};
installFetch();

const mod = await import("../../static/app/render/plan-controls.js");
const { togglePlanPaused, initPlanControls, mountPlanControls, resetPlanControlsState } = mod;

let refreshCount = 0;
initPlanControls({
  refresh: async () => {
    refreshCount += 1;
  },
});

const APPROVED = { name: "alpha", paused: false, approval: { status: "approved" } };
const APPROVED_PAUSED = { name: "alpha", paused: true, approval: { status: "approved" } };

function reset() {
  posts = [];
  refreshCount = 0;
  createdElements = [];
  mountedSlots = [];
  responder = () => okResponse();
  resetPlanControlsState();
}

async function runTest(name, fn) {
  reset();
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

const FORBIDDEN_TEXT = "You are not permitted to pause or resume this plan.";
const GONE_TEXT = "This plan no longer exists.";
const GENERIC_TEXT = "Could not change the plan. Try again.";

// ---------- POSITIVE ----------

await runTest("pause click on an active plan posts to /pause and refreshes", async () => {
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  const button = buttonIn(slot);
  assertEqual(button.textContent, "Pause plan", "label");
  await button.click();
  assertEqual(posts, ["POST /api/plans/alpha/pause"], "requests");
  assertEqual(refreshCount, 1, "refresh count");
});

await runTest("resume click on a paused plan posts to /resume and refreshes", async () => {
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED_PAUSED);
  const button = buttonIn(slot);
  assertEqual(button.textContent, "Resume plan", "label");
  await button.click();
  assertEqual(posts, ["POST /api/plans/alpha/resume"], "requests");
  assertEqual(refreshCount, 1, "refresh count");
});

await runTest("togglePlanPaused returns {ok:true} on success", async () => {
  const result = await togglePlanPaused("alpha", false);
  assertEqual(result, { ok: true }, "result");
});

// ---------- NEGATIVE: failure statuses ----------

await runTest("403 returns status 403 and does not refresh", async () => {
  responder = () => statusResponse(403);
  const result = await togglePlanPaused("alpha", false);
  assertEqual(result, { ok: false, status: 403 }, "result");
  assertEqual(refreshCount, 0, "refresh count");
});

await runTest("403 shows the fixed not-permitted text and never server text", async () => {
  responder = () => statusResponse(403);
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  await buttonIn(slot).click();
  assertEqual(messageIn(slot).textContent, FORBIDDEN_TEXT, "message");
  assertTrue(!messageIn(slot).textContent.includes("secret"), "server text leaked");
  assertEqual(refreshCount, 0, "refresh count");
});

await runTest("404 shows the gone text and does not refresh", async () => {
  responder = () => statusResponse(404);
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  await buttonIn(slot).click();
  assertEqual(messageIn(slot).textContent, GONE_TEXT, "message");
  assertEqual(refreshCount, 0, "refresh count");
});

await runTest("500 shows the generic text", async () => {
  responder = () => statusResponse(500);
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  await buttonIn(slot).click();
  assertEqual(messageIn(slot).textContent, GENERIC_TEXT, "message");
  assertEqual(refreshCount, 0, "refresh count");
});

await runTest("network error resolves with status null and never rethrows", async () => {
  responder = () => {
    throw new TypeError("Failed to fetch");
  };
  const result = await togglePlanPaused("alpha", false);
  assertEqual(result, { ok: false, status: null }, "result");
  assertEqual(refreshCount, 0, "refresh count");
});

await runTest("network error shows the generic text", async () => {
  responder = () => {
    throw new TypeError("Failed to fetch");
  };
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  await buttonIn(slot).click();
  assertEqual(messageIn(slot).textContent, GENERIC_TEXT, "message");
});

// ---------- NEGATIVE: in-flight guard ----------

await runTest("button is disabled during the request and a second click posts nothing", async () => {
  let release;
  responder = () => new Promise((resolve) => {
    release = () => resolve(okResponse());
  });
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  const button = buttonIn(slot);
  const inFlight = button.click();
  assertEqual(button.disabled, true, "disabled while in flight");
  button.click();
  assertEqual(posts.length, 1, "POSTs after second click");
  release();
  await inFlight;
  assertEqual(refreshCount, 1, "refresh count");
});

await runTest("button is re-enabled after a failed request", async () => {
  responder = () => statusResponse(500);
  const slot = makeSlot("alpha");
  mountPlanControls(slot, APPROVED);
  await buttonIn(slot).click();
  assertEqual(buttonIn(slot).disabled, false, "disabled after failure");
});

// ---------- NEGATIVE: visibility ----------

await runTest("unapproved plan renders no control", async () => {
  const slot = makeSlot("alpha");
  mountPlanControls(slot, { name: "alpha", paused: false, approval: { status: "pending_approval" } });
  assertEqual(buttonIn(slot), undefined, "button");
});

await runTest("plan without an approval record renders no control", async () => {
  const slot = makeSlot("alpha");
  mountPlanControls(slot, { name: "alpha", paused: false });
  assertEqual(buttonIn(slot), undefined, "button");
});

await runTest("paused plan stays resumable even if approval is no longer approved", async () => {
  const slot = makeSlot("alpha");
  mountPlanControls(slot, { name: "alpha", paused: true, approval: { status: "pending_approval" } });
  assertEqual(buttonIn(slot).textContent, "Resume plan", "label");
});

// ---------- NEGATIVE: plan names are encoded and never markup ----------

await runTest("plan name with slash and script tag is encoded in the URL", async () => {
  const name = "a/b<script>x";
  const slot = makeSlot(name);
  mountPlanControls(slot, { ...APPROVED, name });
  await buttonIn(slot).click();
  assertEqual(
    posts,
    ["POST /api/plans/a%2Fb%3Cscript%3Ex/pause"],
    "requests",
  );
});

await runTest("plan name with script tag is never built as an element", async () => {
  const name = "<script>alert(1)</script>";
  const slot = makeSlot(name);
  mountPlanControls(slot, { ...APPROVED, name });
  assertTrue(!createdElements.some((el) => el.tagName === "SCRIPT"), "script element created");
  assertEqual(buttonIn(slot).textContent, "Pause plan", "button text");
});

const failures = results.filter((r) => !r.ok);
// eslint-disable-next-line no-console
console.log(`\n${results.length - failures.length} passed, ${failures.length} failed`);
if (failures.length) process.exitCode = 1;
