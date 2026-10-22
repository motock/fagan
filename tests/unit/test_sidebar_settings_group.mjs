// Tests for the sidebar's bottom-pinned SYSTEM settings group (SIDEBAR-1).
//
// Run with:  node tests/unit/test_sidebar_settings_group.mjs
//
// The group is rendered by plan-list.js's `_renderSettingsGroup(nav)`, called
// at the end of renderPlanList's first-call branch so it sits BELOW the plan
// rows and the pinned footer. Its two items (Workspace, Config) are wired
// through the callbacks main.js injects via initPlanList({...}).
//
// Harness notes (mirrors tests/unit/test_plan_list_scoping.mjs):
//   - jsdom is not installed in this repo, so a hand-rolled document stub is
//     used. Unlike the scoping suite's stub, this one's querySelector returns
//     null when nothing matches (real-browser behaviour) and its innerHTML
//     setter parses the simple, fully-closed markup these modules emit into
//     real child elements. Both matter here: the implementation's
//     "already rendered?" guard is a querySelector null-check, and the plan
//     rows' archive button is looked up by class after innerHTML is set.
//   - plan-list.js is imported directly (not through app.js) so the test
//     exercises only the sidebar renderer and never boots main.js's polling.
//     Browser globals are installed on globalThis BEFORE the import because
//     state.js reads `window` at module-evaluation time.
//   - plan-list.js keeps module-level state (planListRowsByName), so every
//     test calls resetPlanListState() and clears #plan-list first.
//
// Grading scope: THIS story only. Assertions are membership / behaviour
// based — never byte hashes or exact file contents.

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

const noop = () => {};

// ---------- element / document stubs ----------

function matches(el, sel) {
  const s = String(sel).trim();
  if (!s) return false;
  if (s.startsWith("[") && s.endsWith("]")) {
    return el.getAttribute(s.slice(1, -1)) !== null;
  }
  if (s.startsWith(".")) {
    return el._className.split(/\s+/).includes(s.slice(1));
  }
  return el.tagName === s.toUpperCase();
}

function makeTextNode(text) {
  return {
    nodeType: 3,
    tagName: "#TEXT",
    children: [],
    parentNode: null,
    _className: "",
    _textContent: String(text),
    getAttribute: () => null,
    get textContent() {
      return this._textContent;
    },
  };
}

// Find the close tag matching the open tag of `tag` that ends at `from`,
// counting nested same-name tags so `<div><div></div></div>` pairs correctly.
function findCloseTag(src, tag, from) {
  const re = new RegExp(`<(/?)${tag}(?:\\s[^>]*)?>`, "gi");
  re.lastIndex = from;
  let depth = 1;
  let m;
  while ((m = re.exec(src))) {
    if (m[1] === "/") {
      depth -= 1;
      if (depth === 0) return { start: m.index, end: m.index + m[0].length };
    } else {
      depth += 1;
    }
  }
  return null;
}

function applyAttrs(el, attrSrc) {
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"/g;
  let m;
  while ((m = re.exec(attrSrc))) {
    // `class` is a live property on real elements, not just an attribute —
    // .classList / querySelector(".foo") must see it.
    if (m[1].toLowerCase() === "class") el.className = m[2];
    else el.setAttribute(m[1], m[2]);
  }
}

// Parse the simple, fully-closed markup these modules emit into real child
// elements. Anything unparseable is kept as text so nothing is silently lost.
function parseInto(parent, html) {
  const src = String(html);
  let i = 0;
  while (i < src.length) {
    const lt = src.indexOf("<", i);
    if (lt === -1) {
      const text = src.slice(i).trim();
      if (text) parent.appendChild(makeTextNode(text));
      return;
    }
    if (lt > i) {
      const text = src.slice(i, lt).trim();
      if (text) parent.appendChild(makeTextNode(text));
    }
    const gt = src.indexOf(">", lt);
    if (gt === -1) return;
    const m = /^([a-zA-Z][a-zA-Z0-9]*)([\s\S]*)$/.exec(src.slice(lt + 1, gt));
    if (!m) {
      i = gt + 1;
      continue;
    }
    const tag = m[1];
    const child = makeElement(tag);
    applyAttrs(child, m[2]);
    const close = findCloseTag(src, tag, gt + 1);
    if (close) {
      parseInto(child, src.slice(gt + 1, close.start));
      i = close.end;
    } else {
      i = gt + 1;
    }
    parent.appendChild(child);
  }
}

function makeElement(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    nodeType: 1,
    children: [],
    parentNode: null,
    style: {},
    dataset: {},
    _attrs: Object.create(null),
    _className: "",
    _innerHTML: "",
    _textContent: "",
    _listeners: Object.create(null),
    value: "",
    checked: false,
    type: "",
    title: "",
    id: "",
    offsetWidth: 0,
    scrollTop: 0,
    scrollHeight: 0,
    hidden: false,
  };

  el.classList = {
    add(c) {
      const set = new Set(el._className.split(/\s+/).filter(Boolean));
      set.add(c);
      el._className = [...set].join(" ");
    },
    remove(c) {
      const set = new Set(el._className.split(/\s+/).filter(Boolean));
      set.delete(c);
      el._className = [...set].join(" ");
    },
    contains(c) {
      return el._className.split(/\s+/).includes(c);
    },
    toggle(c, force) {
      const has = el.classList.contains(c);
      const want = force === undefined ? !has : !!force;
      if (want && !has) el.classList.add(c);
      if (!want && has) el.classList.remove(c);
      return want;
    },
  };

  el.addEventListener = (type, fn) => {
    (el._listeners[type] || (el._listeners[type] = [])).push(fn);
  };
  el.removeEventListener = (type, fn) => {
    const arr = el._listeners[type];
    if (!arr) return;
    const i = arr.indexOf(fn);
    if (i !== -1) arr.splice(i, 1);
  };
  el.setAttribute = (k, v) => {
    const key = String(k);
    el._attrs[key] = String(v);
    if (key.startsWith("data-")) {
      const camel = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      el.dataset[camel] = String(v);
    }
  };
  el.getAttribute = (k) =>
    Object.prototype.hasOwnProperty.call(el._attrs, k) ? el._attrs[k] : null;
  el.removeAttribute = (k) => {
    delete el._attrs[k];
  };
  el.appendChild = (child) => {
    if (child.parentNode) child.remove();
    el.children.push(child);
    child.parentNode = el;
    return child;
  };
  el.insertBefore = (child, ref) => {
    if (child.parentNode) child.remove();
    const i = ref ? el.children.indexOf(ref) : -1;
    if (i === -1) el.children.push(child);
    else el.children.splice(i, 0, child);
    child.parentNode = el;
    return child;
  };
  el.removeChild = (child) => {
    const i = el.children.indexOf(child);
    if (i !== -1) el.children.splice(i, 1);
    child.parentNode = null;
    return child;
  };
  el.remove = () => {
    if (el.parentNode) el.parentNode.removeChild(el);
  };
  el.querySelectorAll = (sel) => {
    const out = [];
    const visit = (node) => {
      for (const c of node.children) {
        if (matches(c, sel)) out.push(c);
        visit(c);
      }
    };
    visit(el);
    return out;
  };
  // Real-browser semantics: null when nothing matches. The implementation's
  // "settings group already rendered?" guard depends on this.
  el.querySelector = (sel) => {
    const found = el.querySelectorAll(sel);
    return found.length ? found[0] : null;
  };
  el.closest = (sel) => {
    let n = el;
    while (n) {
      if (matches(n, sel)) return n;
      n = n.parentNode;
    }
    return null;
  };

  Object.defineProperty(el, "className", {
    get() {
      return el._className;
    },
    set(v) {
      el._className = String(v);
    },
  });
  Object.defineProperty(el, "innerHTML", {
    get() {
      return el._innerHTML;
    },
    set(v) {
      el._innerHTML = String(v);
      el.children = [];
      parseInto(el, el._innerHTML);
    },
  });
  Object.defineProperty(el, "textContent", {
    get() {
      if (el.children.length) return el.children.map((c) => c.textContent).join("");
      return el._textContent;
    },
    set(v) {
      el._textContent = String(v);
      el.children = [];
    },
  });
  return el;
}

// Process-wide singleton document with per-id cached elements, so
// document.getElementById("plan-list") always returns the SAME node the
// implementation rendered into.
const doc = {
  _byId: new Map(),
  getElementById(id) {
    if (!this._byId.has(id)) this._byId.set(id, makeElement("div"));
    return this._byId.get(id);
  },
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeElement(tag),
  createTextNode: (t) => makeTextNode(t),
  body: makeElement("body"),
  documentElement: makeElement("html"),
  head: makeElement("head"),
  title: "",
  hidden: false,
  addEventListener: noop,
  removeEventListener: noop,
};

function makeWindowStub() {
  const win = {
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "{}" }),
    document: doc,
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
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 0),
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  };
  win.window = win;
  win.self = win;
  return win;
}

// Browser globals must exist BEFORE plan-list.js (and its state.js import,
// which reads `window` at module-evaluation time) is evaluated.
const win = makeWindowStub();
globalThis.window = win;
globalThis.document = doc;
globalThis.localStorage = win.localStorage;
globalThis.fetch = win.fetch;

const planListMod = await import("../../static/app/render/plan-list.js");
const stateMod = await import("../../static/app/state.js");

// ---------- helpers ----------

function nav() {
  return doc.getElementById("plan-list");
}

const SAMPLE_PLANS = [
  { name: "alpha-plan", story_count: 4, status_counts: { done: 1 } },
];

// Fresh module + DOM state, then one render with the injected callbacks.
function renderOnce({ plans = SAMPLE_PLANS } = {}) {
  const calls = { workspace: 0, config: 0 };
  if (typeof planListMod.resetPlanListState === "function") {
    planListMod.resetPlanListState();
  }
  if (typeof stateMod.resetState === "function") stateMod.resetState();
  nav().innerHTML = "";
  planListMod.initPlanList({
    selectPlan: noop,
    selectComms: noop,
    selectOverview: noop,
    selectConfig: () => {
      calls.config += 1;
    },
    selectWorkspace: () => {
      calls.workspace += 1;
    },
    refresh: noop,
  });
  planListMod.renderPlanList(plans);
  return calls;
}

function settingsGroup() {
  return nav().querySelector("[data-sidebar-settings]");
}

function click(el) {
  for (const fn of el._listeners.click || []) fn({ type: "click", target: el });
}

async function run(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

// ---------- the group itself ----------

await run("#sidebar-settings is the LAST child of #plan-list", async () => {
  renderOnce();
  const group = settingsGroup();
  assertTrue(group, "expected a [data-sidebar-settings] group in #plan-list");
  assertEqual(group.id, "sidebar-settings", "group id");
  const kids = nav().children;
  assertTrue(
    kids[kids.length - 1] === group,
    "the settings group must be the last child of #plan-list",
  );
});

await run("group contains SYSTEM label, then Workspace, then Config", async () => {
  renderOnce();
  const group = settingsGroup();
  assertTrue(group, "expected a [data-sidebar-settings] group in #plan-list");
  const kids = group.children;
  assertEqual(kids.length, 3, "group should hold exactly label + 2 items");
  assertEqual(kids[0].textContent, "SYSTEM", "first child text");
  assertEqual(kids[1].getAttribute("data-settings"), "workspace", "second child");
  assertEqual(kids[2].getAttribute("data-settings"), "config", "third child");
});

await run("label carries .plan-list-section-label", async () => {
  renderOnce();
  const label = settingsGroup().children[0];
  assertTrue(
    label.classList.contains("plan-list-section-label"),
    "SYSTEM label should carry .plan-list-section-label",
  );
});

await run("both items carry .plan-item settings-item", async () => {
  renderOnce();
  const kids = settingsGroup().children;
  for (const [i, expected] of [
    [1, "workspace"],
    [2, "config"],
  ]) {
    const item = kids[i];
    assertTrue(item.classList.contains("plan-item"), `${expected} item needs .plan-item`);
    assertTrue(item.classList.contains("settings-item"), `${expected} item needs .settings-item`);
  }
});

await run("Workspace item renders its name and meta line", async () => {
  renderOnce();
  const item = settingsGroup().children[1];
  assertEqual(item.querySelector(".plan-name").textContent, "Workspace", "Workspace name");
  assertEqual(item.querySelector(".plan-meta").textContent, "active repo", "Workspace meta");
});

await run("Config item renders its name and meta line", async () => {
  renderOnce();
  const item = settingsGroup().children[2];
  assertEqual(item.querySelector(".plan-name").textContent, "Config", "Config name");
  assertEqual(item.querySelector(".plan-meta").textContent, "roles & env", "Config meta");
});

// ---------- callbacks ----------

await run("clicking Workspace calls the injected selectWorkspace once", async () => {
  const calls = renderOnce();
  click(settingsGroup().children[1]);
  assertEqual(calls.workspace, 1, "selectWorkspace call count");
  assertEqual(calls.config, 0, "selectConfig must not fire");
});

await run("clicking Config calls the injected selectConfig once", async () => {
  const calls = renderOnce();
  click(settingsGroup().children[2]);
  assertEqual(calls.config, 1, "selectConfig call count");
  assertEqual(calls.workspace, 0, "selectWorkspace must not fire");
});

// ---------- idempotence ----------

await run("a second renderPlanList does not duplicate the group", async () => {
  renderOnce();
  planListMod.renderPlanList(SAMPLE_PLANS);
  planListMod.renderPlanList(SAMPLE_PLANS);
  const groups = nav().querySelectorAll("[data-sidebar-settings]");
  assertEqual(groups.length, 1, "exactly one settings group");
  assertEqual(groups[0].children.length, 3, "group still holds label + 2 items");
});

await run("a full rebuild after resetPlanListState does not duplicate the group", async () => {
  renderOnce();
  // Simulate a later full rebuild: the keyed-diff map is dropped, so
  // renderPlanList takes its first-call branch again.
  planListMod.resetPlanListState();
  planListMod.renderPlanList(SAMPLE_PLANS);
  const groups = nav().querySelectorAll("[data-sidebar-settings]");
  assertEqual(groups.length, 1, "exactly one settings group after a rebuild");
});

// ---------- unchanged ordering ----------

await run("Comms/Overview/PLANS ordering is unchanged", async () => {
  renderOnce();
  const kids = nav().children;
  const idx = (pred) => kids.findIndex(pred);
  const commsIdx = idx((el) => el.getAttribute("data-comms") !== null);
  const overviewIdx = idx((el) => el.getAttribute("data-overview") !== null);
  const plansLabelIdx = idx(
    (el) => el.classList.contains("plan-list-section-label") && el.textContent === "PLANS",
  );
  const firstPlanRowIdx = idx((el) => el.getAttribute("data-plan-name") !== null);
  const settingsIdx = idx((el) => el.getAttribute("data-sidebar-settings") !== null);

  assertTrue(commsIdx !== -1, "Comms item missing");
  assertTrue(overviewIdx !== -1, "Overview item missing");
  assertTrue(plansLabelIdx !== -1, "PLANS label missing");
  assertTrue(firstPlanRowIdx !== -1, "plan row missing");
  assertTrue(settingsIdx !== -1, "settings group missing");

  assertTrue(commsIdx < overviewIdx, "Comms must come before Overview");
  assertTrue(overviewIdx < plansLabelIdx, "Overview must come before the PLANS label");
  assertTrue(plansLabelIdx < firstPlanRowIdx, "PLANS label must come before the plan rows");
  assertTrue(firstPlanRowIdx < settingsIdx, "settings group must come after the plan rows");
});

// ---------- active workspace basename (SIDEBAR-5) ----------
//
// The Workspace item's `.plan-meta` shows the basename of the active
// repository path, fetched once per render via workspace.js's
// fetchActiveWorkspace(). The fetch is asynchronous, so every assertion here
// awaits settle() first: immediately after renderPlanList() returns, the
// meta line still holds the pre-fetch placeholder.

// Swap the process-wide fetch stub for one that answers /api/workspace with
// `active` (or rejects when `reject` is true). Returns a restore function so
// each test leaves the shared stub as it found it.
function stubActiveWorkspaceFetch({ active = null, reject = false } = {}) {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => {
    if (reject) throw new Error("network down");
    return {
      ok: true,
      status: 200,
      json: async () => ({ active }),
      text: async () => "{}",
    };
  };
  win.fetch = globalThis.fetch;
  return () => {
    globalThis.fetch = previous;
    win.fetch = previous;
  };
}

// Flush the microtask queue plus one macrotask tick so the promise chain
// inside plan-list.js (fetch -> res.json -> DOM update) has fully settled.
async function settle() {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

// The Workspace item is distinguished from the Config item by its
// data-settings="workspace" attribute. The stub's attribute selector only
// supports presence tests, so filter by value explicitly.
function workspaceItem() {
  const group = settingsGroup();
  assertTrue(group, "expected a [data-sidebar-settings] group in #plan-list");
  const items = group.querySelectorAll("[data-settings]");
  const item = items.find((el) => el.getAttribute("data-settings") === "workspace");
  assertTrue(item, "expected the Workspace settings item");
  return item;
}

function workspaceMeta() {
  const meta = workspaceItem().querySelector(".plan-meta");
  assertTrue(meta, "expected a .plan-meta line on the Workspace item");
  return meta;
}

// The fallback state must carry NO title attribute at all — an empty-string
// title would still render a tooltip, so assert absence, not emptiness.
function hasNoTitle(el) {
  if (typeof el.hasAttribute === "function" && el.hasAttribute("title")) return false;
  return el.getAttribute("title") === null;
}

await run("Workspace meta shows the active repo basename and full path title", async () => {
  const restore = stubActiveWorkspaceFetch({ active: "/tmp/proj/alpha" });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "alpha", "Workspace meta text");
    assertEqual(meta.getAttribute("title"), "/tmp/proj/alpha", "Workspace meta title");
  } finally {
    restore();
  }
});

await run("a trailing slash is stripped from the basename but kept in the title", async () => {
  const restore = stubActiveWorkspaceFetch({ active: "/tmp/proj/alpha/" });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "alpha", "Workspace meta text");
    assertEqual(meta.getAttribute("title"), "/tmp/proj/alpha/", "Workspace meta title");
  } finally {
    restore();
  }
});

await run("no active workspace falls back to 'active repo' with no title", async () => {
  const restore = stubActiveWorkspaceFetch({ active: null });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "active repo", "Workspace meta text");
    assertTrue(hasNoTitle(meta), "the fallback must not carry a title attribute");
  } finally {
    restore();
  }
});

await run("a rejected active-workspace fetch falls back without throwing", async () => {
  const rejections = [];
  const onRejection = (err) => rejections.push(err);
  process.on("unhandledRejection", onRejection);
  const restore = stubActiveWorkspaceFetch({ reject: true });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "active repo", "Workspace meta text");
    assertTrue(hasNoTitle(meta), "the fallback must not carry a title attribute");
    assertEqual(rejections.length, 0, "no unhandled rejection may escape");
  } finally {
    restore();
    process.removeListener("unhandledRejection", onRejection);
  }
});

await run("successive renders replace the basename and clear a stale title", async () => {
  let restore = stubActiveWorkspaceFetch({ active: "/tmp/proj/alpha" });
  try {
    renderOnce();
    await settle();
    let meta = workspaceMeta();
    assertEqual(meta.textContent, "alpha", "call 1 text");
    assertEqual(meta.getAttribute("title"), "/tmp/proj/alpha", "call 1 title");
  } finally {
    restore();
  }

  restore = stubActiveWorkspaceFetch({ active: null });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "active repo", "call 2 text");
    assertTrue(hasNoTitle(meta), "call 2 must drop the previous title");
  } finally {
    restore();
  }

  restore = stubActiveWorkspaceFetch({ active: "/tmp/proj/beta/" });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "beta", "call 3 text");
    assertEqual(meta.getAttribute("title"), "/tmp/proj/beta/", "call 3 title");
  } finally {
    restore();
  }
});

await run("a root active path falls back to 'active repo' but keeps its title", async () => {
  const restore = stubActiveWorkspaceFetch({ active: "/" });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "active repo", "root path meta text");
    assertEqual(meta.getAttribute("title"), "/", "root path keeps the full path as title");
  } finally {
    restore();
  }
});

// The keyed-diff path (planListRowsByName already populated) is what every
// poll tick and every workspace switch takes: selectWorkspace() fires the
// workspace-selected hook -> refresh() -> renderPlanList(plans) WITHOUT a
// resetPlanListState(). The label must refresh there too, otherwise the
// Workspace item keeps showing the previous repository's basename.
await run("the keyed-diff render path refreshes the label after a workspace switch", async () => {
  let restore = stubActiveWorkspaceFetch({ active: "/tmp/proj/alpha" });
  try {
    renderOnce();
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "alpha", "first render text");
    assertEqual(meta.getAttribute("title"), "/tmp/proj/alpha", "first render title");
  } finally {
    restore();
  }

  // Second render on the SAME module/DOM state: no resetPlanListState(), no
  // nav().innerHTML = "" — exactly what _refresh() does after a switch.
  restore = stubActiveWorkspaceFetch({ active: "/tmp/proj/beta/" });
  try {
    planListMod.renderPlanList(SAMPLE_PLANS);
    await settle();
    const meta = workspaceMeta();
    assertEqual(meta.textContent, "beta", "keyed-diff render text");
    assertEqual(meta.getAttribute("title"), "/tmp/proj/beta/", "keyed-diff render title");
  } finally {
    restore();
  }
});

// ---------- summary ----------

const failed = results.filter((r) => !r.ok);
// eslint-disable-next-line no-console
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  // eslint-disable-next-line no-console
  console.error(`\n${failed.length} FAILED:`);
  for (const f of failed) console.error(`  - ${f.name}: ${f.detail}`);
  process.exit(1);
}
