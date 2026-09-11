// Tests for static/app/usage.js — per-backend rows in the usage banner.
//
// Run with:  node tests/unit/test_usage_backends_render.mjs
//
// Follows the JSDOM-harness pattern of tests/test_app_workspace.mjs:
// bootstrap the browser globals through tests/_app_js_loader.mjs (which
// installs globalThis.window / document / localStorage / fetch and keeps
// them set for call-time reads), then import the module under test
// directly with the loader's cache-busting query strategy. renderUsage is
// a pure DOM-string helper, so a small tracked #usage-banner element stub
// stands in for jsdom (which is not installed in this repo).
//
// Written test-first for the per-backend story: today's renderUsage knows
// nothing about usage.backends, so every backends test below FAILS against
// the current file while the preservation tests pass. That mixed state is
// the expected RED for this story — the failures are exactly the new
// behaviour, not harness errors.
//
// Graded criteria covered here:
//   POSITIVE
//   - available:true + backends renders the preserved Claude text
//     ("session N%", "week W%") AND appends a per-backend section after it
//     showing each row's provider, model and role.
//   - a row with ok:false renders its reason.
//   - available:false with a non-empty backends array still renders the
//     banner (the provider-neutral install) and shows the row's provider.
//   NEGATIVE / BOUNDARY
//   - backends missing / [] / null / "nope" render exactly today's output
//     (byte-for-byte) with no per-backend section and no exception.
//   - renderUsage(null) and renderUsage({available:false}) (no backends)
//     hide the banner exactly as today (classList keeps "hidden",
//     innerHTML untouched by the early return).
//   - available:false with an unusable backends array (absent / [] /
//     null / "nope") still hides the banner.
//   - a hostile reason (<img src=x onerror=alert(1)>) is escaped: the HTML
//     contains &lt;img and no literal <img tag; provider, model and role
//     values with metacharacters are escaped too.
//   - rows sharing one provider are de-duplicated to a single mention of
//     that provider/model while the roles stay visible; distinct providers
//     each get their own row content.
//   - the gate_blind branch still renders its full warning text (failure
//     count + blind_since) and adds the blind class with backends set.
//   - the preserved PAUSED marker, the "(measured ...)" muted span, and
//     zero-percent boundary values.
//   - renderUsage still targets the existing #usage-banner element and is
//     still exported; usage.js gains no sha/byte-hash guard in this story.
//
// Deliberately NOT asserted (the brief leaves them open):
//   - the exact markup/classes of the per-backend section (existing
//     classes only; "do LESS instead") — content-level assertions only.
//   - whether the gate_blind branch also lists backends — only its
//     preserved warning text is graded.
//   - how a malformed row (null / {} inside backends) renders — only
//     "must not throw" is graded there.

import { readFileSync } from "node:fs";
import { loadAppInto } from "../_app_js_loader.mjs";

const results = [];

function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${label || "values differ"}: got ${a} want ${e}`);
}

function countOccurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

// ---------- environment ----------

// Swapped out per test; the window stub's fetch delegates to whatever is
// currently installed, so globalThis.fetch (wired by the loader) always
// reaches the active handler.
let currentFetch = null;

function makeWindowStub() {
  const noop = () => {};
  const makeElement = () => ({
    style: {},
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    addEventListener: noop,
    removeEventListener: noop,
    appendChild: noop,
    setAttribute: noop,
    querySelector: () => makeElement(),
    querySelectorAll: () => [],
    children: [],
    innerHTML: "",
    textContent: "",
    scrollTop: 0,
    scrollHeight: 0,
    scrollTo: noop,
  });
  // Defensive document stub: any property read resolves to a no-op (or a
  // stub element for the common lookups), so unknown top-level DOM wiring
  // in the app graph cannot crash the bootstrap.
  const documentStub = new Proxy(
    {},
    {
      get(_target, prop) {
        if (
          prop === "getElementById" ||
          prop === "querySelector" ||
          prop === "createElement"
        ) {
          return () => makeElement();
        }
        if (
          prop === "querySelectorAll" ||
          prop === "getElementsByClassName" ||
          prop === "getElementsByTagName"
        ) {
          return () => [];
        }
        if (prop === "body" || prop === "documentElement" || prop === "head") {
          return makeElement();
        }
        if (prop === "title") return "";
        return noop;
      },
    },
  );
  const win = {
    fetch: (url, opts) => currentFetch(url, opts),
    document: documentStub,
    localStorage: {
      getItem: () => null,
      setItem: noop,
      removeItem: noop,
      clear: noop,
    },
    location: { href: "http://localhost/", pathname: "/", search: "", hash: "" },
    matchMedia: () => ({ matches: false, addListener: noop, removeListener: noop }),
    addEventListener: noop,
    removeEventListener: noop,
    requestAnimationFrame: () => 0,
  };
  win.window = win;
  win.self = win;
  return win;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

// Bootstrap the browser globals (window/document/localStorage/fetch)
// through the shared loader, exactly as the other frontend tests do. The
// loader also imports the full static/app.js graph; that graph's top-level
// DOM wiring may need a fuller environment than this stub provides, but
// the globals are installed BEFORE that import and persist for call-time
// reads (the loader's own comments mandate this), which is all that
// usage.js's bare `document` read needs.
let bootstrapped = false;
async function bootstrapGlobals() {
  if (bootstrapped) return;
  bootstrapped = true;
  currentFetch = async (url, opts) => {
    if (url.includes("/api/plans")) return jsonResponse(200, { plans: [] });
    if (url.includes("/api/usage")) return jsonResponse(200, { available: false });
    return jsonResponse(200, {});
  };
  try {
    await loadAppInto(makeWindowStub());
  } catch {
    // Globals are already wired at this point; the app graph's DOM wiring
    // is out of scope for these pure-helper tests.
  }
}

// Cache-busted re-import per test, mirroring the loader's own dynamic
// import() strategy so a stateful implementation cannot leak across tests.
let importSeq = 0;
async function loadUsageModule() {
  await bootstrapGlobals();
  return import(`../../static/app/usage.js?t=${++importSeq}`);
}

// A tracked #usage-banner element mirroring static/index.html's initial
// markup (class="usage-banner hidden"). classList ops mutate a Set so
// tests can assert hidden/blind state; innerHTML is a plain property so
// the raw string renderUsage assigned is read back verbatim (no HTML
// parsing — escape checks see exactly what the code inserted).
function makeBanner() {
  const classes = new Set(["hidden"]);
  return {
    id: "usage-banner",
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    innerHTML: "",
    _classes: classes,
  };
}

// Point globalThis.document at a stub whose getElementById hands out the
// tracked banner (and records lookups). renderUsage reads the bare
// `document` identifier at call time, which resolves through globalThis,
// so re-pointing it per test is enough; the loader keeps the rest of the
// browser globals installed for the process lifetime by design.
function installBanner() {
  const banner = makeBanner();
  const lookups = [];
  globalThis.document = {
    getElementById: (id) => {
      lookups.push(id);
      return id === "usage-banner" ? banner : null;
    },
  };
  if (globalThis.window) globalThis.window.document = globalThis.document;
  return { banner, lookups };
}

// Pre-seeds innerHTML with a sentinel so tests can also assert that the
// hide path leaves it untouched, exactly as today's early return does.
const SENTINEL = "<!--sentinel-->";

async function renderInto(usage) {
  const mod = await loadUsageModule();
  const { banner, lookups } = installBanner();
  banner.innerHTML = SENTINEL;
  mod.renderUsage(usage);
  return { banner, lookups };
}

async function run(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

// Today's exact normal-branch output for {available:true, session_pct:1,
// week_pct:2} with no paused/measured_at/backends — the string the brief
// says must be preserved byte-for-byte when backends is unusable.
const TODAY_NORMAL =
  'Usage gate: session 1% &middot; week 2% <span class="muted">(measured ?)</span>';

// ---------- exports / target ----------

await run("renderUsage is still exported as a function", async () => {
  const mod = await loadUsageModule();
  assertTrue(
    typeof mod.renderUsage === "function",
    "renderUsage should still be an exported function",
  );
});

await run("renderUsage still renders into the existing #usage-banner element", async () => {
  const { banner, lookups } = await renderInto({
    available: true,
    session_pct: 10,
    week_pct: 20,
    backends: [
      { role: "dispatch", provider: "ollama", model: "glm", ok: true, reason: "" },
    ],
  });
  assertTrue(
    lookups.includes("usage-banner"),
    "getElementById('usage-banner') should be called",
  );
  assertEqual(banner.id, "usage-banner", "the mutated element should be the usage banner");
});

// ---------- positive: per-backend rows ----------

await run(
  "available:true + backends renders the Claude text and appends the backend row",
  async () => {
    const { banner } = await renderInto({
      available: true,
      session_pct: 10,
      week_pct: 20,
      backends: [
        { role: "dispatch", provider: "ollama", model: "glm", ok: true, reason: "" },
      ],
    });
    const html = banner.innerHTML;
    assertTrue(
      html.includes("Usage gate: session 10%"),
      "the preserved Claude text (session 10%) should still render",
    );
    assertTrue(
      html.includes("week 20%"),
      "the preserved Claude text (week 20%) should still render",
    );
    assertTrue(html.includes("ollama"), "the backend row's provider should render");
    assertTrue(html.includes("glm"), "the backend row's model should render");
    assertTrue(html.includes("dispatch"), "the backend row's role should render");
    assertTrue(!banner.classList.contains("hidden"), "the banner should be visible");
    assertTrue(
      html.lastIndexOf("ollama") > html.indexOf("(measured"),
      "the per-backend section should be appended after the existing Claude content",
    );
  },
);

await run("a blocked (ok:false) row renders its reason", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 5,
    week_pct: 6,
    backends: [
      {
        role: "dispatch",
        provider: "openai",
        model: "gpt",
        ok: false,
        reason: "server unreachable",
      },
    ],
  });
  assertTrue(
    banner.innerHTML.includes("server unreachable"),
    "the blocked row's reason should render",
  );
  assertTrue(!banner.classList.contains("hidden"), "the banner should be visible");
});

await run(
  "available:false with a non-empty backends array still renders the banner",
  async () => {
    const { banner } = await renderInto({
      available: false,
      backends: [
        { role: "dispatch", provider: "ollama", model: "glm", ok: true, reason: "" },
      ],
    });
    assertTrue(
      !banner.classList.contains("hidden"),
      "the banner must NOT be hidden when backends is a non-empty array",
    );
    assertTrue(banner.innerHTML.includes("ollama"), "the row's provider should render");
    assertTrue(banner.innerHTML.includes("glm"), "the row's model should render");
    assertTrue(
      !banner.classList.contains("blind"),
      "the blind class must not be added for a provider-neutral install",
    );
  },
);

// ---------- negative / boundary: optional backends ----------

await run("no backends key renders exactly today's output", async () => {
  const { banner } = await renderInto({ available: true, session_pct: 1, week_pct: 2 });
  assertEqual(
    banner.innerHTML,
    TODAY_NORMAL,
    "a payload without backends must render exactly today's output",
  );
  assertTrue(!banner.classList.contains("hidden"), "the banner should be visible");
});

await run("backends: [] renders exactly today's output with no per-backend section", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    backends: [],
  });
  assertEqual(
    banner.innerHTML,
    TODAY_NORMAL,
    "an empty backends array must render exactly today's output",
  );
});

await run("backends: null renders exactly today's output without throwing", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    backends: null,
  });
  assertEqual(
    banner.innerHTML,
    TODAY_NORMAL,
    "a null backends must render exactly today's output",
  );
});

await run("backends: 'nope' (wrong type) renders exactly today's output without throwing", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    backends: "nope",
  });
  assertEqual(
    banner.innerHTML,
    TODAY_NORMAL,
    "a non-array backends must render exactly today's output",
  );
});

// ---------- negative / boundary: hiding, exactly as today ----------

await run("renderUsage(null) hides the banner exactly as today", async () => {
  const { banner } = await renderInto(null);
  assertTrue(
    banner.classList.contains("hidden"),
    "a null payload must hide the banner",
  );
  assertEqual(
    banner.innerHTML,
    SENTINEL,
    "the early return must not touch innerHTML",
  );
});

await run("renderUsage({available:false}) with no backends hides the banner as today", async () => {
  const { banner } = await renderInto({ available: false });
  assertTrue(
    banner.classList.contains("hidden"),
    "available:false with no backends must hide the banner",
  );
  assertEqual(
    banner.innerHTML,
    SENTINEL,
    "hiding must not touch innerHTML",
  );
});

await run("available:false with an unusable backends array still hides the banner", async () => {
  for (const backends of [undefined, [], null, "nope"]) {
    const { banner } = await renderInto({ available: false, backends });
    assertTrue(
      banner.classList.contains("hidden"),
      `available:false + backends=${JSON.stringify(backends) ?? "absent"} must hide the banner`,
    );
  }
});

// ---------- escaping ----------

await run("a hostile reason is escaped (no literal <img tag reaches innerHTML)", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 5,
    week_pct: 6,
    backends: [
      {
        role: "dispatch",
        provider: "ollama",
        model: "glm",
        ok: false,
        reason: "<img src=x onerror=alert(1)>",
      },
    ],
  });
  const html = banner.innerHTML;
  assertTrue(
    html.includes("&lt;img src=x onerror=alert(1)&gt;"),
    "the reason must be escaped through escapeHtml",
  );
  assertTrue(
    !html.includes("<img"),
    "no literal <img tag may reach innerHTML",
  );
});

await run("provider, model and role values are escaped too", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 5,
    week_pct: 6,
    backends: [
      {
        role: 'di"spatch<x>',
        provider: "<b>ollama</b>",
        model: "glm&co",
        ok: true,
        reason: "",
      },
    ],
  });
  const html = banner.innerHTML;
  assertTrue(
    html.includes("&lt;b&gt;ollama&lt;/b&gt;"),
    "the provider value must be escaped",
  );
  assertTrue(html.includes("glm&amp;co"), "the model value must be escaped");
  assertTrue(
    html.includes("di&quot;spatch&lt;x&gt;"),
    "the role value must be escaped",
  );
  assertTrue(!html.includes("<b>"), "no literal <b> tag may reach innerHTML");
  assertTrue(!html.includes("<x>"), "no literal <x> tag may reach innerHTML");
});

// ---------- grouping / de-duplication by provider ----------

await run("rows sharing one provider are de-duplicated to a single provider mention", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 5,
    week_pct: 6,
    backends: [
      { role: "dispatch", provider: "ollama", model: "glm", ok: true, reason: "" },
      { role: "review", provider: "ollama", model: "glm", ok: true, reason: "" },
    ],
  });
  const html = banner.innerHTML;
  assertEqual(
    countOccurrences(html, "ollama"),
    1,
    "a shared provider must be mentioned once, not once per role",
  );
  assertEqual(
    countOccurrences(html, "glm"),
    1,
    "a shared model must be mentioned once, not once per role",
  );
  assertTrue(html.includes("dispatch"), "the first role must still be shown");
  assertTrue(html.includes("review"), "the second role must still be shown");
});

await run("distinct providers each get their own row content", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 5,
    week_pct: 6,
    backends: [
      { role: "dispatch", provider: "ollama", model: "glm", ok: true, reason: "" },
      {
        role: "review",
        provider: "openai",
        model: "gpt",
        ok: false,
        reason: "rate limited",
      },
    ],
  });
  const html = banner.innerHTML;
  assertTrue(
    html.includes("ollama") && html.includes("glm"),
    "the ollama/glm row should render",
  );
  assertTrue(
    html.includes("openai") && html.includes("gpt"),
    "the openai/gpt row should render",
  );
  assertTrue(
    html.includes("rate limited"),
    "the second row's reason should render",
  );
});

// ---------- preserved gate_blind branch ----------

await run("gate_blind still renders its full warning text with backends populated", async () => {
  const { banner } = await renderInto({
    available: true,
    gate_blind: true,
    consecutive_parse_failures: 3,
    blind_since: "2025-01-01T00:00:00Z",
    backends: [
      { role: "dispatch", provider: "ollama", model: "glm", ok: true, reason: "" },
    ],
  });
  const html = banner.innerHTML;
  assertTrue(
    html.includes("Claude usage gate is BLIND"),
    "the warning headline must be preserved",
  );
  assertTrue(
    html.includes("failing OPEN"),
    "the failing-open wording must be preserved",
  );
  assertTrue(
    html.includes("unguarded"),
    "the unguarded wording must be preserved",
  );
  assertTrue(
    html.includes("3 consecutive failures"),
    "consecutive_parse_failures must still be interpolated",
  );
  assertTrue(
    html.includes("2025-01-01T00:00:00Z"),
    "blind_since must still be interpolated",
  );
  assertTrue(banner.classList.contains("blind"), "the blind class must be added");
  assertTrue(!banner.classList.contains("hidden"), "the banner should be visible");
});

// ---------- preserved normal-branch details ----------

await run("the preserved PAUSED marker still renders", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    paused: true,
  });
  assertTrue(
    banner.innerHTML.includes("<strong>PAUSED</strong>"),
    "the PAUSED marker must be preserved",
  );
  assertTrue(
    banner.innerHTML.includes("Usage gate: session 1%"),
    "the Claude text must still render",
  );
});

await run("the preserved (measured ...) muted span still renders", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    measured_at: "2025-06-01T12:00:00Z",
  });
  assertTrue(
    banner.innerHTML.includes('<span class="muted">(measured 2025-06-01T12:00:00Z)</span>'),
    "the measured_at muted span must be preserved",
  );
});

await run("zero-percent boundary values still render", async () => {
  const { banner } = await renderInto({
    available: true,
    session_pct: 0,
    week_pct: 0,
    backends: [],
  });
  assertTrue(banner.innerHTML.includes("session 0%"), "session_pct 0 must render");
  assertTrue(banner.innerHTML.includes("week 0%"), "week_pct 0 must render");
});

// ---------- malformed rows ----------

await run("malformed rows inside backends must not throw", async () => {
  const a = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    backends: [null],
  });
  assertTrue(
    a.banner.innerHTML.includes("session 1%"),
    "the Claude text must still render alongside a malformed row",
  );
  const b = await renderInto({
    available: true,
    session_pct: 1,
    week_pct: 2,
    backends: [{}],
  });
  assertTrue(
    b.banner.innerHTML.includes("session 1%"),
    "the Claude text must still render alongside an empty row",
  );
  await renderInto({ available: false, backends: [null] }); // must not throw
});

// ---------- story hygiene ----------

await run("usage.js gains no sha/byte-hash guard in this story", async () => {
  const src = readFileSync(
    new URL("../../static/app/usage.js", import.meta.url),
    "utf8",
  );
  assertTrue(
    !/sha[-_]?256|byte[-_]?hash/i.test(src),
    "no sha/byte-hash guard should be added to usage.js",
  );
});

// ---------- summary ----------

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log("FAILURES:");
  for (const r of failed) {
    console.log(`  - ${r.name}${r.detail ? ` — ${r.detail}` : ""}`);
  }
}
process.exit(failed.length ? 1 : 0);