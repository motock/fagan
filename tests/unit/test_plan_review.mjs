// Tests for the plan-review panel in static/app/render/plan-review.js.
//
// Run with:  node tests/unit/test_plan_review.mjs
//
// Covers:
//   - renderPlanReview(container, plan) fetches /api/plans/<plan>/review and
//     renders approval status, warnings, owner/team, viewer and self-approval
//     notes and one row per story, from textContent only.
//   - Approve / Request-changes controls exist only for pending_approval.
//   - Approve posts exactly {digest: current_digest} of the payload on screen,
//     and is disabled while digest_current is false.
//   - Request-changes enforces the 1..2000 character bound on the stripped
//     reason and posts the stripped reason.
//   - Every decision refetches the review payload and re-renders from it.
//   - Errors map to fixed text only; server body and digest never appear.
//   - A double click posts once.
//
// Harness: hand-rolled window / document / fetch stubs, same approach as
// tests/unit/test_plan_controls.mjs (jsdom is not installed in this repo).

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
  return {
    tagName: String(tagName).toUpperCase(),
    children: [],
    listeners: {},
    dataset: {},
    attributes: {},
    className: "",
    disabled: false,
    value: "",
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
    // Fire every listener for `type`; return the first handler's result so a
    // test can await the decision it started.
    fire(type) {
      let first;
      for (const fn of this.listeners[type] || []) {
        const out = fn({ type, target: this });
        if (first === undefined) first = out;
      }
      return first;
    },
    click() {
      return this.fire("click");
    },
  };
}

globalThis.document = {
  createElement(tagName) {
    return makeElement(tagName);
  },
};

// Every descendant (depth-first) matching the predicate.
function findAll(root, pred, out = []) {
  for (const child of root.children) {
    if (pred(child)) out.push(child);
    findAll(child, pred, out);
  }
  return out;
}

function actionButton(root, action) {
  return findAll(root, (el) => el.dataset.reviewAction === action)[0];
}

function reasonBox(root) {
  return findAll(root, (el) => el.tagName === "TEXTAREA")[0];
}

function counterEl(root) {
  return findAll(root, (el) => el.dataset.reviewField === "counter")[0];
}

// Sets the textarea value the way a keystroke would, then fires `input`.
function typeReason(root, value) {
  const box = reasonBox(root);
  box.value = value;
  box.fire("input");
}

// ---------- fetch stub ----------

const DIGEST = "a".repeat(64);
const DIGEST_NEW = "b".repeat(64);

function approval(status, extra = {}) {
  return {
    status,
    digest: DIGEST,
    decided_by: null,
    at: null,
    self_approved: false,
    reason: null,
    ...extra,
  };
}

function payload(overrides = {}) {
  return {
    plan: "alpha",
    approval: approval("pending_approval"),
    current_digest: DIGEST,
    digest_current: true,
    viewer_is_author: false,
    owner: "ops@example.com",
    team: "platform",
    role_config: {},
    stories: [
      {
        key: "S-1",
        summary: "Add login",
        risk: "low",
        persona: "software-engineer",
        files: ["a.py", "b.py"],
      },
    ],
    warnings: ["no rollback plan"],
    ...overrides,
  };
}

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

let current = payload();
let log = [];
let responder = null;

function defaultResponder(method) {
  if (method === "GET") return jsonResponse(200, current);
  return jsonResponse(200, { ok: true });
}

globalThis.fetch = async (url, init = {}) => {
  if (url === "/auth/status") return jsonResponse(404, {});
  const method = init.method || "GET";
  const body = init.body !== undefined ? JSON.parse(init.body) : undefined;
  log.push({ method, url, body });
  return responder(method, url, body);
};

globalThis.window = {
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { pathname: "/", search: "", assign: () => {} },
};

const mod = await import("../../static/app/render/plan-review.js");
const { renderPlanReview, resetPlanReviewState } = mod;

const REVIEW_URL = "/api/plans/alpha/review";
const APPROVE_URL = "/api/plans/alpha/review/approve";
const REQUEST_URL = "/api/plans/alpha/review/request-changes";

const posts = (url) => log.filter((e) => e.method === "POST" && (!url || e.url === url));
const gets = () => log.filter((e) => e.method === "GET");

function reset() {
  current = payload();
  log = [];
  responder = defaultResponder;
  resetPlanReviewState();
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

async function openPanel(container = makeElement("div")) {
  await renderPlanReview(container, "alpha");
  return container;
}

const APPROVE_BLOCKED = "The plan changed since it was submitted. Reload to review the current version.";
const FORBIDDEN_TEXT = "Sign in with your organisation account (or you are not allowed to decide this plan).";
const CONFLICT_TEXT = "This plan changed or was already decided. Reload and review it again.";
const GONE_TEXT = "This plan no longer exists.";
const GENERIC_TEXT = "Could not record the decision. Try again.";

// ---------- POSITIVE: rendering ----------

await runTest("renders status, owner, team, warnings and one row per story", async () => {
  const container = await openPanel();
  const text = container.textContent;
  assertTrue(text.includes("pending_approval"), "approval status shown");
  assertTrue(text.includes("ops@example.com"), "owner shown");
  assertTrue(text.includes("platform"), "team shown");
  assertTrue(text.includes("no rollback plan"), "warning shown");
  const rows = findAll(container, (el) => el.dataset.reviewRow === "story");
  assertEqual(rows.length, 1, "story rows");
  assertTrue(rows[0].textContent.includes("S-1"), "row key");
  assertTrue(rows[0].textContent.includes("Add login"), "row summary");
  assertTrue(rows[0].textContent.includes("low"), "row risk");
  assertTrue(rows[0].textContent.includes("software-engineer"), "row persona");
  assertTrue(rows[0].textContent.includes("2"), "row files count");
});

await runTest("viewer who ingested the plan sees the authorship note", async () => {
  current = payload({ viewer_is_author: true });
  const container = await openPanel();
  assertTrue(container.textContent.includes("You ingested this plan"), "authorship note");
});

await runTest("viewer who did not ingest the plan sees no authorship note", async () => {
  current = payload({ viewer_is_author: false });
  const container = await openPanel();
  assertTrue(!container.textContent.includes("You ingested this plan"), "no authorship note");
});

await runTest("self-approved decision shows the self-approval note", async () => {
  current = payload({ approval: approval("approved", { self_approved: true }) });
  const container = await openPanel();
  assertTrue(container.textContent.includes("approved by its own author"), "self-approval note");
});

await runTest("approved decision without self-approval shows no self-approval note", async () => {
  current = payload({ approval: approval("approved", { self_approved: false }) });
  const container = await openPanel();
  assertTrue(!container.textContent.includes("approved by its own author"), "no self-approval note");
});

// ---------- POSITIVE: controls and decisions ----------

await runTest("pending plan shows Approve and Request-changes controls", async () => {
  const container = await openPanel();
  assertTrue(actionButton(container, "approve"), "approve button");
  assertTrue(actionButton(container, "request-changes"), "request-changes button");
  assertTrue(reasonBox(container), "reason textarea");
});

await runTest("approve posts exactly {digest: current_digest} of the displayed payload", async () => {
  current = payload({ current_digest: DIGEST, digest_current: true });
  const container = await openPanel();
  await actionButton(container, "approve").click();
  assertEqual(posts(APPROVE_URL).map((e) => e.body), [{ digest: DIGEST }], "approve body");
});

await runTest("after a 409 the next approve uses the digest from the refetched payload", async () => {
  const container = await openPanel();
  responder = (method) => {
    if (method === "POST") {
      // The plan changed on the server between the two fetches.
      current = payload({ current_digest: DIGEST_NEW, approval: approval("pending_approval", { digest: DIGEST_NEW }) });
      return jsonResponse(409, { detail: "stale" });
    }
    return defaultResponder(method);
  };
  await actionButton(container, "approve").click();
  responder = defaultResponder;
  await actionButton(container, "approve").click();
  assertEqual(posts(APPROVE_URL).map((e) => e.body), [{ digest: DIGEST }, { digest: DIGEST_NEW }], "approve bodies");
});

await runTest("request-changes posts the stripped reason with the displayed digest", async () => {
  const container = await openPanel();
  typeReason(container, "  needs a rollback plan  ");
  await actionButton(container, "request-changes").click();
  assertEqual(
    posts(REQUEST_URL).map((e) => e.body),
    [{ digest: DIGEST, reason: "needs a rollback plan" }],
    "request-changes body",
  );
});

await runTest("a successful decision refetches the review and re-renders from it", async () => {
  const container = await openPanel();
  current = payload({ approval: approval("approved", { decided_by: "alice" }) });
  const before = gets().length;
  await actionButton(container, "approve").click();
  assertEqual(gets().length - before, 1, "refetch count");
  assertTrue(container.textContent.includes("approved"), "re-rendered status");
  assertEqual(actionButton(container, "approve"), undefined, "controls gone after approval");
});

// ---------- NEGATIVE: controls ----------

await runTest("approved plan shows no decision controls", async () => {
  current = payload({ approval: approval("approved") });
  const container = await openPanel();
  assertEqual(actionButton(container, "approve"), undefined, "approve");
  assertEqual(actionButton(container, "request-changes"), undefined, "request-changes");
  assertEqual(reasonBox(container), undefined, "textarea");
});

await runTest("rejected plan shows no decision controls", async () => {
  current = payload({ approval: approval("rejected") });
  const container = await openPanel();
  assertEqual(actionButton(container, "approve"), undefined, "approve");
  assertEqual(actionButton(container, "request-changes"), undefined, "request-changes");
});

await runTest("plan with no approval record shows no decision controls", async () => {
  current = payload({ approval: approval(null) });
  const container = await openPanel();
  assertEqual(actionButton(container, "approve"), undefined, "approve");
  assertEqual(actionButton(container, "request-changes"), undefined, "request-changes");
});

await runTest("approve is disabled with a fixed explanation when digest_current is false", async () => {
  current = payload({ digest_current: false, current_digest: DIGEST_NEW });
  const container = await openPanel();
  const button = actionButton(container, "approve");
  assertEqual(button.disabled, true, "approve disabled");
  assertTrue(container.textContent.includes(APPROVE_BLOCKED), "fixed explanation");
});

await runTest("disabled approve does not post when clicked", async () => {
  current = payload({ digest_current: false, current_digest: DIGEST_NEW });
  const container = await openPanel();
  await actionButton(container, "approve").click();
  assertEqual(posts(APPROVE_URL).length, 0, "no approve post");
});

// ---------- BOUNDARY: request-changes reason length ----------

const reasonCases = [
  { label: "empty", value: "", enabled: false, length: 0 },
  { label: "whitespace only", value: "   \n\t  ", enabled: false, length: 0 },
  { label: "one character", value: "x", enabled: true, length: 1 },
  { label: "2000 characters", value: "x".repeat(2000), enabled: true, length: 2000 },
  { label: "2001 characters", value: "x".repeat(2001), enabled: false, length: 2001 },
  { label: "2000 stripped plus padding", value: `  ${"x".repeat(2000)}  `, enabled: true, length: 2000 },
];

for (const c of reasonCases) {
  await runTest(`request-changes with ${c.label} sets submit enabled=${c.enabled}`, async () => {
    const container = await openPanel();
    typeReason(container, c.value);
    assertEqual(actionButton(container, "request-changes").disabled, !c.enabled, "submit disabled");
    assertEqual(counterEl(container).textContent, `${c.length} / 2000 characters`, "counter");
  });
}

await runTest("request-changes outside the bound posts nothing", async () => {
  const container = await openPanel();
  typeReason(container, "x".repeat(2001));
  await actionButton(container, "request-changes").click();
  assertEqual(posts(REQUEST_URL).length, 0, "no request-changes post");
});

await runTest("whitespace-only reason posts nothing", async () => {
  const container = await openPanel();
  typeReason(container, "   ");
  await actionButton(container, "request-changes").click();
  assertEqual(posts(REQUEST_URL).length, 0, "no request-changes post");
});

// ---------- NEGATIVE: double click ----------

await runTest("double click on approve posts once", async () => {
  const container = await openPanel();
  const button = actionButton(container, "approve");
  const first = button.click();
  const second = button.click();
  await Promise.all([first, second]);
  assertEqual(posts(APPROVE_URL).length, 1, "approve posts");
});

// ---------- NEGATIVE: errors show fixed text only ----------

const SECRET = "SECRET-SERVER-DETAIL";

const errorCases = [
  { label: "403", status: 403, text: FORBIDDEN_TEXT },
  { label: "404", status: 404, text: GONE_TEXT },
  { label: "409", status: 409, text: CONFLICT_TEXT },
  { label: "500", status: 500, text: GENERIC_TEXT },
];

for (const c of errorCases) {
  await runTest(`approve ${c.label} shows only the fixed text`, async () => {
    const container = await openPanel();
    responder = (method) => {
      if (method === "POST") return jsonResponse(c.status, { detail: `${SECRET} ${DIGEST}` });
      return defaultResponder(method);
    };
    await actionButton(container, "approve").click();
    const text = container.textContent;
    assertTrue(text.includes(c.text), `fixed text ${c.text}`);
    assertTrue(!text.includes(SECRET), "server detail hidden");
    assertTrue(!text.includes(DIGEST), "digest hidden");
  });
}

await runTest("network failure shows the generic text and no error detail", async () => {
  const container = await openPanel();
  responder = (method) => {
    if (method === "POST") throw new TypeError("network down at /review/approve");
    return defaultResponder(method);
  };
  await actionButton(container, "approve").click();
  const text = container.textContent;
  assertTrue(text.includes(GENERIC_TEXT), "generic text");
  assertTrue(!text.includes("network down"), "error message hidden");
});

await runTest("request-changes 403 shows the fixed text and no server body", async () => {
  const container = await openPanel();
  typeReason(container, "please fix");
  responder = (method) => {
    if (method === "POST") return jsonResponse(403, { detail: SECRET });
    return defaultResponder(method);
  };
  await actionButton(container, "request-changes").click();
  const text = container.textContent;
  assertTrue(text.includes(FORBIDDEN_TEXT), "fixed text");
  assertTrue(!text.includes(SECRET), "server detail hidden");
});

// ---------- NEGATIVE: markup in server text stays text ----------

await runTest("story summary containing an img onerror renders as text, not an element", async () => {
  const hostile = '<img src=x onerror="alert(1)">';
  current = payload({ stories: [{ key: "S-2", summary: hostile, risk: "low", persona: "x", files: [] }] });
  const container = await openPanel();
  const imgs = findAll(container, (el) => el.tagName === "IMG");
  assertEqual(imgs.length, 0, "no img elements");
  assertTrue(container.textContent.includes(hostile), "summary shown verbatim");
});

await runTest("warning containing markup renders as text", async () => {
  const hostile = "<script>steal()</script>";
  current = payload({ warnings: [hostile] });
  const container = await openPanel();
  assertEqual(findAll(container, (el) => el.tagName === "SCRIPT").length, 0, "no script elements");
  assertTrue(container.textContent.includes(hostile), "warning shown verbatim");
});

// ---------- summary ----------

const failed = results.filter((r) => !r.ok);
// eslint-disable-next-line no-console
console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
if (failed.length) process.exit(1);
