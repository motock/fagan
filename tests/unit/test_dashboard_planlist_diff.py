"""Node-eval harness tests for the keyed-diff refactor of renderPlanList in
static/app.js.

This file mirrors the harness pattern in tests/unit/test_dashboard.py and
tests/unit/test_dashboard_notifications_ui.py: it builds a minimal (but
richer) DOM shim, evals static/app.js under `node -e`, and JSON-stringifies
the result of a test expression. The shim is copied + extended here (rather
than imported) so this file stands alone and does not touch the other test
files.

EXTENSION TO THE SHIM (this file only): `document.createElement` tags every
created element with a unique incrementing `__testId` number, and
`appendChild` / `innerHTML=""` actually maintain a `children` array on each
node, and `innerHTML` parses simple open/close tag pairs so
`querySelector`/`querySelectorAll` can locate `.plan-meta`, `.plan-name` and
`.plan-archive-btn`. This lets a test detect whether a `.plan-item` row was
REUSED across two renderPlanList calls (same `__testId`) or RECREATED (new
`__testId`).

These tests are RED until the implementation lands:
  - the current renderPlanList body must be renamed to
    `_renderPlanListFull(plans)` (unchanged full-rebuild),
  - a new `renderPlanList(plans)` wrapper must do a keyed diff by plan name
    using a module-level `let planListRowsByName = null;`,
  - each per-plan `.plan-item` row must carry a `data-plan-name` attribute,
  - renderPlanList must be added to module.exports.
"""
import json
import os

from _app_js import run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


# A self-contained, richer DOM shim. Built once as a Python string and
# embedded in every node invocation. The shim implements just enough of the
# DOM for renderPlanList: createElement with unique __testId, appendChild
# maintaining a children array, innerHTML="" clearing children, innerHTML
# parsing simple open/close tag pairs into child nodes, and
# querySelector/querySelectorAll matching a single class selector.
_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    // Parse a tiny subset of HTML (open/close tag pairs and text) into child
    // element nodes. Each parsed element gets its own __testId so it is
    // distinguishable, and supports class-based querySelector. Attributes we
    // care about (class, title, type) are captured.
    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        // Tokenize tags and text.
        const re = /<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*>([\s\S]*?)<\/\1>|<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*\/?>/g;
        let m;
        while ((m = re.exec(html)) !== null) {
            const tag = m[1] || m[4];
            const attrs = m[2] || m[5] || "";
            const inner = m[3] || "";
            const el = __newEl(tag);
            // parse attributes
            const are = /([a-zA-Z-]+)="([^"]*)"/g;
            let am;
            while ((am = are.exec(attrs)) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else el.setAttribute(am[1], am[2]);
            }
            if (inner) {
                // Recurse for nested tags; also keep raw text content.
                __parseHtml(inner, el);
                el.__text = inner.replace(/<[^>]*>/g, "");
            }
            parent.__children.push(el);
            el.__parent = parent;
        }
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            tagName: (tag || "div").toUpperCase(),
            innerHTML: "",
            textContent: "",
            className: "",
            title: "",
            type: "",
            checked: false,
            dataset: {},
            classList: {
                _classes: () => (el.className || "").split(/\s+/).filter(Boolean),
                add: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.add(x)); el.className = [...s].join(" "); },
                remove: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.delete(x)); el.className = [...s].join(" "); },
                toggle: (c, f) => { const s = new Set(el.classList._classes()); if (f === undefined) f = !s.has(c); if (f) s.add(c); else s.delete(c); el.className = [...s].join(" "); },
                contains: (c) => el.classList._classes().includes(c),
            },
            addEventListener: noop,
            removeEventListener: noop,
            setAttribute: (k, v) => {
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k.startsWith("data-")) el.dataset[k.slice(5)] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k.startsWith("data-")) return el.dataset[k.slice(5)];
                return el[k] != null ? String(el[k]) : null;
            },
            appendChild: (child) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                el.__children.push(child);
                child.__parent = el;
                return child;
            },
            removeChild: (child) => {
                const i = el.__children.indexOf(child);
                if (i >= 0) el.__children.splice(i, 1);
                child.__parent = null;
                return child;
            },
            remove: () => {
                if (el.__parent) el.__parent.removeChild(el);
            },
            querySelector: (sel) => {
                return el.__queryAll(sel)[0] || null;
            },
            querySelectorAll: (sel) => {
                return el.__queryAll(sel);
            },
            __queryAll: (sel) => {
                const out = [];
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (__matches(c, sel)) out.push(c);
                        walk(c);
                    }
                };
                walk(el);
                return out;
            },
        };
        // innerHTML setter: parse into children. We use a JS getter/setter via
        // Object.defineProperty so assignment `el.innerHTML = "..."` works.
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => {
                el.__innerHTMLRaw = String(v);
                __parseHtml(String(v), el);
            },
            configurable: true,
        });
        // textContent setter: just store text, clear children.
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        return el;
    }

    function __matches(el, sel) {
        // Support a single ".classname" selector only.
        if (!sel) return false;
        if (sel.startsWith(".")) {
            const cls = sel.slice(1);
            return el.classList.contains(cls);
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[key] === val;
            return el.dataset[key] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            return __newEl("div");
        },
        createElement: (tag) => __newEl(tag),
    };
    globalThis.window = {
        location: { hash: "" },
        addEventListener: noop,
    };
    globalThis.localStorage = { getItem: () => null, setItem: noop };
    globalThis.fetch = () => new Promise(() => {});
    process.on("unhandledRejection", () => {});
    globalThis.setInterval = () => 0;
    globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    script = (
        _SHIM
        + "const fs = require('fs');"
        + f"eval(fs.readFileSync({json.dumps(APP_JS)}, 'utf8'));"
        + "globalThis.state = globalThis.window.state;"
        + "process.stdout.write(JSON.stringify(" + expr + "));"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _plan(name, done=0, total=0, archived=False, paused=False):
    """Build a plan dict shaped like /api/plans entries."""
    return {
        "name": name,
        "story_count": total,
        "status_counts": {"done": done},
        "archived": archived,
        "paused": paused,
    }


# === Static-source rename-and-delegate assertions ===========================

def test_render_plan_list_full_implementation_renamed():
    """The current full-rebuild body must be renamed to _renderPlanListFull."""
    js = _app_js_source()
    assert "function _renderPlanListFull(plans)" in js
    # The old single-function full rebuild must no longer be the public
    # entry point: there must be a NEW wrapper named renderPlanList that
    # delegates. (Both names must exist.)
    assert "function renderPlanList(plans)" in js


def test_plan_list_rows_by_name_module_state_exists():
    """A module-level `let planListRowsByName = null;` must exist to track
    the first-call vs subsequent-call state."""
    js = _app_js_source()
    assert "planListRowsByName" in js
    assert "let planListRowsByName = null" in js


def test_render_plan_list_is_exported():
    """renderPlanList must be present in module.exports so it can be tested
    directly via the node harness and called by name from other modules."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out.renderPlanList;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_render_plan_list_full_is_exported():
    """_renderPlanListFull must be exported so the harness can drive the
    first-call full rebuild path directly if needed."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out._renderPlanListFull;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_plan_item_rows_carry_data_plan_name_attribute():
    """Each per-plan .plan-item row must carry a data-plan-name attribute so
    the diff can key rows by plan name robustly (not positional indexing)."""
    js = _app_js_source()
    assert "data-plan-name" in js


# === Harness helper: render twice and report per-plan __testId + meta ========

def _render_twice(plans_a, plans_b):
    """Call renderPlanList twice (resetting the module-level diff state is
    NOT done between - the whole point is the second call diffs against the
    first). Returns a JSON object describing the per-plan rows after each
    call: { first: [{name, testId, meta, classes}], second: [...] }.

    Rows are identified by their data-plan-name attribute; pinned
    Overview/footer items are excluded (they have no data-plan-name)."""
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        " const collect = () => {"
        "  const rows = [];"
        "  const walk = (node) => {"
        "   for (const c of (node.__children || [])) {"
        "    const pn = c.dataset ? c.dataset['plan-name'] : undefined;"
        "    if (pn) {"
        "     const meta = c.querySelector('.plan-meta');"
        "     rows.push({ name: pn, testId: c.__testId,"
        "       meta: meta ? (meta.__text || meta.textContent || '') : '',"
        "       classes: c.className || '' });"
        "    }"
        "    walk(c);"
        "   }"
        "  };"
        "  walk(nav);"
        "  return rows;"
        " };"
        f" renderPlanList({json.dumps(plans_a)});"
        " const first = collect();"
        f" renderPlanList({json.dumps(plans_b)});"
        " const second = collect();"
        " return { first, second };"
        " })()"
    )
    return _run_app_js(expr)


def _by_name(rows):
    return {r["name"]: r for r in rows}


# === Identical plans: no row recreated ======================================

def test_identical_plans_second_call_does_not_recreate_rows():
    """Calling renderPlanList twice with the IDENTICAL plans array must not
    remove-and-recreate any row: each per-plan row's __testId is unchanged
    across both calls."""
    plans = [_plan("alpha", done=1, total=3), _plan("beta", done=2, total=5)]
    res = _render_twice(plans, plans)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert set(first) == set(second) == {"alpha", "beta"}
    for name in ("alpha", "beta"):
        assert first[name]["testId"] == second[name]["testId"], (
            f"row {name} was recreated across identical calls"
        )


def test_identical_plans_second_call_preserves_meta():
    """Meta text must still be correct after the no-op second call."""
    plans = [_plan("alpha", done=1, total=3)]
    res = _render_twice(plans, plans)
    second = _by_name(res["second"])
    assert "1/3" in second["alpha"]["meta"]


# === Changed done count: same node, updated meta ============================

def test_changed_done_count_reuses_node_and_updates_meta():
    """A plan whose `done` count changed on the second call: that plan's row
    is the SAME node (__testId unchanged) but its rendered meta text reflects
    the new count."""
    a = [_plan("alpha", done=1, total=3)]
    b = [_plan("alpha", done=2, total=3)]
    res = _render_twice(a, b)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert "alpha" in first and "alpha" in second
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "row alpha was recreated when only its done count changed"
    )
    assert "1/3" in first["alpha"]["meta"]
    assert "2/3" in second["alpha"]["meta"]
    assert "1/3" not in second["alpha"]["meta"]


def test_changed_paused_state_reuses_node_and_updates_meta():
    """A plan whose paused flag flips on the second call: same node, meta
    reflects the paused marker."""
    a = [_plan("alpha", done=1, total=3, paused=False)]
    b = [_plan("alpha", done=1, total=3, paused=True)]
    res = _render_twice(a, b)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert first["alpha"]["testId"] == second["alpha"]["testId"]
    assert "paused" not in first["alpha"]["meta"].lower() or "paused" in second["alpha"]["meta"].lower()
    assert "paused" in second["alpha"]["meta"].lower()


def test_changed_archived_state_reuses_node_and_updates_class():
    """A plan whose archived flag flips on the second call: same node, the
    plan-archived class is toggled on the EXISTING node (not recreated)."""
    a = [_plan("alpha", done=1, total=3, archived=False)]
    b = [_plan("alpha", done=1, total=3, archived=True)]
    res = _render_twice(a, b)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "row alpha was recreated when only its archived flag changed"
    )
    assert "plan-archived" not in first["alpha"]["classes"]
    assert "plan-archived" in second["alpha"]["classes"]


def test_changed_total_count_reuses_node_and_updates_meta():
    """A plan whose total count changed: same node, meta reflects new total."""
    a = [_plan("alpha", done=1, total=3)]
    b = [_plan("alpha", done=1, total=4)]
    res = _render_twice(a, b)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert first["alpha"]["testId"] == second["alpha"]["testId"]
    assert "1/3" in first["alpha"]["meta"]
    assert "1/4" in second["alpha"]["meta"]


# === New plan added: new row appears, existing rows unchanged ===============

def test_new_plan_added_creates_new_row_preserves_existing():
    """A NEW plan added on the second call: a new row appears for it; the
    existing rows' __testId values are unchanged."""
    a = [_plan("alpha", done=1, total=3)]
    b = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=2)]
    res = _render_twice(a, b)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert set(first) == {"alpha"}
    assert set(second) == {"alpha", "beta"}
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "existing row alpha was recreated when a new plan was added"
    )
    assert "beta" in second
    assert "0/2" in second["beta"]["meta"]


# === Plan removed: row gone, remaining rows unchanged =======================

def test_plan_removed_drops_row_preserves_remaining():
    """A plan REMOVED on the second call: that row is gone; the remaining
    rows' __testId values are unchanged."""
    a = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=2)]
    b = [_plan("alpha", done=1, total=3)]
    res = _render_twice(a, b)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert set(first) == {"alpha", "beta"}
    assert set(second) == {"alpha"}
    assert "beta" not in second
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "remaining row alpha was recreated when a plan was removed"
    )


# === Negative / boundary ====================================================

def test_empty_plans_first_call_does_not_throw():
    """renderPlanList([]) on the first call does not throw and results in
    zero per-plan rows (pinned items are a separate concern, not asserted)."""
    expr = (
        "(() => {"
        " let ok = true, err = null, count = -1;"
        " try {"
        "  renderPlanList([]);"
        "  const nav = document.getElementById('plan-list');"
        "  let n = 0;"
        "  const walk = (node) => { for (const c of (node.__children || [])) {"
        "   if (c.dataset && c.dataset['plan-name']) n++;"
        "   walk(c);"
        "  } };"
        "  walk(nav);"
        "  count = n;"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err, count };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["ok"] is True, f"renderPlanList([]) threw: {res.get('err')}"
    assert res["count"] == 0


def test_empty_then_populated_then_empty_roundtrip():
    """Boundary: empty -> one plan -> empty. Rows appear and disappear
    cleanly without throwing, and the single plan's row is a fresh node."""
    a = []
    b = [_plan("solo", done=0, total=1)]
    c = []
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        " const collect = () => {"
        "  const rows = [];"
        "  const walk = (node) => { for (const c of (node.__children || [])) {"
        "   if (c.dataset && c.dataset['plan-name']) rows.push({ name: c.dataset['plan-name'], testId: c.__testId });"
        "   walk(c);"
        "  } };"
        "  walk(nav); return rows;"
        " };"
        f" renderPlanList({json.dumps(a)});"
        " const r0 = collect();"
        f" renderPlanList({json.dumps(b)});"
        " const r1 = collect();"
        f" renderPlanList({json.dumps(c)});"
        " const r2 = collect();"
        " return { r0, r1, r2 };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["r0"] == []
    assert len(res["r1"]) == 1 and res["r1"][0]["name"] == "solo"
    assert res["r2"] == []


def test_single_plan_boundary_reused_on_identical_call():
    """Boundary: a single plan rendered twice identically is reused."""
    plans = [_plan("only", done=0, total=0)]
    res = _render_twice(plans, plans)
    first, second = _by_name(res["first"]), _by_name(res["second"])
    assert set(first) == set(second) == {"only"}
    assert first["only"]["testId"] == second["only"]["testId"]


def test_zero_done_zero_total_meta_rendered():
    """Boundary: done=0 total=0 renders a '0/0 done' meta without throwing."""
    plans = [_plan("zero", done=0, total=0)]
    res = _render_twice(plans, plans)
    second = _by_name(res["second"])
    assert "0/0" in second["zero"]["meta"]


# === Pinned items left untouched by the diff ================================

def test_pinned_overview_item_still_present_after_diff():
    """The pinned Overview item is not part of the per-plan diff and must
    still be present after a second renderPlanList call. It carries a
    data-overview attribute (not data-plan-name), so it is excluded from the
    per-plan row collection but must still exist in the nav."""
    plans = [_plan("alpha", done=1, total=3)]
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        f" renderPlanList({json.dumps(plans)});"
        f" renderPlanList({json.dumps(plans)});"
        " let overview = null;"
        " const walk = (node) => { for (const c of (node.__children || [])) {"
        "  if (c.dataset && c.dataset['overview'] === 'true') overview = c;"
        "  walk(c);"
        " } };"
        " walk(nav);"
        " return { hasOverview: !!overview, classes: overview ? overview.className : '' };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["hasOverview"] is True
    assert "overview-item" in res["classes"]


def test_pinned_overview_active_class_untouched_when_no_plan_selected():
    """The pinned Overview .active class logic must remain exactly as today:
    when state.selectedPlan is falsy AND Comms is not active, Overview is
    active. This is not part of the per-plan diff and must not be disturbed
    by it."""
    plans = [_plan("alpha", done=1, total=3)]
    expr = (
        "(() => {"
        " state.selectedPlan = null;"
        " state.commsActive = false;"
        " const nav = document.getElementById('plan-list');"
        f" renderPlanList({json.dumps(plans)});"
        f" renderPlanList({json.dumps(plans)});"
        " let overview = null;"
        " const walk = (node) => { for (const c of (node.__children || [])) {"
        "  if (c.dataset && c.dataset['overview'] === 'true') overview = c;"
        "  walk(c);"
        " } };"
        " walk(nav);"
        " return overview ? overview.className : '';"
        " })()"
    )
    classes = _run_app_js(expr)
    assert "active" in classes
    assert "overview-item" in classes


def test_pinned_active_classes_update_after_state_change_between_calls():
    """Regression test: the pinned Comms/Overview .active classes must be
    re-toggled on EVERY renderPlanList call, not just the first (full
    rebuild) one. Before the fix, the diff path (second and later calls)
    never re-toggled these classes, so selecting Comms or navigating away
    from a plan never updated the sidebar highlighting after the initial
    load. This must fail if the toggle is moved back inside the
    `planListRowsByName === null` branch."""
    plans = [_plan("alpha", done=1, total=3)]
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        " const findPinned = () => {"
        "  let overview = null, comms = null;"
        "  const walk = (node) => { for (const c of (node.__children || [])) {"
        "   if (c.dataset && c.dataset['overview'] === 'true') overview = c;"
        "   if (c.dataset && c.dataset['comms'] === 'true') comms = c;"
        "   walk(c);"
        "  } };"
        "  walk(nav);"
        "  return { overview: overview.className, comms: comms.className };"
        " };"
        " state.selectedPlan = null;"
        " state.commsActive = false;"
        f" renderPlanList({json.dumps(plans)});"
        " const before = findPinned();"
        " state.commsActive = true;"
        f" renderPlanList({json.dumps(plans)});"
        " const after = findPinned();"
        " return { before, after };"
        " })()"
    )
    res = _run_app_js(expr)
    assert "active" in res["before"]["overview"]
    assert "active" not in res["before"]["comms"]
    assert "active" not in res["after"]["overview"]
    assert "active" in res["after"]["comms"]